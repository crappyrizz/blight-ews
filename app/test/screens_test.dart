// Tests for the diagnosis, history and error handling behind the new screens.
// No server: responses are hand-made in the backend's exact shape, and the
// network is a fake client.

import 'dart:convert';
import 'dart:io';

import 'package:blight_ews/api.dart';
import 'package:blight_ews/models.dart';
import 'package:blight_ews/report_screen.dart';
import 'package:blight_ews/scan_screen.dart';
import 'package:blight_ews/trend_chart.dart';
import 'package:blight_ews/update.dart';
import 'package:blight_ews/widgets.dart';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// POST /images response, in the backend's shape (ImageUploadOut).
Map<String, dynamic> uploadResponse({double r = 0.9, double tau = 0.44, double pLate = 0.55, bool flagged = true,
    String basis = 'learned_24h', String level = 'high'}) => {
      'image': {'image_id': 'IMG-1', 'farmer_id': 'FRM-1', 'capture_date': '2026-10-19T08:00:00Z'},
      'prediction': {
        'label': 'late_blight',
        'confidence': pLate,
        'probabilities': {'early_blight': 0.3, 'healthy': 0.15, 'late_blight': pLate},
      },
      'diagnosis': {
        'result_id': 'DX-1', 'label': 'late_blight', 'confidence': pLate,
        'risk_score_used': r, 'risk_basis': basis, 'risk_for_date': '2026-10-19',
        'risk_note': 'Risk from the 24-hour forecast made from the readings of 2026-10-18.',
        'threshold_used': tau, 'late_blight_flagged': flagged, 'risk_level': level,
        'summary': 'The photo shows signs that look like late blight, so it may be worth checking the nearby plants closely.',
        'contributing_readings': 288, 'sms_alert': 'dry_run',
      },
    };

void main() {
  group('Diagnosis', () {
    test('reads the upload response', () {
      final d = UploadResult.fromJson(uploadResponse()).diagnosis;
      expect(diseaseName(d.label), 'Late blight');
      expect(d.confidence, 0.55);
      expect(d.pLate, 0.55);
      expect(d.risk, 0.9);
      expect(d.threshold, 0.44);
      expect(d.flagged, isTrue);
      expect(d.riskLevel, RiskLevel.high);
      expect(d.smsAlert, 'dry_run');
    });

    test('explains how high risk lowered the bar', () {
      final d = UploadResult.fromJson(uploadResponse()).diagnosis;
      expect(d.reasoning, contains('Weather risk: 90%'));
      expect(d.reasoning, contains('lowered the bar from 60% to 44%'));
      expect(d.reasoning, contains('scored 55% for late blight, which reaches the bar, so it is flagged'));
    });

    test('explains how low risk raised the bar and the leaf was not flagged', () {
      final d = UploadResult.fromJson(uploadResponse(r: 0.1, tau: 0.76, flagged: false, level: 'low')).diagnosis;
      expect(d.reasoning, contains('raised the bar from 60% to 76%'));
      expect(d.reasoning, contains('below the bar, so it is not flagged'));
    });

    test('no weather data is shown as unknown risk, and says so', () {
      final d = UploadResult.fromJson(uploadResponse(r: 0.5, tau: 0.6, basis: 'neutral_prior', level: 'medium')).diagnosis;
      expect(d.riskLevel, RiskLevel.unknown);
      expect(d.reasoning, contains('kept the bar at 60%'));
      expect(d.reasoning, contains('no recent weather readings'));
    });
  });

  group('History', () {
    final history = History.fromJson({
      'risk': [
        {'for_date': '2026-10-19', 'hutton': {'sensor': {'label': 'Incomplete data (14 of 24 hours)'}}, 'risk_level': null},
        {'for_date': '2026-10-18', 'hutton': {'sensor': {'label': 'High risk'}}, 'risk_level': 'high'},
        {'for_date': '2026-10-17', 'hutton': {'sensor': null}, 'risk_level': null},
      ],
      'diagnoses': [
        {'timestamp': '2026-10-18T07:30:00+00:00', 'disease_label': 'late_blight', 'confidence': 0.82,
         'risk_level': 'high', 'late_blight_flagged': true},
      ],
      'alerts': [
        {'alert_type': 'high_risk', 'message': 'Blight risk ... HIGH', 'sent_at': '2026-10-18T05:00:00+00:00'},
      ],
    });

    test('newest first, each day led by its weather risk', () {
      final kinds = history.items.map((i) => i.kind).toList();
      expect(kinds, [
        HistoryKind.risk, // 19 Oct
        HistoryKind.risk, HistoryKind.diagnosis, HistoryKind.alert, // 18 Oct: the day, then photo 10:30, SMS 08:00
        HistoryKind.risk, // 17 Oct
      ]);
    });

    test('gaps are shown, not hidden', () {
      final gap = history.items.last;
      expect(gap.title, 'Weather risk: not known');
      expect(gap.detail, 'No sensor readings');
      expect(history.items.first.detail, 'Incomplete data (14 of 24 hours)');
    });

    test('photos and SMS read plainly', () {
      expect(history.items[2].title, 'Leaf photo: Late blight (82%)');
      expect(history.items[2].detail, 'Late blight flagged');
      expect(history.items[3].title, 'SMS: high risk warning');
      expect(dayHeading(DateTime(2026, 10, 18)), 'Sun 18 Oct');
    });
  });

  group('Trend', () {
    // 14 days, 1 to 14 Oct, newest first as the backend sends them. Hutton
    // scores: a dry first week, then blight weather; the forecast stored under
    // a day (made from its readings) is about the next day.
    Map<String, dynamic> day(int d, double? measured, double? p24) => {
          'for_date': '2026-10-${d.toString().padLeft(2, '0')}',
          'hutton': {'sensor': measured == null ? null : {'score': measured, 'label': 'x'}},
          'learned': {'p24': {'sensor': p24, 'api': null}, 'p48': {'sensor': null, 'api': null}},
          'risk_level': null,
        };
    final measured = <double?>[0, 0, 0, 0, 0, 0, 0, 0.5, 0.5, 1, 1, 0, 0.5, 1];
    final forecasts = <double?>[0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.4, 0.5, 0.8, 0.9, 0.2, 0.3, 0.7, 0.42];
    Trend trendOf(List<double?> m, List<double?> f, {Map<String, dynamic>? bands}) => History.fromJson({
          'bands': bands ?? {'medium_from': 0.33, 'high_from': 0.66},
          'risk': [for (var d = m.length; d >= 1; d--) day(d, m[d - 1], f[d - 1])],
        }).trend!;

    test('each forecast sits on the day it was about, and tomorrow ends the chart', () {
      final t = trendOf(measured, forecasts);
      expect(t.points.length, 15);
      expect(t.points.first.day, DateTime(2026, 10, 1));
      expect(t.points.first.forecast, isNull); // made on 30 Sep: outside the window
      expect(t.points[1].forecast, 0.1); // made on 1 Oct, about 2 Oct
      expect(t.points[13].measured, 1.0);
      expect(t.points[13].forecast, 0.7);
      expect(t.tomorrow.day, DateTime(2026, 10, 15));
      expect(t.tomorrow.measured, isNull);
      expect(t.tomorrow.forecast, 0.42);
    });

    test('bands come from the server', () {
      final t = trendOf(measured, forecasts, bands: {'medium_from': 0.4, 'high_from': 0.8});
      expect(t.band(0.39), RiskLevel.low);
      expect(t.band(0.4), RiskLevel.medium);
      expect(t.band(0.8), RiskLevel.high);
    });

    test('summary: direction, blight days, tomorrow, and how the forecasts did', () {
      final s = trendOf(measured, forecasts).summary;
      expect(s, contains('Blight weather has become more frequent over the last week.'));
      expect(s, contains('Blight weather on 6 of the 14 days with full readings, most recently on 14 Oct.'));
      expect(s, contains("The model's forecast for tomorrow: 42% (moderate)."));
      // Forecast vs measured band on 2-14 Oct: the same every day except 13 Oct (low forecast, moderate measured).
      expect(s, contains('On 12 of 13 days, the forecast was in the same band as the weather that followed.'));
    });

    test('gaps stay gaps, and too little data says so instead of guessing', () {
      final t = trendOf([null, null, 0.5, null], [null, 0.6, null, null]);
      expect(t.points[0].measured, isNull);
      expect(t.points[2].forecast, 0.6);
      expect(t.tomorrow.forecast, isNull);
      expect(t.summary, contains('Too few days with full sensor readings yet'));
      expect(t.summary, contains('No forecast for tomorrow yet.'));
      expect(t.summary, contains('Blight weather on 1 of the 1 days'));
    });

    testWidgets('the card draws the chart, the key and the sentences', (tester) async {
      await tester.pumpWidget(MaterialApp(home: Scaffold(body: SingleChildScrollView(
          child: TrendCard(trend: trendOf(measured, forecasts))))));
      expect(find.text('Trend'), findsOneWidget);
      expect(find.text("Model's forecast for that day, made the day before"), findsOneWidget);
      expect(find.textContaining('more frequent over the last week'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });

  group('Leaf check with several photos', () {
    test('a scan reply carries each photo and whether they agreed', () {
      final reply = uploadResponse()
        ..['photos'] = [
          {'image_id': 'IMG-1', 'label': 'late_blight', 'confidence': 0.8, 'probabilities': {}},
          {'image_id': 'IMG-2', 'label': 'healthy', 'confidence': 0.7, 'probabilities': {}},
        ]
        ..['photos_agree'] = false;
      final d = UploadResult.fromJson(reply).diagnosis;
      expect(d.photos.map((p) => p.label), ['late_blight', 'healthy']);
      expect(d.photos[1].confidence, 0.7);
      expect(d.photosAgree, isFalse);
    });

    test('a reply without photos (one photo, older server) still reads', () {
      final d = UploadResult.fromJson(uploadResponse()).diagnosis;
      expect(d.photos, isEmpty);
      expect(d.photosAgree, isTrue);
    });

    test('the photos to retake are known by position', () {
      final e = Api.explain(422, {
        'detail': {
          'reason': 'too_dark',
          'message': 'Photo 2: too dark. Photo 3: blurry.',
          'photos': [
            {'index': 1, 'reason': 'too_dark', 'message': 'The photo is too dark.'},
            {'index': 2, 'reason': 'too_blurry', 'message': 'The photo is blurry.'},
          ],
        },
      }, '/scans');
      expect(e.rejectedPhotos, {1: 'The photo is too dark.', 2: 'The photo is blurry.'});
      expect(e.reason, 'too_dark');
      expect(Api.explain(422, {'detail': 'x'}, '/scans').rejectedPhotos, isEmpty);
    });

    testWidgets('the scan screen asks for three photos and waits for at least one', (tester) async {
      tester.view.physicalSize = const Size(1080, 2400); // a typical phone, 393 dp wide
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(MaterialApp(
          home: ScanScreen(node: SensorNode(nodeId: 'NODE-1', location: 'Plot'), onDiagnosed: () {})));
      expect(find.text('1. The whole leaf'), findsOneWidget);
      // Further down: the list builds what scrolls into view, as on the phone.
      await tester.scrollUntilVisible(find.text('2. Closer: the worst spot'), 200);
      await tester.scrollUntilVisible(find.text('3. The same leaf, another way'), 200);
      final button = find.widgetWithText(FilledButton, 'Take at least one photo');
      await tester.scrollUntilVisible(button, 200);
      final check = tester.widget<FilledButton>(button);
      expect(check.onPressed, isNull); // nothing to send yet
      expect(tester.takeException(), isNull); // no overflow
    });

    test('the photos go to /scans in one request, in order', () async {
      final dir = Directory.systemTemp.createTempSync('scan_test');
      addTearDown(() => dir.deleteSync(recursive: true));
      final photos = [for (final n in ['a', 'b', 'c']) File('${dir.path}/$n.jpg')..writeAsBytesSync([1, 2, 3, 4])];
      http.BaseRequest? sent;
      final api = Api(
        storage: _FakeStorage(),
        client: MockClient.streaming((request, body) async {
          sent = request;
          await body.drain<void>();
          return http.StreamedResponse(Stream.value(utf8.encode(jsonEncode(uploadResponse()))), 201);
        }),
      );
      final progress = <double>[];
      await api.uploadScan(photos, nodeId: 'NODE-1', onProgress: progress.add);
      final request = sent! as http.MultipartRequest;
      expect(request.url.path, '/scans');
      expect(request.fields['node_id'], 'NODE-1');
      expect(request.files.map((f) => (f.field, f.filename)), [('files', 'a.jpg'), ('files', 'b.jpg'), ('files', 'c.jpg')]);
      expect(progress.last, 1.0);
    });
  });

  group('Readable errors and status', () {
    test('dashboard warnings are read, and a server without them means none', () {
      final warnings = Warning.listFromJson([
        {'code': 'sensor_quiet', 'message': 'Your sensor has not sent readings for 5 hours.'},
      ]);
      expect(warnings.single.code, 'sensor_quiet');
      expect(warnings.single.message, contains('5 hours'));
      expect(Warning.listFromJson(null), isEmpty);
    });

    test('an admin is recognised, and an older server means a farmer', () {
      final admin = Farmer.fromJson({'farmer_id': 'F', 'name': 'A', 'phone_number': '+2547', 'role': 'admin'});
      final farmer = Farmer.fromJson({'farmer_id': 'F', 'name': 'B', 'phone_number': '+2547'});
      expect(admin.isAdmin, isTrue);
      expect(farmer.isAdmin, isFalse);
    });

    test('the system status is read part by part', () {
      final s = SystemStatus.fromJson({
        'overall': 'warning',
        'checks': [
          {'name': 'leaf_checker', 'status': 'ok', 'message': 'The CNN is loaded.'},
          {'name': 'sensors', 'status': 'warning', 'message': '1 of 2 sensors sent readings.'},
        ],
        'sensors': [
          {'node_id': 'node-02', 'location': 'Bench node B', 'owner': 'Admin', 'last_seen': null, 'quiet': true},
        ],
        'recent_problems': [
          {'occurred_at': '2026-11-05T07:00:00Z', 'source': 'sms', 'severity': 'warning',
           'message': 'An SMS for node-01 was not delivered.', 'node_id': 'node-01'},
        ],
      });
      expect(s.overall, CheckState.warning);
      expect(s.checks.map((c) => c.title), ['Leaf checker', 'Sensors']);
      expect(s.checks.last.state, CheckState.warning);
      expect(s.sensors.single.quiet, isTrue);
      expect(s.sensors.single.lastSeen, isNull);
      expect(s.problems.single.nodeId, 'node-01');
    });

    test("any other error uses the server's plain sentence", () {
      final e = Api.explain(403, {
        'detail': {'x': 1},
        'error': {'code': 'not_allowed', 'message': 'Only an admin can do that.'},
      }, '/admin/status');
      expect(e.message, 'Only an admin can do that.');
      expect(e.reason, 'not_allowed');
    });

    testWidgets('a warning reads plainly, with an icon', (tester) async {
      await tester.pumpWidget(const MaterialApp(home: Scaffold(body: WarningBox(message: 'Your sensor is quiet.'))));
      expect(find.text('Your sensor is quiet.'), findsOneWidget);
      expect(find.byIcon(Icons.warning_amber_rounded), findsOneWidget);
    });
  });

  group('App updates', () {
    MockClient answering(int status, String body) => MockClient((request) async {
          expect(request.url, versionInfoUrl);
          return http.Response(body, status);
        });

    test('a newer build on GitHub is offered', () async {
      final update = await checkForUpdate(
          client: answering(200, '{"build": 105, "version": "1.0.0", "commit": "abc1234"}'), currentBuild: 104);
      expect(update?.build, 105);
      expect(update?.version, '1.0.0');
    });

    test('the same or an older build is not', () async {
      expect(await checkForUpdate(client: answering(200, '{"build": 104}'), currentBuild: 104), isNull);
      expect(await checkForUpdate(client: answering(200, '{"build": 103}'), currentBuild: 104), isNull);
    });

    test('no answer, or a broken one, just means no banner', () async {
      expect(await checkForUpdate(client: answering(404, 'Not Found'), currentBuild: 104), isNull);
      expect(await checkForUpdate(client: answering(200, '<html>'), currentBuild: 104), isNull);
      expect(await checkForUpdate(client: answering(200, '{"version": "1.0.0"}'), currentBuild: 104), isNull);
      final offline = MockClient((_) async => throw const SocketException('no network'));
      expect(await checkForUpdate(client: offline, currentBuild: 104), isNull);
    });

    test('a build made on the PC never asks', () async {
      final never = MockClient((_) async => fail('a PC build must not check for updates'));
      expect(await checkForUpdate(client: never, currentBuild: 0), isNull);
      expect(appBuild, 0); // the tests are a PC build too
    });

    testWidgets('the strip names the build and can be closed', (tester) async {
      var closed = false;
      await tester.pumpWidget(MaterialApp(home: Scaffold(
          body: UpdateBanner(update: AppUpdate(build: 105, version: '1.0.0'), onClose: () => closed = true))));
      expect(find.text('A new version of the app is ready (build 105).'), findsOneWidget);
      expect(find.text('Update'), findsOneWidget);
      await tester.tap(find.byTooltip('Not now'));
      expect(closed, isTrue);
    });
  });

  group('Errors become plain sentences', () {
    test('a rejected photo keeps the reason, so the app can offer Retake', () {
      final e = Api.explain(422, {'detail': {'reason': 'too_blurry', 'message': 'The photo is blurry.'}}, '/images');
      expect(e.message, 'The photo is blurry.');
      expect(e.reason, 'too_blurry');
    });

    test('a refused status SMS says why', () {
      final e = Api.explain(409, {'detail': {'reason': 'risk_high', 'message': 'The current risk is high, not low.'}}, '/x');
      expect(e.message, 'The current risk is high, not low.');
      expect(e.reason, 'risk_high');
    });

    test('model missing and file too large', () {
      expect(Api.explain(503, {'detail': 'x'}, '/images').message, contains('leaf checker is not available'));
      expect(Api.explain(413, {'detail': 'x'}, '/images').message, contains('too large'));
    });

    test('no network never escapes as a crash', () async {
      final api = Api(
        storage: _FakeStorage(),
        client: MockClient((_) async => throw const SocketException('no route to host')),
      );
      expect(
        () => api.history('NODE-1'),
        throwsA(isA<ApiException>().having((e) => e.message, 'message', contains("Can't reach the server"))),
      );
    });

    test('a broken reply (not JSON) is still a friendly message', () async {
      final api = Api(
        storage: _FakeStorage(),
        client: MockClient((_) async => http.Response('<html>proxy error</html>', 502)),
      );
      expect(
        () => api.dashboard('NODE-1'),
        throwsA(isA<ApiException>().having((e) => e.message, 'message', contains('server had a problem'))),
      );
    });

    test('a good reply is parsed', () async {
      final api = Api(
        storage: _FakeStorage(),
        client: MockClient((_) async => http.Response(jsonEncode({'risk': [], 'diagnoses': [], 'alerts': []}), 200)),
      );
      expect((await api.history('NODE-1')).items, isEmpty);
    });
  });

  group('Sensor picker', () {
    final plot = SensorNode(nodeId: 'NODE-E30CEA4721CE', location: 'Kinangop plot');
    final sim = SensorNode(nodeId: 'wokwi-01', location: 'Wokwi node');
    final demo = SensorNode(nodeId: 'demo-01', location: 'Demo node');

    test('reopens on the sensor the farmer last chose', () {
      expect(pickNode([plot, sim, demo], 'demo-01'), same(demo));
    });

    test('falls back to the first sensor when nothing, or a sensor no longer theirs, was remembered', () {
      expect(pickNode([plot, sim], null), same(plot));
      expect(pickNode([plot, sim], 'node-99'), same(plot));
    });

    test('names a sensor by place, then ID', () {
      expect(nodeLabel(sim), 'Wokwi node (wokwi-01)');
    });
  });

  test('by default the app talks to the hosted server over HTTPS', () {
    // A plain build must reach the Render deployment from any network; a PC
    // address is only ever passed in with --dart-define.
    expect(apiBaseUrl, 'https://blight-ews-api.onrender.com');
  });

    test('the report date can never be in the future', () {
    final today = todayDate();
    final now = DateTime.now();
    expect(today, DateTime(now.year, now.month, now.day));
    expect(today.isAfter(now), isFalse);
  });
}

/// In-memory stand-in for the phone's secure storage, holding a login token.
class _FakeStorage extends FlutterSecureStorage {
  _FakeStorage();

  final Map<String, String> _values = {'access_token': 'test-token'};

  @override
  Future<String?> read({required String key, AppleOptions? iOptions, AndroidOptions? aOptions,
          LinuxOptions? lOptions, WebOptions? webOptions, AppleOptions? mOptions, WindowsOptions? wOptions}) async =>
      _values[key];
}
