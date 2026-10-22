// Tests for the diagnosis, history and error handling behind the new screens.
// No server: responses are hand-made in the backend's exact shape, and the
// network is a fake client.

import 'dart:convert';
import 'dart:io';

import 'package:blight_ews/api.dart';
import 'package:blight_ews/models.dart';
import 'package:blight_ews/report_screen.dart';
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
