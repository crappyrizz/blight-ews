// Small pieces shared by the screens: the error box, risk colours, the gauge.

import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'login_screen.dart';
import 'main.dart';
import 'models.dart';

/// The session ended (or the farmer logged out): forget the token, go to login.
Future<void> goToLogin(BuildContext context) async {
  await api.logout();
  if (!context.mounted) return;
  Navigator.of(context).pushAndRemoveUntil(
    MaterialPageRoute(builder: (_) => const LoginScreen()),
    (_) => false,
  );
}

/// Risk colours. Each is dark enough for white text (contrast above 4.5:1),
/// and every level also has its own icon and words, so colour is never the
/// only signal (colour-blind farmers, sunlight on the screen).
Color riskColor(RiskLevel level) => switch (level) {
      RiskLevel.low => const Color(0xFF2E7D32), // green
      RiskLevel.medium => const Color(0xFFB45309), // dark orange
      RiskLevel.high => const Color(0xFFC62828), // red
      RiskLevel.unknown => const Color(0xFF616161), // grey
    };

IconData riskIcon(RiskLevel level) => switch (level) {
      RiskLevel.low => Icons.check_circle,
      RiskLevel.medium => Icons.warning_amber_rounded,
      RiskLevel.high => Icons.error,
      RiskLevel.unknown => Icons.help,
    };

/// A problem, in words, with an optional "Try again" button.
class ErrorBox extends StatelessWidget {
  const ErrorBox({super.key, required this.message, this.onRetry});

  final String message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: scheme.errorContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(Icons.info_outline, color: scheme.onErrorContainer),
              const SizedBox(width: 12),
              Expanded(
                child: Text(message,
                    style: Theme.of(context).textTheme.bodyLarge?.copyWith(color: scheme.onErrorContainer)),
              ),
            ],
          ),
          if (onRetry != null) ...[
            const SizedBox(height: 12),
            OutlinedButton.icon(onPressed: onRetry, icon: const Icon(Icons.refresh), label: const Text('Try again')),
          ],
        ],
      ),
    );
  }
}

/// A half-circle gauge for the 0-1 risk score (empty when the risk is unknown).
class RiskGauge extends StatelessWidget {
  const RiskGauge({super.key, required this.score, required this.color, this.size = 96});

  final double? score;
  final Color color;
  final double size;

  @override
  Widget build(BuildContext context) => SizedBox(
        width: size,
        height: size / 2 + 8,
        child: CustomPaint(painter: _GaugePainter(score, color)),
      );
}

class _GaugePainter extends CustomPainter {
  _GaugePainter(this.score, this.color);

  final double? score;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    const stroke = 12.0;
    final rect = Rect.fromLTWH(stroke / 2, stroke / 2, size.width - stroke, size.width - stroke);
    final track = Paint()
      ..color = const Color(0xFFE0E0E0)
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke
      ..strokeCap = StrokeCap.round;
    canvas.drawArc(rect, math.pi, math.pi, false, track);
    if (score != null) {
      final value = Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = stroke
        ..strokeCap = StrokeCap.round;
      canvas.drawArc(rect, math.pi, math.pi * score!.clamp(0.0, 1.0), false, value);
    }
  }

  @override
  bool shouldRepaint(_GaugePainter old) => old.score != score || old.color != color;
}

/// A simple card with an icon, a title and a line of text.
class InfoTile extends StatelessWidget {
  const InfoTile({super.key, required this.icon, required this.title, required this.body});

  final IconData icon;
  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Card(
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        leading: Icon(icon, size: 30),
        title: Text(title, style: text.bodyMedium),
        subtitle: Text(body, style: text.titleMedium),
      ),
    );
  }
}
