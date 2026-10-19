// Blight EWS: the farmer's app.
//
// Design rules (the farmer is not a technology user):
// - large, readable text and big buttons;
// - as few steps as possible: log in once, then the dashboard is the app;
// - every problem shown as a plain sentence with a way to retry;
// - the risk is decision support ("consider checking"), never an order.

import 'package:flutter/material.dart';

import 'api.dart';
import 'home_screen.dart';
import 'login_screen.dart';

/// One API client for the whole app.
final Api api = Api();

void main() => runApp(const BlightApp());

class BlightApp extends StatelessWidget {
  const BlightApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Blight EWS',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(),
      home: const StartScreen(),
    );
  }
}

/// Plain Material 3, a little larger than the defaults.
ThemeData buildTheme() {
  final scheme = ColorScheme.fromSeed(seedColor: const Color(0xFF2E7D32)); // potato-leaf green
  final base = ThemeData(colorScheme: scheme, useMaterial3: true);
  return base.copyWith(
    textTheme: base.textTheme.copyWith(
      headlineMedium: base.textTheme.headlineMedium?.copyWith(fontWeight: FontWeight.bold),
      titleLarge: base.textTheme.titleLarge?.copyWith(fontSize: 24, fontWeight: FontWeight.w600),
      titleMedium: base.textTheme.titleMedium?.copyWith(fontSize: 19),
      bodyLarge: base.textTheme.bodyLarge?.copyWith(fontSize: 18, height: 1.35),
      bodyMedium: base.textTheme.bodyMedium?.copyWith(fontSize: 17, height: 1.35),
      labelLarge: base.textTheme.labelLarge?.copyWith(fontSize: 18),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(56)),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(56)),
    ),
    inputDecorationTheme: const InputDecorationTheme(
      border: OutlineInputBorder(),
      contentPadding: EdgeInsets.symmetric(horizontal: 16, vertical: 18),
    ),
  );
}

/// Opens the dashboard if a login is saved on the phone, otherwise the login screen.
class StartScreen extends StatelessWidget {
  const StartScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<bool>(
      future: api.isLoggedIn(),
      builder: (context, snapshot) {
        if (!snapshot.hasData) {
          return const Scaffold(body: Center(child: CircularProgressIndicator()));
        }
        return snapshot.data! ? const HomeScreen() : const LoginScreen();
      },
    );
  }
}
