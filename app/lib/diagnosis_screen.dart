// Diagnosis result (wireframe Fig 4.9): what the CNN saw, whether late blight
// was flagged once the weather was taken into account, and why.

import 'package:flutter/material.dart';

import 'models.dart';
import 'widgets.dart';

class DiagnosisScreen extends StatelessWidget {
  const DiagnosisScreen({super.key, required this.diagnosis});

  final Diagnosis diagnosis;

  @override
  Widget build(BuildContext context) {
    final d = diagnosis;
    final text = Theme.of(context).textTheme;
    final flagColor = d.flagged ? riskColor(RiskLevel.high) : riskColor(RiskLevel.low);
    return Scaffold(
      appBar: AppBar(title: const Text('Leaf result')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // What the photo looks like, in the CNN's own terms.
          Card(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('The leaf looks like', style: text.bodyMedium),
                  const SizedBox(height: 4),
                  Text(diseaseName(d.label), style: text.headlineMedium),
                  const SizedBox(height: 4),
                  Text('${percent(d.confidence)} confidence', style: text.titleMedium),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          // The decision that matters: late blight flagged or not.
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(color: flagColor, borderRadius: BorderRadius.circular(12)),
            child: Row(
              children: [
                Icon(d.flagged ? Icons.error : Icons.check_circle, color: Colors.white, size: 32),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(d.flagged ? 'Late blight flagged' : 'Late blight not flagged',
                      style: text.titleLarge?.copyWith(color: Colors.white)),
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          Card(
            child: ListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              leading: Icon(riskIcon(d.riskLevel), color: riskColor(d.riskLevel), size: 30),
              title: Text('Weather risk at your plot', style: text.bodyMedium),
              subtitle: Text(riskLabel(d.riskLevel),
                  style: text.titleMedium?.copyWith(color: riskColor(d.riskLevel), fontWeight: FontWeight.bold)),
            ),
          ),
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Icon(Icons.lightbulb_outline, size: 28),
                  const SizedBox(width: 12),
                  Expanded(child: Text(d.summary, style: text.bodyLarge)),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          // The reasoning, one tap away (r and tau in plain words).
          Card(
            clipBehavior: Clip.antiAlias,
            child: ExpansionTile(
              leading: const Icon(Icons.tune),
              title: Text('Confidence adjusted for current field conditions', style: text.bodyLarge),
              subtitle: const Text('Tap to see why'),
              childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
              expandedCrossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(d.reasoning, style: text.bodyLarge),
                if (d.riskNote.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  Text(d.riskNote, style: text.bodySmall),
                ],
              ],
            ),
          ),
          if (d.smsAlert != null) ...[
            const SizedBox(height: 12),
            InfoTile(
              icon: Icons.sms_outlined,
              title: 'SMS',
              body: d.smsAlert == 'dry_run'
                  ? 'An SMS would be sent (test mode: not actually sent).'
                  : 'This result was also sent to your phone by SMS.',
            ),
          ],
          const SizedBox(height: 24),
          FilledButton.icon(
            onPressed: () => Navigator.of(context).pop(),
            icon: const Icon(Icons.camera_alt),
            label: const Text('Scan another leaf'),
          ),
        ],
      ),
    );
  }
}
