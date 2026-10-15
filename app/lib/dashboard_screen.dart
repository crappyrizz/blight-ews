// The home dashboard (wireframe Fig 4.7): the risk for the farmer's plot at a
// glance, the latest readings, today's Hutton status, the 24 h / 48 h outlook
// and one plain summary sentence. Pull down to refresh.

import 'package:flutter/material.dart';

import 'api.dart';
import 'login_screen.dart';
import 'main.dart';
import 'models.dart';
import 'widgets.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  SensorNode? _node;
  String? _farmId;
  Dashboard? _dashboard;
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
      if (_node == null) {
        final me = await api.me();
        final nodes = await api.nodes();
        if (nodes.isEmpty) {
          setState(() {
            _error = 'No sensor is linked to your account yet. '
                'Please contact your extension officer to set one up.';
            _loading = false;
          });
          return;
        }
        _farmId = me.farmId;
        _node = nodes.first; // one plot per farmer for now
      }
      final dashboard = await api.dashboard(_node!.nodeId);
      setState(() {
        _dashboard = dashboard;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (e.sessionExpired) {
        await _toLogin();
        return;
      }
      setState(() {
        _error = e.message;
        _loading = false;
      });
    }
  }

  Future<void> _toLogin() async {
    await api.logout();
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const LoginScreen()),
      (_) => false,
    );
  }

  Future<void> _confirmLogout() async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Log out?'),
        content: const Text('You will need your phone number and password to log in again.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Log out')),
        ],
      ),
    );
    if (yes == true) await _toLogin();
  }

  void _comingSoon(String what) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$what is coming in the next version.')));
  }

  @override
  Widget build(BuildContext context) {
    final title = _node == null ? 'My plot' : _node!.location + (_farmId == null ? '' : ' ($_farmId)');
    return Scaffold(
      appBar: AppBar(
        title: Text(title),
        actions: [
          IconButton(tooltip: 'Log out', icon: const Icon(Icons.logout), onPressed: _confirmLogout),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          // Always scrollable, so pull-to-refresh works even on a short screen.
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          children: _children(context),
        ),
      ),
    );
  }

  List<Widget> _children(BuildContext context) {
    if (_loading && _dashboard == null) {
      return const [SizedBox(height: 200), Center(child: CircularProgressIndicator())];
    }
    final d = _dashboard;
    return [
      if (_error != null) ...[ErrorBox(message: _error!, onRetry: _load), const SizedBox(height: 16)],
      if (d != null) ...[
        RiskCard(dashboard: d),
        const SizedBox(height: 16),
        SummaryCard(text: d.summary),
        const SizedBox(height: 16),
        ReadingsRow(latest: d.latest),
        const SizedBox(height: 16),
        InfoTile(icon: Icons.thermostat_auto, title: "Today's blight weather", body: d.huttonToday.explanation),
        const SizedBox(height: 16),
        OutlookRow(day1: d.outlook24, day2: d.outlook48),
        const SizedBox(height: 24),
        FilledButton.icon(
          onPressed: () => _comingSoon('Scanning a leaf'),
          icon: const Icon(Icons.camera_alt),
          label: const Text('Scan Leaf'),
        ),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          onPressed: () => _comingSoon('History'),
          icon: const Icon(Icons.history),
          label: const Text('View History'),
        ),
        const SizedBox(height: 8),
        Text('Pull down to refresh.', textAlign: TextAlign.center, style: Theme.of(context).textTheme.bodySmall),
      ],
    ];
  }
}

/// The big card: the risk level in words, colour, icon and a gauge.
class RiskCard extends StatelessWidget {
  const RiskCard({super.key, required this.dashboard});

  final Dashboard dashboard;

  @override
  Widget build(BuildContext context) {
    final level = dashboard.level;
    final color = riskColor(level);
    final text = Theme.of(context).textTheme;
    return Card(
      color: color.withValues(alpha: 0.08),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(color: color.withValues(alpha: 0.4)),
      ),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Row(
          children: [
            RiskGauge(score: dashboard.score, color: color),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                    decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(24)),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(riskIcon(level), color: Colors.white, size: 22),
                        const SizedBox(width: 8),
                        Flexible(
                          child: Text(riskLabel(level),
                              style: text.titleMedium?.copyWith(color: Colors.white, fontWeight: FontWeight.bold)),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 10),
                  Text('Updated ${ageText(dashboard.latest?.ageMinutes)}', style: text.bodyMedium),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// The decision-support sentence from the server, as large text.
class SummaryCard extends StatelessWidget {
  const SummaryCard({super.key, required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Icon(Icons.lightbulb_outline, size: 28),
            const SizedBox(width: 12),
            Expanded(child: Text(text, style: Theme.of(context).textTheme.bodyLarge)),
          ],
        ),
      ),
    );
  }
}

/// Latest temperature and humidity, with how old they are.
class ReadingsRow extends StatelessWidget {
  const ReadingsRow({super.key, required this.latest});

  final LatestReading? latest;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    if (latest == null) {
      return const InfoTile(icon: Icons.sensors_off, title: 'Sensor', body: 'No readings from the sensor yet.');
    }
    Widget value(String big, String label) => Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(big, style: text.headlineMedium),
              Text(label, style: text.bodyMedium),
            ],
          ),
        );
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              value('${latest!.temperature.toStringAsFixed(1)}°C', 'Temperature'),
              value('${latest!.humidity.round()}% RH', 'Humidity'),
            ]),
            const SizedBox(height: 8),
            Text('Sensor reading from ${ageText(latest!.ageMinutes)}', style: text.bodySmall),
          ],
        ),
      ),
    );
  }
}

/// The learned model's outlook: next 24 h and next 48 h.
class OutlookRow extends StatelessWidget {
  const OutlookRow({super.key, required this.day1, required this.day2});

  final Outlook? day1;
  final Outlook? day2;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(child: _OutlookTile(title: 'Next 24 hours', outlook: day1)),
        const SizedBox(width: 12),
        Expanded(child: _OutlookTile(title: 'Next 48 hours', outlook: day2)),
      ],
    );
  }
}

class _OutlookTile extends StatelessWidget {
  const _OutlookTile({required this.title, required this.outlook});

  final String title;
  final Outlook? outlook;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final level = outlook?.level ?? RiskLevel.unknown;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: text.bodyMedium),
            const SizedBox(height: 6),
            if (outlook == null)
              Text('Not available yet', style: text.titleMedium)
            else ...[
              Row(children: [
                Icon(riskIcon(level), color: riskColor(level)),
                const SizedBox(width: 6),
                Text(riskLabel(level).replaceAll(' RISK', ''),
                    style: text.titleMedium?.copyWith(color: riskColor(level), fontWeight: FontWeight.bold)),
              ]),
              Text('${(outlook!.probability * 100).round()}% chance of blight weather', style: text.bodySmall),
            ],
          ],
        ),
      ),
    );
  }
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
