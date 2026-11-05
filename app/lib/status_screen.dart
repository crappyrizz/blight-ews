// System status (admins only, GET /admin/status): is everything working? Each
// part of the system with ok / warning / error and a sentence, every sensor's
// last contact, and the problems recorded in the last 7 days. Farmers never
// see this tab; they get only the warnings that affect them, on Home.

import 'package:flutter/material.dart';

import 'api.dart';
import 'main.dart';
import 'models.dart';
import 'widgets.dart';

Color checkColor(CheckState s) => switch (s) {
      CheckState.ok => riskColor(RiskLevel.low),
      CheckState.warning => riskColor(RiskLevel.medium),
      CheckState.error => riskColor(RiskLevel.high),
    };

IconData checkIcon(CheckState s) => switch (s) {
      CheckState.ok => Icons.check_circle,
      CheckState.warning => Icons.warning_amber_rounded,
      CheckState.error => Icons.error,
    };

/// "2 Nov 14:05", or "never".
String stamp(DateTime? d) => d == null ? 'never' : '${shortDate(d)} ${clockTime(d)}';

class StatusScreen extends StatefulWidget {
  const StatusScreen({super.key});

  @override
  State<StatusScreen> createState() => _StatusScreenState();
}

class _StatusScreenState extends State<StatusScreen> {
  SystemStatus? _status;
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final status = await api.systemStatus();
      if (!mounted) return;
      setState(() {
        _status = status;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (e.sessionExpired) {
        if (mounted) await goToLogin(context);
        return;
      }
      if (!mounted) return;
      setState(() {
        _error = e.message;
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('System status')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: _children(context),
        ),
      ),
    );
  }

  List<Widget> _children(BuildContext context) {
    if (_loading && _status == null) {
      return const [SizedBox(height: 200), Center(child: CircularProgressIndicator())];
    }
    final text = Theme.of(context).textTheme;
    final s = _status;
    return [
      if (_error != null) ...[ErrorBox(message: _error!, onRetry: _load), const SizedBox(height: 16)],
      if (s != null) ...[
        Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(color: checkColor(s.overall), borderRadius: BorderRadius.circular(12)),
          child: Row(children: [
            Icon(checkIcon(s.overall), color: Colors.white, size: 32),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                switch (s.overall) {
                  CheckState.ok => 'Everything is working',
                  CheckState.warning => 'Working, with warnings',
                  CheckState.error => 'Something is not working',
                },
                style: text.titleLarge?.copyWith(color: Colors.white),
              ),
            ),
          ]),
        ),
        const SizedBox(height: 12),
        for (final c in s.checks)
          Card(
            child: ListTile(
              leading: Icon(checkIcon(c.state), color: checkColor(c.state), size: 28),
              title: Text(c.title, style: text.titleMedium),
              subtitle: Text(c.message, style: text.bodyMedium),
            ),
          ),
        _heading(context, 'Sensors'),
        for (final n in s.sensors)
          Card(
            child: ListTile(
              leading: Icon(n.quiet ? Icons.sensors_off : Icons.sensors,
                  color: n.quiet ? riskColor(RiskLevel.medium) : riskColor(RiskLevel.low)),
              title: Text('${n.nodeId}: ${n.location}', style: text.bodyLarge),
              subtitle: Text('${n.owner}. Last contact: ${stamp(n.lastSeen)}${n.quiet ? ' (quiet)' : ''}'),
            ),
          ),
        _heading(context, 'Problems in the last 7 days'),
        if (s.problems.isEmpty)
          const InfoTile(icon: Icons.thumb_up_alt_outlined, title: 'Problems', body: 'None recorded.')
        else
          for (final p in s.problems)
            Card(
              child: ListTile(
                leading: Icon(checkIcon(p.severity), color: checkColor(p.severity)),
                title: Text(p.message, style: text.bodyMedium),
                subtitle: Text('${stamp(p.when)} · ${p.source}${p.nodeId == null ? '' : ' · ${p.nodeId}'}'),
              ),
            ),
        const SizedBox(height: 8),
        Text('Pull down to refresh.', textAlign: TextAlign.center, style: text.bodySmall),
      ],
    ];
  }

  Widget _heading(BuildContext context, String title) => Padding(
        padding: const EdgeInsets.only(top: 16, bottom: 4),
        child: Text(title, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold)),
      );
}
