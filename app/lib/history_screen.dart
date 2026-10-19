// History (FR viii): the last 30 days, newest first, mixing leaf photos, SMS
// alerts and each day's weather risk, grouped by day.

import 'package:flutter/material.dart';

import 'api.dart';
import 'main.dart';
import 'models.dart';
import 'widgets.dart';

const historyDays = 30;

class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key, required this.node});

  final SensorNode node;

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  History? _history;
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
      final history = await api.history(widget.node.nodeId, days: historyDays);
      if (!mounted) return;
      setState(() {
        _history = history;
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
      appBar: AppBar(title: const Text('History')),
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
    if (_loading && _history == null) {
      return const [SizedBox(height: 200), Center(child: CircularProgressIndicator())];
    }
    final text = Theme.of(context).textTheme;
    final rows = <Widget>[
      if (_error != null) ...[ErrorBox(message: _error!, onRetry: _load), const SizedBox(height: 16)],
    ];
    final items = _history?.items ?? const <HistoryItem>[];
    if (items.isEmpty && _error == null) {
      rows.add(const InfoTile(icon: Icons.inbox_outlined, title: 'History', body: 'Nothing here yet.'));
      return rows;
    }
    String? currentDay;
    for (final item in items) {
      final day = dayHeading(item.when);
      if (day != currentDay) {
        currentDay = day;
        rows.add(Padding(
          padding: const EdgeInsets.only(top: 16, bottom: 4),
          child: Text(day, style: text.titleMedium?.copyWith(fontWeight: FontWeight.bold)),
        ));
      }
      rows.add(_HistoryTile(item: item));
    }
    rows.add(Padding(
      padding: const EdgeInsets.only(top: 16),
      child: Text('The last $historyDays days. Pull down to refresh.',
          textAlign: TextAlign.center, style: text.bodySmall),
    ));
    return rows;
  }
}

class _HistoryTile extends StatelessWidget {
  const _HistoryTile({required this.item});

  final HistoryItem item;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final (IconData icon, Color color) = switch (item.kind) {
      HistoryKind.diagnosis => (
          Icons.eco,
          item.detail == 'Late blight flagged' ? riskColor(RiskLevel.high) : riskColor(RiskLevel.low),
        ),
      HistoryKind.alert => (Icons.sms, Theme.of(context).colorScheme.primary),
      HistoryKind.risk => (riskIcon(item.level ?? RiskLevel.unknown), riskColor(item.level ?? RiskLevel.unknown)),
    };
    return Card(
      child: ListTile(
        leading: Icon(icon, color: color, size: 30),
        title: Text(item.title, style: text.bodyLarge),
        subtitle: Text(item.detail, style: text.bodyMedium),
        trailing: item.kind == HistoryKind.risk ? null : Text(clockTime(item.when), style: text.bodySmall),
      ),
    );
  }
}
