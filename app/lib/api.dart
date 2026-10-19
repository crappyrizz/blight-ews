// Talking to the Blight EWS backend (FastAPI).
//
// Every call goes through this file, so there is one place that knows the
// server address, stores the login token, and turns failures into plain
// sentences a farmer can act on (never a stack trace or an HTTP code).
// No call lets an exception escape except ApiException, so a dropped
// network can never crash a screen.

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

import 'models.dart';

/// The backend's address. THE ONE PLACE TO CHANGE IT.
///
/// During development this is the PC's address on the local network (the
/// phone and the PC must be on the same Wi-Fi). Without editing this file it
/// can also be set at run time:
///   flutter run --dart-define=API_BASE_URL=http://192.168.1.77:8000
/// Over USB, `adb reverse tcp:8000 tcp:8000` lets the phone use
///   --dart-define=API_BASE_URL=http://127.0.0.1:8000
const String apiBaseUrl = String.fromEnvironment(
  'API_BASE_URL',
  defaultValue: 'http://192.168.137.38:8000',
);

const Duration _timeout = Duration(seconds: 15);
const Duration _uploadTimeout = Duration(seconds: 90); // photos on a weak network
const String _tokenKey = 'access_token';

/// A problem the app should show to the user, already in plain words.
class ApiException implements Exception {
  ApiException(this.message, {this.sessionExpired = false, this.reason});

  final String message;
  final bool sessionExpired; // true: send the user back to the login screen
  final String? reason; // the backend's code, e.g. "too_blurry" or "risk_high"

  @override
  String toString() => message;
}

class Api {
  Api({FlutterSecureStorage? storage, http.Client? client})
      : _storage = storage ?? const FlutterSecureStorage(),
        _client = client ?? http.Client();

  final FlutterSecureStorage _storage;
  final http.Client _client;

  // --- the login token (kept in Android's encrypted storage) ----------------

  Future<String?> token() => _storage.read(key: _tokenKey);

  Future<bool> isLoggedIn() async => (await token()) != null;

  Future<void> logout() => _storage.delete(key: _tokenKey);

  // --- accounts and the plot ---------------------------------------------------

  Future<void> login(String phone, String password) async {
    final body = await _send('POST', '/auth/login',
        body: {'phone_number': phone, 'password': password}, auth: false);
    await _storage.write(key: _tokenKey, value: body['access_token'] as String);
  }

  /// Create the account, then log straight in (one step less for the farmer).
  Future<void> register(String name, String phone, String password) async {
    await _send('POST', '/auth/register',
        body: {'name': name, 'phone_number': phone, 'password': password}, auth: false);
    await login(phone, password);
  }

  Future<Farmer> me() async => Farmer.fromJson(await _send('GET', '/me'));

  Future<List<SensorNode>> nodes() async {
    final list = await _send('GET', '/nodes') as List<dynamic>;
    return list.map((n) => SensorNode.fromJson(n as Map<String, dynamic>)).toList();
  }

  Future<Dashboard> dashboard(String nodeId) async =>
      Dashboard.fromJson(await _send('GET', '/nodes/$nodeId/dashboard'));

  Future<History> history(String nodeId, {int days = 30}) async =>
      History.fromJson(await _send('GET', '/nodes/$nodeId/history?days=$days'));

  // --- leaf photos ------------------------------------------------------------------

  /// Upload a leaf photo; the server checks it, runs the CNN and fuses the risk.
  /// [onProgress] gets 0.0-1.0 as the bytes leave the phone.
  Future<UploadResult> uploadImage(File photo,
      {required String nodeId, void Function(double)? onProgress}) async {
    final t = await _tokenOrThrow();
    final length = await photo.length();
    var sent = 0;
    final counted = photo.openRead().transform(
      StreamTransformer<List<int>, List<int>>.fromHandlers(handleData: (chunk, sink) {
        sent += chunk.length;
        onProgress?.call(length == 0 ? 1 : sent / length);
        sink.add(chunk);
      }),
    );
    final request = http.MultipartRequest('POST', Uri.parse('$apiBaseUrl/images'))
      ..headers['Authorization'] = 'Bearer $t'
      ..headers['Accept'] = 'application/json'
      ..fields['node_id'] = nodeId
      ..files.add(http.MultipartFile('file', counted, length, filename: photo.uri.pathSegments.last));
    final body = await _guard('/images', () async {
      final streamed = await _client.send(request).timeout(_uploadTimeout);
      return http.Response.fromStream(streamed);
    });
    return UploadResult.fromJson(body as Map<String, dynamic>);
  }

  // --- the farmer's own reports and requests ----------------------------------------

  Future<void> reportSymptom(String nodeId, DateTime onset, String? notes) async {
    final day = '${onset.year.toString().padLeft(4, '0')}-'
        '${onset.month.toString().padLeft(2, '0')}-${onset.day.toString().padLeft(2, '0')}';
    await _send('POST', '/symptom-reports', body: {
      'node_id': nodeId,
      'onset_date': day,
      if (notes != null && notes.trim().isNotEmpty) 'notes': notes.trim(),
    });
  }

  Future<List<SymptomReport>> symptomReports(String nodeId) async {
    final list = await _send('GET', '/symptom-reports?node_id=$nodeId') as List<dynamic>;
    return list.map((r) => SymptomReport.fromJson(r as Map<String, dynamic>)).toList();
  }

  /// Ask for a status SMS. Returns the message that was sent.
  Future<String> statusSms(String nodeId) async {
    final body = await _send('POST', '/nodes/$nodeId/status-sms');
    return (body as Map<String, dynamic>)['message'] as String? ?? '';
  }

  // --- one request, with every failure turned into words ------------------------

  Future<String> _tokenOrThrow() async {
    final t = await token();
    if (t == null) throw ApiException('Please log in again.', sessionExpired: true);
    return t;
  }

  Future<dynamic> _send(String method, String path,
      {Map<String, dynamic>? body, bool auth = true}) async {
    final headers = {'Content-Type': 'application/json', 'Accept': 'application/json'};
    if (auth) headers['Authorization'] = 'Bearer ${await _tokenOrThrow()}';
    final uri = Uri.parse('$apiBaseUrl$path');
    return _guard(path, () {
      final call = method == 'POST'
          ? _client.post(uri, headers: headers, body: jsonEncode(body ?? {}))
          : _client.get(uri, headers: headers);
      return call.timeout(_timeout);
    });
  }

  /// Run one request; return the decoded body or throw ApiException. Nothing else escapes.
  Future<dynamic> _guard(String path, Future<http.Response> Function() call) async {
    http.Response response;
    try {
      response = await call();
    } on TimeoutException {
      throw ApiException('The server is taking too long to answer. Please try again.');
    } on SocketException {
      throw ApiException("Can't reach the server. Check that your phone has internet "
          '(or is on the same Wi-Fi as the server) and try again.');
    } on http.ClientException {
      throw ApiException("Can't reach the server. Please try again.");
    } on ApiException {
      rethrow;
    } catch (_) {
      throw ApiException('Something went wrong while talking to the server. Please try again.');
    }

    final decoded = _decode(response.body);
    if (response.statusCode >= 200 && response.statusCode < 300) {
      return decoded;
    }
    throw explain(response.statusCode, decoded, path);
  }

  static dynamic _decode(String text) {
    if (text.isEmpty) return null;
    try {
      return jsonDecode(text);
    } on FormatException {
      return text;
    }
  }

  /// The backend's error, as a sentence the farmer can act on.
  static ApiException explain(int status, dynamic body, String path) {
    final detail = body is Map ? body['detail'] : null;
    final reason = detail is Map ? detail['reason']?.toString() : null;

    if (status == 401) {
      if (path == '/auth/login') {
        return ApiException('That phone number and password do not match. Please check them and try again.');
      }
      return ApiException('Your session has ended. Please log in again.', sessionExpired: true);
    }
    if (status == 409) {
      // e.g. "That phone number is already registered", or a status-SMS refusal
      if (detail is String) return ApiException(detail);
      if (detail is Map && detail['message'] is String) {
        return ApiException(detail['message'] as String, reason: reason);
      }
    }
    if (status == 422) {
      return ApiException(validationMessage(detail), reason: reason);
    }
    if (status == 413) {
      return ApiException('That photo file is too large. Please take a new photo with the camera.');
    }
    if (status == 503) {
      return ApiException('The leaf checker is not available on the server right now. '
          'Please try again later.');
    }
    if (status == 404) {
      return ApiException('That information was not found. If this keeps happening, '
          'please contact your extension officer.');
    }
    if (status >= 500) {
      return ApiException('The server had a problem. Please try again in a few minutes.');
    }
    if (detail is String) return ApiException(detail);
    return ApiException('Something went wrong (code $status). Please try again.');
  }

  /// FastAPI's 422 detail -> one plain sentence.
  static String validationMessage(dynamic detail) {
    if (detail is String) return detail;
    if (detail is Map && detail['message'] is String) return detail['message'] as String;
    if (detail is List && detail.isNotEmpty && detail.first is Map) {
      final first = detail.first as Map;
      final field = (first['loc'] is List && (first['loc'] as List).isNotEmpty)
          ? (first['loc'] as List).last.toString()
          : '';
      final msg = (first['msg'] ?? '').toString().replaceFirst('Value error, ', '');
      switch (field) {
        case 'password':
          return 'The password must be at least 8 characters long.';
        case 'name':
          return 'Please enter your name (at least 2 letters).';
        case 'phone_number':
          return msg.isNotEmpty ? msg : 'Please enter a valid Kenyan phone number, e.g. 0712345678.';
        case 'onset_date':
          return 'Please choose the day you first saw the spots (not a future day).';
      }
      if (msg.isNotEmpty) return msg;
    }
    return 'Some of the details are not valid. Please check them and try again.';
  }
}
