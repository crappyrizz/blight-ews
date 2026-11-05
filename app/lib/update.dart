// App updates without the Play Store. Every merge to main builds a signed APK on
// GitHub (.github/workflows/app-release.yml) and publishes it with version.json
// to the release "app-latest". When the app opens it reads version.json; if the
// build there is newer than its own, a banner offers the update. Tapping it opens
// the APK in the browser, and Android asks the farmer to confirm the install.

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:url_launcher/url_launcher.dart';

/// This build's number, set by the release workflow (--dart-define=APP_BUILD).
/// 0 on builds made on the PC (`flutter run`, `flutter build` without it): they never offer an update.
const appBuild = int.fromEnvironment('APP_BUILD');

const _release = 'https://github.com/crappyrizz/blight-ews/releases/download/app-latest';
final versionInfoUrl = Uri.parse('$_release/version.json');
final apkUrl = Uri.parse('$_release/blight-ews.apk');

class AppUpdate {
  AppUpdate({required this.build, required this.version});

  final int build;
  final String version;
}

/// The newer build, or null: up to date, a PC build, or no answer (offline, GitHub
/// unreachable, a broken file). A failed check never shows an error: the farmer
/// simply sees no banner, and the next start tries again.
Future<AppUpdate?> checkForUpdate({http.Client? client, int currentBuild = appBuild}) async {
  if (currentBuild == 0) return null;
  final c = client ?? http.Client();
  try {
    final response = await c.get(versionInfoUrl).timeout(const Duration(seconds: 15));
    if (response.statusCode != 200) return null;
    final j = jsonDecode(response.body);
    if (j is! Map<String, dynamic>) return null;
    final build = (j['build'] as num?)?.toInt();
    if (build == null || build <= currentBuild) return null;
    return AppUpdate(build: build, version: (j['version'] as String?) ?? '');
  } catch (_) {
    return null;
  } finally {
    if (client == null) c.close();
  }
}

/// The strip above the tabs: "A new version of the app is ready" with Update and close.
class UpdateBanner extends StatelessWidget {
  const UpdateBanner({super.key, required this.update, required this.onClose});

  final AppUpdate update;
  final VoidCallback onClose;

  Future<void> _download(BuildContext context) async {
    final opened = await launchUrl(apkUrl, mode: LaunchMode.externalApplication);
    if (!opened && context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Could not open the download. In the browser, go to: $apkUrl')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.primaryContainer,
      child: Padding(
        padding: const EdgeInsets.only(left: 16),
        child: Row(
          children: [
            Icon(Icons.system_update, color: scheme.onPrimaryContainer),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                'A new version of the app is ready (build ${update.build}).',
                style: TextStyle(color: scheme.onPrimaryContainer),
              ),
            ),
            TextButton(onPressed: () => _download(context), child: const Text('Update')),
            IconButton(
              tooltip: 'Not now',
              onPressed: onClose,
              icon: Icon(Icons.close, color: scheme.onPrimaryContainer),
            ),
          ],
        ),
      ),
    );
  }
}
