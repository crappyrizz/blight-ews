// The app after login: loads the farmer's sensors once, then four tabs at the
// bottom: Home, Scan, History, Report. A farmer with more than one sensor (e.g.
// the plot node plus a simulated or demo node) gets a picker above the tabs;
// every tab then shows, and sends photos and reports for, the chosen sensor.

import 'package:flutter/material.dart';

import 'api.dart';
import 'dashboard_screen.dart';
import 'history_screen.dart';
import 'main.dart';
import 'models.dart';
import 'report_screen.dart';
import 'scan_screen.dart';
import 'widgets.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  static const homeTab = 0, scanTab = 1, historyTab = 2; // Report is tab 3

  int _tab = homeTab;
  List<SensorNode> _nodes = const [];
  SensorNode? _node;
  String? _farmId;
  String? _error;
  int _historyVersion = 0; // bumped after a new diagnosis or report, to refresh History

  @override
  void initState() {
    super.initState();
    _loadPlot();
  }

  Future<void> _loadPlot() async {
    setState(() => _error = null);
    try {
      final me = await api.me();
      final nodes = await api.nodes();
      final remembered = await api.rememberedNodeId();
      if (!mounted) return;
      if (nodes.isEmpty) {
        setState(
          () => _error =
              'No sensor is linked to your account yet. '
              'Please contact your extension officer to set one up.',
        );
        return;
      }
      setState(() {
        _farmId = me.farmId;
        _nodes = nodes;
        _node = pickNode(nodes, remembered);
      });
    } on ApiException catch (e) {
      if (e.sessionExpired) {
        if (mounted) await goToLogin(context);
        return;
      }
      if (mounted) setState(() => _error = e.message);
    }
  }

  void _go(int tab) => setState(() => _tab = tab);

  void _select(String? nodeId) {
    final chosen = _nodes.where((n) => n.nodeId == nodeId);
    if (chosen.isEmpty || chosen.first.nodeId == _node?.nodeId) return;
    setState(() => _node = chosen.first);
    api.rememberNode(chosen.first.nodeId);
  }

  /// "Sensor: Kinangop plot (NODE-...)" above the tabs; only when there is a choice to make.
  Widget? _nodePicker(SensorNode node) {
    if (_nodes.length < 2) return null;
    return Material(
      color: Theme.of(context).colorScheme.surfaceContainerHigh,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Row(
          children: [
            const Icon(Icons.sensors),
            const SizedBox(width: 12),
            const Text('Sensor:'),
            const SizedBox(width: 8),
            Expanded(
              child: DropdownButton<String>(
                key: const ValueKey('node-picker'),
                isExpanded: true,
                value: node.nodeId,
                underline: const SizedBox.shrink(),
                items: [
                  for (final n in _nodes)
                    DropdownMenuItem(
                      value: n.nodeId,
                      child: Text(nodeLabel(n), overflow: TextOverflow.ellipsis),
                    ),
                ],
                onChanged: _select,
              ),
            ),
          ],
        ),
      ),
    );
  }

  void _somethingNew() => setState(() => _historyVersion++);

  @override
  Widget build(BuildContext context) {
    final node = _node;
    if (node == null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Blight EWS')),
        body: Padding(
          padding: const EdgeInsets.all(16),
          child: _error == null
              ? const Center(child: CircularProgressIndicator())
              : ListView(
                  children: [
                    ErrorBox(message: _error!, onRetry: _loadPlot),
                    const SizedBox(height: 24),
                    OutlinedButton.icon(
                      onPressed: () => goToLogin(context),
                      icon: const Icon(Icons.logout),
                      label: const Text('Log out'),
                    ),
                  ],
                ),
        ),
      );
    }

    final picker = _nodePicker(node);
    final id = node.nodeId; // keys: switching sensor rebuilds every tab for the new one
    return Scaffold(
      // IndexedStack keeps each tab's state (e.g. a half-written report) when switching.
      body: IndexedStack(
        index: _tab,
        children: [
          DashboardScreen(
            key: ValueKey('home-$id'),
            node: node,
            farmId: _farmId,
            onScan: () => _go(scanTab),
            onHistory: () => _go(historyTab),
          ),
          ScanScreen(key: ValueKey('scan-$id'), node: node, onDiagnosed: _somethingNew),
          HistoryScreen(key: ValueKey('history-$id-$_historyVersion'), node: node),
          ReportScreen(key: ValueKey('report-$id'), node: node, onReported: _somethingNew),
        ],
      ),
      bottomNavigationBar: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          ?picker,
          NavigationBar(
            selectedIndex: _tab,
            onDestinationSelected: _go,
            destinations: const [
              NavigationDestination(icon: Icon(Icons.home_outlined), selectedIcon: Icon(Icons.home), label: 'Home'),
              NavigationDestination(
                icon: Icon(Icons.camera_alt_outlined),
                selectedIcon: Icon(Icons.camera_alt),
                label: 'Scan',
              ),
              NavigationDestination(icon: Icon(Icons.history), label: 'History'),
              NavigationDestination(icon: Icon(Icons.flag_outlined), selectedIcon: Icon(Icons.flag), label: 'Report'),
            ],
          ),
        ],
      ),
    );
  }
}
