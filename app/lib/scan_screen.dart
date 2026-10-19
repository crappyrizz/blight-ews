// Leaf capture (wireframe Fig 4.8): take or choose a photo, upload it, and
// either show the result or the server's reason to retake it.

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

import 'api.dart';
import 'diagnosis_screen.dart';
import 'main.dart';
import 'models.dart';
import 'widgets.dart';

/// The server's quality-check reasons that mean "take the photo again".
const retakeReasons = {'unreadable', 'too_small', 'too_dark', 'too_blurry'};

class ScanScreen extends StatefulWidget {
  const ScanScreen({super.key, required this.node, required this.onDiagnosed});

  final SensorNode node;
  final VoidCallback onDiagnosed;

  @override
  State<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<ScanScreen> {
  final _picker = ImagePicker();
  File? _photo;
  double? _progress; // null = not uploading
  String? _error;
  bool _retake = false; // the error says the photo itself needs retaking

  Future<void> _pick(ImageSource source) async {
    XFile? picked;
    try {
      // Shrunk on the phone: quicker over a weak network, still far above
      // the server's 224-pixel minimum.
      picked = await _picker.pickImage(source: source, maxWidth: 1600, maxHeight: 1600, imageQuality: 88);
    } catch (_) {
      setState(() => _error = source == ImageSource.camera
          ? 'The camera could not be opened. Please allow camera access for this app, or choose a photo from the gallery.'
          : 'The gallery could not be opened. Please try again.');
      return;
    }
    if (picked == null) return; // the farmer cancelled
    setState(() {
      _photo = File(picked!.path);
      _error = null;
      _retake = false;
    });
    await _upload();
  }

  Future<void> _upload() async {
    final photo = _photo;
    if (photo == null) return;
    setState(() {
      _progress = 0;
      _error = null;
    });
    try {
      final result = await api.uploadImage(photo, nodeId: widget.node.nodeId,
          onProgress: (p) => mounted ? setState(() => _progress = p) : null);
      if (!mounted) return;
      setState(() => _progress = null);
      widget.onDiagnosed();
      await Navigator.of(context).push(
        MaterialPageRoute(builder: (_) => DiagnosisScreen(diagnosis: result.diagnosis)),
      );
      if (mounted) setState(() => _photo = null); // ready for the next leaf
    } on ApiException catch (e) {
      if (e.sessionExpired) {
        if (mounted) await goToLogin(context);
        return;
      }
      if (!mounted) return;
      setState(() {
        _progress = null;
        _error = e.message;
        _retake = retakeReasons.contains(e.reason);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final uploading = _progress != null;
    return Scaffold(
      appBar: AppBar(title: const Text('Scan a leaf')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: ListTile(
              contentPadding: const EdgeInsets.all(16),
              leading: const Icon(Icons.tips_and_updates_outlined, size: 32),
              title: Text('For a good photo', style: text.titleMedium),
              subtitle: Text('One leaf, good light, fill the frame.', style: text.bodyLarge),
            ),
          ),
          const SizedBox(height: 16),
          if (_photo != null)
            ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: Image.file(_photo!, height: 260, fit: BoxFit.cover),
            )
          else
            Container(
              height: 200,
              decoration: BoxDecoration(
                border: Border.all(color: Theme.of(context).colorScheme.outline),
                borderRadius: BorderRadius.circular(12),
              ),
              child: const Center(child: Icon(Icons.eco_outlined, size: 72)),
            ),
          const SizedBox(height: 16),
          if (uploading) ...[
            LinearProgressIndicator(value: _progress! < 1 ? _progress : null, minHeight: 8),
            const SizedBox(height: 8),
            Text(_progress! < 1 ? 'Uploading the photo... ${percent(_progress!)}' : 'Checking the leaf...',
                textAlign: TextAlign.center, style: text.bodyLarge),
          ] else ...[
            if (_error != null) ...[
              ErrorBox(message: _error!),
              const SizedBox(height: 12),
              if (_retake)
                FilledButton.icon(
                  onPressed: () => _pick(ImageSource.camera),
                  icon: const Icon(Icons.camera_alt),
                  label: const Text('Retake photo'),
                )
              else if (_photo != null)
                FilledButton.icon(onPressed: _upload, icon: const Icon(Icons.refresh), label: const Text('Try again')),
              const SizedBox(height: 12),
            ],
            if (!_retake || _error == null) ...[
              FilledButton.icon(
                onPressed: () => _pick(ImageSource.camera),
                icon: const Icon(Icons.camera_alt),
                label: const Text('Take a photo'),
              ),
              const SizedBox(height: 12),
            ],
            OutlinedButton.icon(
              onPressed: () => _pick(ImageSource.gallery),
              icon: const Icon(Icons.photo_library_outlined),
              label: const Text('Choose from gallery'),
            ),
          ],
        ],
      ),
    );
  }
}
