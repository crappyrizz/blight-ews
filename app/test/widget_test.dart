// Tests for parsing, wording and the login screen. No server needed.

import 'package:blight_ews/api.dart';
import 'package:blight_ews/login_screen.dart';
import 'package:blight_ews/main.dart';
import 'package:blight_ews/models.dart';
import 'package:blight_ews/register_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// The real dashboard response from the backend on 12 Oct 2026 (trimmed).
const Map<String, dynamic> realDashboard = {
  'node_id': 'NODE-E30CEA4721CE',
  'latest_reading': {'timestamp': '2026-10-12T10:10:00+00:00', 'temperature': 18.44, 'humidity': 54.42, 'age_minutes': 179},
  'last_24h': {'readings': 127, 'valid_hours': 22, 'min_temperature': 9.98, 'max_temperature': 18.53, 'humid_hours': 12},
  'hutton_today': {
    'for_date': '2026-10-12', 'status': 'insufficient_data', 'valid_hours': 14,
    'hutton_day': null, 'hutton_period': null, 'label': 'Incomplete data (14 of 24 hours)',
  },
  'forecast': {
    'p24': {'probability': 0.6757, 'risk_level': 'high', 'issued_from': '2026-10-11', 'covers': ['2026-10-12']},
    'p48': {'probability': 0.341, 'risk_level': 'medium', 'issued_from': '2026-10-11', 'covers': ['2026-10-12', '2026-10-13']},
  },
  'risk': {'score': 1.0, 'risk_level': 'high', 'basis': 'hutton_fallback', 'for_date': '2026-10-11',
           'note': 'No complete readings for 2026-10-12. Using the risk from 2026-10-11.'},
  'summary': 'Conditions favour blight over the next 2 days, so consider checking the crop.',
};

void main() {
  group('Dashboard parsing', () {
    test('reads the real backend response', () {
      final d = Dashboard.fromJson(realDashboard);
      expect(d.level, RiskLevel.high);
      expect(d.score, 1.0);
      expect(d.latest!.temperature, 18.44);
      expect(d.latest!.ageMinutes, 179);
      expect(d.huttonToday.explanation, 'Incomplete data (14 of 24 hours)');
      expect(d.outlook24!.level, RiskLevel.high);
      expect(d.outlook48!.probability, closeTo(0.341, 1e-9));
      expect(d.summary, startsWith('Conditions favour blight'));
    });

    test('no recent data is UNKNOWN, never moderate or low', () {
      final d = Dashboard.fromJson({
        'latest_reading': null,
        'hutton_today': {'status': 'not_computed', 'label': 'Not computed yet'},
        'forecast': {'p24': null, 'p48': null},
        'risk': {'score': 0.5, 'risk_level': 'medium', 'basis': 'neutral_prior', 'note': '...'},
        'summary': 'There are no complete sensor readings for the last few days, so the blight risk is unknown.',
      });
      expect(d.level, RiskLevel.unknown);
      expect(d.score, isNull);
      expect(d.latest, isNull);
      expect(d.outlook24, isNull);
      expect(riskLabel(d.level), 'RISK UNKNOWN');
    });

    test('a forecast for days already past is not current', () {
      final d = Dashboard.fromJson(realDashboard);
      expect(d.outlook24!.isCurrent(DateTime(2026, 10, 12)), isTrue); // covers 12 Oct
      expect(d.outlook24!.isCurrent(DateTime(2026, 10, 15)), isFalse); // three days later
      expect(d.outlook48!.isCurrent(DateTime(2026, 10, 13)), isTrue); // covers 12-13 Oct
      expect(shortDate(d.outlook24!.issuedFrom!), '11 Oct');
    });

    test('a judged Hutton period is explained in words', () {
      final h = HuttonToday.fromJson({'status': 'ok', 'label': 'High risk', 'hutton_day': true, 'hutton_period': true});
      expect(h.explanation, 'Blight weather two days running (Hutton period)');
    });
  });

  group('Wording', () {
    test('medium is shown as MODERATE, as in the wireframe', () {
      expect(riskLabel(parseRiskLevel('medium')), 'MODERATE RISK');
    });

    test('age of a reading', () {
      expect(ageText(null), 'no readings yet');
      expect(ageText(1), 'just now');
      expect(ageText(12), '12 min ago');
      expect(ageText(60), '1 hour ago');
      expect(ageText(179), '2 hours ago');
      expect(ageText(60 * 72), '3 days ago');
    });

    test('server validation errors become one plain sentence', () {
      expect(Api.validationMessage([{'loc': ['body', 'password'], 'msg': 'String should have at least 8 characters'}]),
          'The password must be at least 8 characters long.');
      expect(Api.validationMessage([{'loc': ['body', 'phone_number'], 'msg': 'Value error, Phone number must be a Kenyan mobile number'}]),
          'Phone number must be a Kenyan mobile number');
      expect(Api.validationMessage({'reason': 'too_dark', 'message': 'The photo is too dark to judge.'}),
          'The photo is too dark to judge.');
    });

    test('registration is checked on the phone first', () {
      expect(checkRegistration('', '0712345678', 'longenough'), 'Please enter your name.');
      expect(checkRegistration('Wanjiru', '07', 'longenough'), contains('phone number'));
      expect(checkRegistration('Wanjiru', '0712345678', 'short'), contains('at least 8'));
      expect(checkRegistration('Wanjiru', '0712 345 678', 'longenough'), isNull);
    });
  });

  testWidgets('login asks for the details in plain words when left empty', (tester) async {
    await tester.pumpWidget(MaterialApp(theme: buildTheme(), home: const LoginScreen()));

    expect(find.text('Blight Early Warning'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Log in'));
    await tester.pump();

    expect(find.text('Please enter your phone number and password.'), findsOneWidget);
  });
}
