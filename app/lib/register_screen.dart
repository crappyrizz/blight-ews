// Create an account: name, phone number, password. Then straight to the dashboard.

import 'package:flutter/material.dart';

import 'api.dart';
import 'home_screen.dart';
import 'main.dart';
import 'widgets.dart';

const int minPasswordLength = 8; // the backend's rule (FarmerRegister in schemas.py)

/// Checks done on the phone first, so simple mistakes don't need the server.
/// Returns a plain sentence, or null if the details look fine.
String? checkRegistration(String name, String phone, String password) {
  if (name.trim().length < 2) return 'Please enter your name.';
  final digits = phone.replaceAll(RegExp(r'[\s\-()+]'), '');
  if (digits.length < 9 || int.tryParse(digits) == null) {
    return 'Please enter your phone number, e.g. 0712345678.';
  }
  if (password.length < minPasswordLength) {
    return 'The password must be at least $minPasswordLength characters long.';
  }
  return null;
}

class RegisterScreen extends StatefulWidget {
  const RegisterScreen({super.key});

  @override
  State<RegisterScreen> createState() => _RegisterScreenState();
}

class _RegisterScreenState extends State<RegisterScreen> {
  final _name = TextEditingController();
  final _phone = TextEditingController();
  final _password = TextEditingController();
  bool _busy = false;
  bool _showPassword = false;
  String? _error;

  @override
  void dispose() {
    _name.dispose();
    _phone.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _register() async {
    final problem = checkRegistration(_name.text, _phone.text, _password.text);
    if (problem != null) {
      setState(() => _error = problem);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await api.register(_name.text.trim(), _phone.text.trim(), _password.text);
      if (!mounted) return;
      Navigator.of(context).pushAndRemoveUntil(
        MaterialPageRoute(builder: (_) => const HomeScreen()),
        (_) => false,
      );
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Create an account')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(24),
          children: [
            TextField(
              controller: _name,
              textCapitalization: TextCapitalization.words,
              textInputAction: TextInputAction.next,
              style: text.bodyLarge,
              decoration: const InputDecoration(labelText: 'Your name', prefixIcon: Icon(Icons.person)),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _phone,
              keyboardType: TextInputType.phone,
              textInputAction: TextInputAction.next,
              style: text.bodyLarge,
              decoration: const InputDecoration(
                labelText: 'Phone number',
                hintText: '0712 345 678',
                helperText: 'Alerts will be sent to this number by SMS.',
                prefixIcon: Icon(Icons.phone),
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _password,
              obscureText: !_showPassword,
              textInputAction: TextInputAction.done,
              onSubmitted: (_) => _busy ? null : _register(),
              style: text.bodyLarge,
              decoration: InputDecoration(
                labelText: 'Password',
                helperText: 'At least $minPasswordLength characters.',
                prefixIcon: const Icon(Icons.lock),
                suffixIcon: IconButton(
                  tooltip: _showPassword ? 'Hide password' : 'Show password',
                  icon: Icon(_showPassword ? Icons.visibility_off : Icons.visibility),
                  onPressed: () => setState(() => _showPassword = !_showPassword),
                ),
              ),
            ),
            if (_error != null) ...[
              const SizedBox(height: 16),
              ErrorBox(message: _error!),
            ],
            const SizedBox(height: 24),
            FilledButton(
              onPressed: _busy ? null : _register,
              child: _busy
                  ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 3))
                  : const Text('Create account'),
            ),
          ],
        ),
      ),
    );
  }
}
