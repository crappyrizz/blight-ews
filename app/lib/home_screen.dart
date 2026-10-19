// The app after login: loads the farmer's plot once, then four tabs at the
// bottom: Home, Scan, History, Report.

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
      if (!mounted) return;
      if (nodes.isEmpty) {
        setState(() => _error = 'No sensor is linked to your account yet. '
            'Please contact your extension officer to set one up.');
        return;
      }
      setState(() {
        _farmId = me.farmId;
        _node = nodes.first; // one plot per farmer for now
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
              : ListView(children: [
                  ErrorBox(message: _error!, onRetry: _loadPlot),
                  const SizedBox(height: 24),
                  OutlinedButton.icon(
                    onPressed: () => goToLogin(context),
                    icon: const Icon(Icons.logout),
                    label: const Text('Log out'),
                  ),
                ]),
        ),
      );
    }

    return Scaffold(
      // IndexedStack keeps each tab's state (e.g. a half-written report) when switching.
      body: IndexedStack(
        index: _tab,
        children: [
          DashboardScreen(node: node, farmId: _farmId, onScan: () => _go(scanTab), onHistory: () => _go(historyTab)),
          ScanScreen(node: node, onDiagnosed: _somethingNew),
          HistoryScreen(key: ValueKey(_historyVersion), node: node),
          ReportScreen(node: node, onReported: _somethingNew),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: _go,
        destinations: const [
          NavigationDestination(icon: Icon(Icons.home_outlined), selectedIcon: Icon(Icons.home), label: 'Home'),
          NavigationDestination(icon: Icon(Icons.camera_alt_outlined), selectedIcon: Icon(Icons.camera_alt), label: 'Scan'),
          NavigationDestination(icon: Icon(Icons.history), label: 'History'),
          NavigationDestination(icon: Icon(Icons.flag_outlined), selectedIcon: Icon(Icons.flag), label: 'Report'),
        ],
      ),
    );
  }
}
