// Leaf check (wireframe Fig 4.8), guided like an identity check: up to three
// photos of the SAME leaf, each with its own prompt, sent together and judged
// as one result (backend/scan.py). One photo is enough to start; three give the
// most reliable result. A photo the server rejects is marked in its own slot
// with the reason, so only that one is retaken.

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

import 'api.dart';
import 'diagnosis_screen.dart';
import 'main.dart';
import 'models.dart';
import 'widgets.dart';

/// The three photos asked for, in order. The model only knows the top side of
/// leaves, so the underside is a tip for the farmer, not a scored step.
const scanSteps = [
  ('The whole leaf', 'Top side, filling the frame, in good light.'),
  ('Closer: the worst spot', 'Hold the phone close to the spots, still and in focus.'),
  ('The same leaf, another way', 'From another angle, or in other light without glare.'),
];

class ScanScreen extends StatefulWidget {
  const ScanScreen({super.key, required this.node, required this.onDiagnosed});

  final SensorNode node;
  final VoidCallback onDiagnosed;

  @override
  State<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<ScanScreen> {
  final _picker = ImagePicker();
  final List<File?> _photos = List.filled(scanSteps.length, null);
  Map<int, String> _problems = {}; // slot -> why the server wants it retaken
  double? _progress; // null = not uploading
  String? _error;

  int get _count => _photos.whereType<File>().length;

  Future<void> _pick(int slot, ImageSource source) async {
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
      _photos[slot] = File(picked!.path);
      _problems = {..._problems}..remove(slot);
      _error = null;
    });
  }

  void _remove(int slot) => setState(() {
        _photos[slot] = null;
        _problems = {..._problems}..remove(slot);
      });

  Future<void> _check() async {
    // The slots that hold a photo, in order: the server's photo index i is slots[i].
    final slots = [for (var i = 0; i < _photos.length; i++) if (_photos[i] != null) i];
    if (slots.isEmpty) return;
    setState(() {
      _progress = 0;
      _error = null;
      _problems = {};
    });
    try {
      final result = await api.uploadScan([for (final i in slots) _photos[i]!], nodeId: widget.node.nodeId,
          onProgress: (p) => mounted ? setState(() => _progress = p) : null);
      if (!mounted) return;
      setState(() => _progress = null);
      widget.onDiagnosed();
      await Navigator.of(context).push(
        MaterialPageRoute(builder: (_) => DiagnosisScreen(diagnosis: result.diagnosis)),
      );
      if (mounted) setState(() => _photos.fillRange(0, _photos.length, null)); // ready for the next leaf
    } on ApiException catch (e) {
      if (e.sessionExpired) {
        if (mounted) await goToLogin(context);
        return;
      }
      if (!mounted) return;
      setState(() {
        _progress = null;
        _problems = {
          for (final entry in e.rejectedPhotos.entries)
            if (entry.key < slots.length) slots[entry.key]: entry.value,
        };
        _error = _problems.isEmpty
            ? e.message
            : (_problems.length == 1
                ? 'One photo needs to be taken again: see the marked photo below.'
                : 'Some photos need to be taken again: see the marked photos below.');
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final uploading = _progress != null;
    return Scaffold(
      appBar: AppBar(title: const Text('Check a leaf')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: ListTile(
              contentPadding: const EdgeInsets.all(16),
              leading: const Icon(Icons.tips_and_updates_outlined, size: 32),
              title: Text('Up to 3 photos of the same leaf', style: text.titleMedium),
              subtitle: Text('One photo is enough to start; three give the most reliable result. '
                  'Keep the leaf in the middle of each photo.\n\n'
                  'Also turn the leaf over: white fuzz underneath is a strong sign of late blight.',
                  style: text.bodyLarge),
            ),
          ),
          const SizedBox(height: 8),
          for (var i = 0; i < scanSteps.length; i++)
            _PhotoSlot(
              number: i + 1,
              title: scanSteps[i].$1,
              hint: scanSteps[i].$2,
              photo: _photos[i],
              problem: _problems[i],
              enabled: !uploading,
              onCamera: () => _pick(i, ImageSource.camera),
              onGallery: () => _pick(i, ImageSource.gallery),
              onRemove: () => _remove(i),
            ),
          const SizedBox(height: 16),
          if (uploading) ...[
            LinearProgressIndicator(value: _progress! < 1 ? _progress : null, minHeight: 8),
            const SizedBox(height: 8),
            Text(
                _progress! < 1
                    ? 'Uploading ${_count == 1 ? 'the photo' : 'the photos'}... ${percent(_progress!)}'
                    : 'Checking the leaf...',
                textAlign: TextAlign.center,
                style: text.bodyLarge),
          ] else ...[
            if (_error != null) ...[ErrorBox(message: _error!), const SizedBox(height: 12)],
            FilledButton.icon(
              onPressed: _count == 0 || _problems.isNotEmpty ? null : _check,
              icon: const Icon(Icons.eco),
              label: Text(_count == 0
                  ? 'Take at least one photo'
                  : 'Check this leaf ($_count ${_count == 1 ? 'photo' : 'photos'})'),
            ),
            if (_count > 0 && _count < scanSteps.length) ...[
              const SizedBox(height: 8),
              Text('More photos give a more reliable result.', textAlign: TextAlign.center, style: text.bodySmall),
            ],
          ],
        ],
      ),
    );
  }
}

class _PhotoSlot extends StatelessWidget {
  const _PhotoSlot({
    required this.number,
    required this.title,
    required this.hint,
    required this.photo,
    required this.problem,
    required this.enabled,
    required this.onCamera,
    required this.onGallery,
    required this.onRemove,
  });

  final int number;
  final String title;
  final String hint;
  final File? photo;
  final String? problem; // the server's reason to retake this photo
  final bool enabled;
  final VoidCallback onCamera;
  final VoidCallback onGallery;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final scheme = Theme.of(context).colorScheme;
    final bad = problem != null;
    return Card(
      shape: bad
          ? RoundedRectangleBorder(
              side: BorderSide(color: scheme.error, width: 2), borderRadius: BorderRadius.circular(12))
          : null,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(8),
              child: SizedBox(
                width: 76,
                height: 76,
                child: photo != null
                    ? Image.file(photo!, fit: BoxFit.cover)
                    : ColoredBox(
                        color: scheme.surfaceContainerHighest,
                        child: Center(child: Text('$number', style: text.headlineMedium)),
                      ),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('$number. $title', style: text.titleMedium),
                  Text(hint, style: text.bodyMedium),
                  if (bad) ...[
                    const SizedBox(height: 4),
                    Text(problem!, style: text.bodyMedium?.copyWith(color: scheme.error)),
                  ],
                  Row(
                    children: [
                      TextButton.icon(
                        onPressed: enabled ? onCamera : null,
                        icon: const Icon(Icons.camera_alt),
                        label: Text(photo == null ? 'Camera' : 'Retake'),
                      ),
                      IconButton(
                        tooltip: 'Choose from gallery',
                        onPressed: enabled ? onGallery : null,
                        icon: const Icon(Icons.photo_library_outlined),
                      ),
                      if (photo != null)
                        IconButton(
                          tooltip: 'Remove this photo',
                          onPressed: enabled ? onRemove : null,
                          icon: const Icon(Icons.delete_outline),
                        ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
