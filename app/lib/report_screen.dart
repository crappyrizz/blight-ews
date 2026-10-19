// Report symptom onset: the day the farmer first saw blight spots.
//
// This is ground-truth data: it is how the project checks whether the
// warnings came early enough. So it must be quick: a date (today unless
// changed), an optional note, one button.

import 'package:flutter/material.dart';

import 'api.dart';
import 'main.dart';
import 'models.dart';
import 'widgets.dart';

const reportLookbackDays = 90; // how far back the date picker goes

/// The newest day the farmer may choose: today, never a future day.
DateTime todayDate() {
  final now = DateTime.now();
  return DateTime(now.year, now.month, now.day);
}

class ReportScreen extends StatefulWidget {
  const ReportScreen({super.key, required this.node, required this.onReported});

  final SensorNode node;
  final VoidCallback onReported;

  @override
  State<ReportScreen> createState() => _ReportScreenState();
}

class _ReportScreenState extends State<ReportScreen> {
  final _note = TextEditingController();
  DateTime _onset = todayDate();
  bool _sending = false;
  String? _error;
  String? _done;
  List<SymptomReport> _earlier = const [];

  @override
  void initState() {
    super.initState();
    _loadEarlier();
  }

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  Future<void> _loadEarlier() async {
    try {
      final reports = await api.symptomReports(widget.node.nodeId);
      if (mounted) setState(() => _earlier = reports);
    } on ApiException {
      // Not essential: the form still works without the list.
    }
  }

  Future<void> _chooseDate() async {
    final today = todayDate();
    final picked = await showDatePicker(
      context: context,
      initialDate: _onset,
      firstDate: today.subtract(const Duration(days: reportLookbackDays)),
      lastDate: today, // no future days
      helpText: 'Day you first saw the spots',
    );
    if (picked != null) setState(() => _onset = picked);
  }

  Future<void> _send() async {
    if (_onset.isAfter(todayDate())) {
      setState(() => _error = 'Please choose today or an earlier day.');
      return;
    }
    setState(() {
      _sending = true;
      _error = null;
      _done = null;
    });
    try {
      await api.reportSymptom(widget.node.nodeId, _onset, _note.text);
      if (!mounted) return;
      setState(() {
        _done = 'Thank you. Your report for ${dayHeading(_onset)} has been saved.';
        _note.clear();
        _onset = todayDate();
      });
      widget.onReported();
      await _loadEarlier();
    } on ApiException catch (e) {
      if (e.sessionExpired) {
        if (mounted) await goToLogin(context);
        return;
      }
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Report blight spots')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('Tell us the day you first saw blight spots', style: text.titleLarge),
          const SizedBox(height: 8),
          Text('This helps check whether the warnings come early enough.', style: text.bodyLarge),
          const SizedBox(height: 20),
          Card(
            child: ListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              leading: const Icon(Icons.calendar_month, size: 32),
              title: Text('Day you first saw the spots', style: text.bodyMedium),
              subtitle: Text(_onset == todayDate() ? 'Today (${dayHeading(_onset)})' : dayHeading(_onset),
                  style: text.titleMedium),
              trailing: TextButton(onPressed: _sending ? null : _chooseDate, child: const Text('Change')),
              onTap: _sending ? null : _chooseDate,
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _note,
            maxLines: 3,
            maxLength: 1000,
            style: text.bodyLarge,
            decoration: const InputDecoration(
              labelText: 'Note (optional)',
              hintText: 'e.g. dark patches on the lower leaves',
            ),
          ),
          if (_error != null) ...[const SizedBox(height: 8), ErrorBox(message: _error!)],
          if (_done != null) ...[
            const SizedBox(height: 8),
            InfoTile(icon: Icons.check_circle, title: 'Saved', body: _done!),
          ],
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: _sending ? null : _send,
            icon: _sending
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.send),
            label: const Text('Send report'),
          ),
          if (_earlier.isNotEmpty) ...[
            const SizedBox(height: 28),
            Text('Your earlier reports', style: text.titleMedium?.copyWith(fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            for (final r in _earlier)
              Card(
                child: ListTile(
                  leading: const Icon(Icons.flag),
                  title: Text(dayHeading(r.onsetDate), style: text.bodyLarge),
                  subtitle: r.notes == null ? null : Text(r.notes!, style: text.bodyMedium),
                ),
              ),
          ],
        ],
      ),
    );
  }
}
