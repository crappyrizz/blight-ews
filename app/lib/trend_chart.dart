// The trend card on the History screen: what the risk model forecast for each
// day, against the blight weather the sensor then measured, over the last 30
// days, plus tomorrow's forecast. Drawn by hand (no chart package): a bar per
// measured day and a dashed forecast line over the low / moderate / high
// bands, and the same story in sentences below.

import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import 'models.dart';
import 'widgets.dart';

const forecastColor = Color(0xFF1565C0); // blue: not one of the risk colours

class TrendCard extends StatelessWidget {
  const TrendCard({super.key, required this.trend});

  final Trend trend;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final measuredColor = Theme.of(context).colorScheme.onSurface;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Trend', style: text.titleMedium?.copyWith(fontWeight: FontWeight.bold)),
            const SizedBox(height: 12),
            // Screen readers get the sentences below instead of the drawing.
            ExcludeSemantics(
              child: SizedBox(
                height: 190,
                width: double.infinity,
                child: CustomPaint(painter: TrendPainter(trend, measuredColor: measuredColor, text: text.bodySmall!)),
              ),
            ),
            const SizedBox(height: 8),
            Wrap(spacing: 16, runSpacing: 4, children: [
              _Key(color: measuredColor, dashed: false, label: 'Blight weather measured'),
              const _Key(color: forecastColor, dashed: true, label: "Model's forecast for that day, made the day before"),
            ]),
            const SizedBox(height: 12),
            Text(trend.summary, style: text.bodyMedium),
          ],
        ),
      ),
    );
  }
}

class _Key extends StatelessWidget {
  const _Key({required this.color, required this.dashed, required this.label});

  final Color color;
  final bool dashed;
  final String label;

  @override
  Widget build(BuildContext context) => Row(mainAxisSize: MainAxisSize.min, children: [
        SizedBox(
          width: 24,
          height: 10,
          child: CustomPaint(painter: _KeyLine(color, dashed)),
        ),
        const SizedBox(width: 6),
        Flexible(child: Text(label, style: Theme.of(context).textTheme.bodySmall)),
      ]);
}

class _KeyLine extends CustomPainter {
  _KeyLine(this.color, this.dashed);

  final Color color;
  final bool dashed;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..strokeWidth = 2.5;
    final y = size.height / 2;
    if (dashed) {
      _dash(canvas, Offset(0, y), Offset(size.width, y), paint);
    } else {
      canvas.drawRect(Rect.fromLTWH(size.width / 2 - 4, 0, 8, size.height), paint..color = color.withValues(alpha: 0.75));
    }
  }

  @override
  bool shouldRepaint(_KeyLine old) => old.color != color || old.dashed != dashed;
}

class TrendPainter extends CustomPainter {
  TrendPainter(this.trend, {required this.measuredColor, required this.text});

  final Trend trend;
  final Color measuredColor;
  final TextStyle text;

  static const _labelHeight = 18.0; // the dates under the chart
  static const _inset = 6.0; // keeps the dots at 0 % and 100 % inside the chart

  @override
  void paint(Canvas canvas, Size size) {
    final chart = Rect.fromLTWH(0, 0, size.width, size.height - _labelHeight);
    final n = trend.points.length;
    final slot = chart.width / n; // each day gets a slot, so the first and last bars sit inside
    double x(int i) => chart.left + slot * (i + 0.5);
    double y(double v) => chart.top + _inset + (chart.height - 2 * _inset) * (1 - v.clamp(0.0, 1.0));

    // The bands, weakly tinted, each named on the left.
    final bands = [
      (RiskLevel.high, trend.highFrom, 1.0, 'High'),
      (RiskLevel.medium, trend.mediumFrom, trend.highFrom, 'Moderate'),
      (RiskLevel.low, 0.0, trend.mediumFrom, 'Low'),
    ];
    for (final (level, from, to, name) in bands) {
      final rect = Rect.fromLTRB(chart.left, to >= 1 ? chart.top : y(to), chart.right, from <= 0 ? chart.bottom : y(from));
      canvas.drawRect(rect, Paint()..color = riskColor(level).withValues(alpha: 0.10));
      _label(canvas, name, Offset(rect.left + 4, rect.top + 2), riskColor(level));
    }

    // Today: a thin line, so tomorrow's forecast reads as the future.
    final today = x(n - 2);
    canvas.drawLine(Offset(today, chart.top), Offset(today, chart.bottom),
        Paint()..color = measuredColor.withValues(alpha: 0.35)..strokeWidth = 1);

    // Measured: a bar per judged day (a stub for a day without blight weather); nothing for a day
    // without full readings, so a gap is shown as a gap.
    final barWidth = slot * 0.55;
    final bar = Paint()..color = measuredColor.withValues(alpha: 0.75);
    for (var i = 0; i < n; i++) {
      final v = trend.points[i].measured;
      if (v == null) continue;
      final top = v > 0 ? y(v) : chart.bottom - 3;
      canvas.drawRect(Rect.fromLTRB(x(i) - barWidth / 2, top, x(i) + barWidth / 2, chart.bottom), bar);
    }

    // Forecast: a dashed line, broken where a day has no forecast.
    final line = Paint()
      ..color = forecastColor
      ..strokeWidth = 2.5
      ..strokeCap = StrokeCap.round;
    for (var i = 1; i < n; i++) {
      final v0 = trend.points[i - 1].forecast, v1 = trend.points[i].forecast;
      if (v0 != null && v1 != null) _dash(canvas, Offset(x(i - 1), y(v0)), Offset(x(i), y(v1)), line);
    }
    final dot = Paint()..color = forecastColor;
    for (var i = 0; i < n; i++) {
      final v = trend.points[i].forecast;
      if (v != null) canvas.drawCircle(Offset(x(i), y(v)), i == n - 1 ? 4.5 : 2.5, dot); // tomorrow: bigger
    }

    // Dates: the first day on the left, "Today" ending at the today line (tomorrow is the point after it).
    final labelTop = chart.bottom + 3;
    _label(canvas, shortDate(trend.points.first.day), Offset(chart.left, labelTop), measuredColor);
    _label(canvas, 'Today', Offset(today, labelTop), measuredColor, alignRight: true);
  }

  void _label(Canvas canvas, String s, Offset at, Color color, {bool alignRight = false}) {
    final painter = TextPainter(
      text: TextSpan(text: s, style: text.copyWith(color: color, fontSize: 11)),
      textDirection: ui.TextDirection.ltr,
    )..layout();
    painter.paint(canvas, Offset(alignRight ? at.dx - painter.width : at.dx, at.dy));
  }

  @override
  bool shouldRepaint(TrendPainter old) =>
      old.trend != trend || old.measuredColor != measuredColor || old.text != text;
}

/// A dashed straight line from a to b.
void _dash(Canvas canvas, Offset a, Offset b, Paint paint, {double on = 5, double off = 4}) {
  final length = (b - a).distance;
  if (length == 0) return;
  final step = (b - a) / length;
  for (var d = 0.0; d < length; d += on + off) {
    final end = (d + on).clamp(0.0, length);
    canvas.drawLine(a + step * d, a + step * end, paint);
  }
}
