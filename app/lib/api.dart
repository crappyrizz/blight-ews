// Talking to the Blight EWS backend (FastAPI).
//
// Every call goes through this file, so there is one place that knows the
// server address, stores the login token, and turns failures into plain
// sentences a farmer can act on (never a stack trace or an HTTP code).

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
const String apiBaseUrl = String.fromEnvironment(
  'API_BASE_URL',
  defaultValue: 'http://192.168.137.38:8000',
);

const Duration _timeout = Duration(seconds: 15);
const String _tokenKey = 'access_token';

/// A problem the app should show to the user, already in plain words.
class ApiException implements Exception {
  ApiException(this.message, {this.sessionExpired = false});

  final String message;
  final bool sessionExpired; // true: send the user back to the login screen

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

  // --- accounts ---------------------------------------------------------------

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

  // --- one request, with every failure turned into words ------------------------

  Future<dynamic> _send(String method, String path,
      {Map<String, dynamic>? body, bool auth = true}) async {
    final headers = {'Content-Type': 'application/json', 'Accept': 'application/json'};
    if (auth) {
      final t = await token();
      if (t == null) {
        throw ApiException('Please log in again.', sessionExpired: true);
      }
      headers['Authorization'] = 'Bearer $t';
    }
    final uri = Uri.parse('$apiBaseUrl$path');

    http.Response response;
    try {
      response = await (method == 'POST'
              ? _client.post(uri, headers: headers, body: jsonEncode(body ?? {}))
              : _client.get(uri, headers: headers))
          .timeout(_timeout);
    } on TimeoutException {
      throw ApiException('The server is taking too long to answer. Please try again.');
    } on SocketException {
      throw ApiException("Can't reach the server. Check that your phone has internet "
          '(or is on the same Wi-Fi as the server) and try again.');
    } on http.ClientException {
      throw ApiException("Can't reach the server. Please try again.");
    }

    final decoded = _decode(response.body);
    if (response.statusCode >= 200 && response.statusCode < 300) {
      return decoded;
    }
    throw _explain(response.statusCode, decoded, path);
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
  static ApiException _explain(int status, dynamic body, String path) {
    final detail = body is Map ? body['detail'] : null;

    if (status == 401) {
      if (path == '/auth/login') {
        return ApiException('That phone number and password do not match. Please check them and try again.');
      }
      return ApiException('Your session has ended. Please log in again.', sessionExpired: true);
    }
    if (status == 409 && detail is String) {
      return ApiException(detail); // e.g. "That phone number is already registered"
    }
    if (status == 422) {
      return ApiException(validationMessage(detail));
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
      }
      if (msg.isNotEmpty) return msg;
    }
    return 'Some of the details are not valid. Please check them and try again.';
  }
}
