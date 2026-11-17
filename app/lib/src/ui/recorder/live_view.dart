import 'dart:async';

import 'package:flutter/material.dart';

import '../../remote_client.dart';
import 'common.dart';
import 'record_button.dart';
import 'settings_sheet.dart';

/// Live tab (REC-01, REC-03..07): the source's signal, the record button, status and settings -
/// and for film work the tally (REC-13), timecode, slate and markers (REC-11, REC-15) and the
/// storage figures with the disk test (REC-12). All over Bluetooth: the picture itself (preview,
/// monitoring, focus loupe, meters) is the web UI's (CON-06).
class LiveView extends StatefulWidget {
  const LiveView({super.key, required this.client, required this.active, required this.onOpenGallery});
  final RemoteClient client;
  final bool active;
  final VoidCallback onOpenGallery;

  @override
  State<LiveView> createState() => _LiveViewState();
}

class _LiveViewState extends State<LiveView> {
  bool _pending = false;
  double _elapsedBase = 0;
  DateTime _elapsedAt = DateTime.now();
  Timer? _tick;

  RemoteClient get client => widget.client;

  @override
  void initState() {
    super.initState();
    client.topic('recorder').addListener(_onRecorder);
    _tick = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (mounted && widget.active && _rec['recording']?['active'] == true) setState(() {});
    });
    _onRecorder();
  }

  @override
  void dispose() {
    client.topic('recorder').removeListener(_onRecorder);
    _tick?.cancel();
    super.dispose();
  }

  Map<String, dynamic> get _rec => asMap(client.topic('recorder').value);

  void _onRecorder() {
    final r = asMap(_rec['recording']);
    if (r['active'] == true) {
      _elapsedBase = (r['elapsed'] as num?)?.toDouble() ?? 0;
      _elapsedAt = DateTime.now();
    }
  }

  Future<void> _toggleRecording() async {
    final active = asMap(_rec['recording'])['active'] == true;
    setState(() => _pending = true);
    if (active) {
      await runOp(context, client, 'recorder.stop', const {}, null, const Duration(seconds: 60));
    } else {
      await _start();
    }
    if (mounted) setState(() => _pending = false);
  }

  /// REC-12: a disk measured too slow refuses the start - the user may record anyway.
  Future<void> _start({bool force = false}) async {
    final messenger = ScaffoldMessenger.maybeOf(context);
    try {
      await client.request('recorder.start', {if (force) 'force': true}, const Duration(seconds: 60));
    } catch (e) {
      final msg = e.toString();
      if (!force && msg.startsWith('disk too slow') && mounted) {
        final why = msg.replaceFirst(RegExp(r'\s*\(record anyway: force\)\s*$'), '');
        if (await confirm(context, 'Record anyway?', '${why[0].toUpperCase()}${why.substring(1)}.',
                ok: 'Record anyway') &&
            mounted) {
          await _start(force: true);
        }
        return;
      }
      messenger?.showSnackBar(SnackBar(content: Text(msg), backgroundColor: const Color(0xFF3A2321)));
    }
  }

  Future<void> _mark() async {
    final messenger = ScaffoldMessenger.maybeOf(context);
    final m = await runOp(context, client, 'recorder.mark');
    if (m != null) {
      messenger?.showSnackBar(
          SnackBar(content: Text('Marker at ${asMap(m)['tc'] ?? '–'}'), duration: const Duration(seconds: 2)));
    }
  }

  Future<void> _testDisk() async {
    final messenger = ScaffoldMessenger.maybeOf(context);
    final r = await runOp(context, client, 'recorder.disk_test', const {}, null, const Duration(seconds: 90));
    if (r != null) {
      final m = asMap(r);
      messenger?.showSnackBar(SnackBar(content: Text('This disk writes ${perSec(m['speed'] as num?)}')));
    }
  }

  double get _elapsed {
    final r = asMap(_rec['recording']);
    if (r['active'] != true) return 0;
    if (r['stopping'] == true) return _elapsedBase;
    return _elapsedBase + DateTime.now().difference(_elapsedAt).inMilliseconds / 1000;
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([client.topic('recorder'), client.topic('recorder.settings')]),
      builder: (context, _) {
        final wide = MediaQuery.sizeOf(context).width >= 1000;
        final signal = _signalCard(context);
        final panel = _panel(context);
        return SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
          child: wide
              ? Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Expanded(child: signal),
                  const SizedBox(width: 16),
                  SizedBox(width: 340, child: panel),
                ])
              : Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  signal,
                  const SizedBox(height: 12),
                  panel,
                ]),
        );
      },
    );
  }

  // ---------------------------------------------------------------- signal
  /// What the recorder sees, without the picture: the source's signal, or why there is none,
  /// and the tally while recording (REC-01, REC-13).
  Widget _signalCard(BuildContext context) {
    final theme = Theme.of(context);
    final rec = _rec;
    final sig = asMap(rec['signal']);
    final recording = asMap(rec['recording'])['active'] == true;
    final up = rec['available'] == true;
    final present = up && sig['present'] == true;
    final title = !client.isConnected
        ? 'Not connected'
        : !up
            ? 'Recorder not running'
            : present
                ? '${sig['width']}×${sig['height']} · ${((sig['fps'] as num?) ?? 0).round()} fps'
                : 'No signal';
    final detail = !up
        ? 'The recorder on the Pi is starting or switched off.'
        : present
            ? [rec['source_title'] ?? 'HDMI input', sig['format']].whereType<String>().join(' · ')
            : (sig['why'] as String? ?? 'Connect a source to the input.');
    final dot = recording ? kRec : present ? kOk : up ? kWarn : theme.colorScheme.outline;
    return Card(
      key: const Key('signal-card'),
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: recording ? const BorderSide(color: kRec, width: 2) : BorderSide(color: theme.dividerColor),
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Container(width: 8, height: 8, decoration: BoxDecoration(color: dot, shape: BoxShape.circle)),
            const SizedBox(width: 10),
            Expanded(
              child: Text(title,
                  style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w600, fontFeatures: const [FontFeature.tabularFigures()])),
            ),
            if (recording)
              Container(
                key: const Key('tally'),
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(color: kRec.withValues(alpha: 0.14), borderRadius: BorderRadius.circular(6)),
                child: Text('REC ${clock(_elapsed)}',
                    style: const TextStyle(color: kRec, fontSize: 12, fontWeight: FontWeight.w600)),
              ),
          ]),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 4),
            Padding(
              padding: const EdgeInsets.only(left: 18),
              child: Text(detail, style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            ),
          ],
          const SizedBox(height: 10),
          Padding(
            padding: const EdgeInsets.only(left: 18),
            child: Text('The live picture, monitoring and meters are in the web UI (Settings › Web access).',
                style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ),
        ]),
      ),
    );
  }

  // ----------------------------------------------------------------- panel
  Widget _panel(BuildContext context) {
    final rec = _rec;
    final r = asMap(rec['recording']);
    final sig = asMap(rec['signal']);
    final disk = asMap(rec['disk']);
    final settings = asMap(client.topic('recorder.settings').value);
    final active = r['active'] == true;
    final stopping = r['stopping'] == true;
    final canStart = rec['available'] == true && sig['present'] == true;
    final theme = Theme.of(context);
    final hint = active
        ? (stopping ? 'Finishing the file…' : r['file'] as String? ?? '')
        : rec['available'] != true
            ? 'Recorder not running'
            : sig['present'] != true
                ? 'Waiting for a signal'
                : _pending
                    ? 'Starting…'
                    : 'Tap to record';
    final last = asMap(rec['last']);
    final take = asMap(rec['take']);
    final storage = asMap(rec['storage']);
    final av = asMap(r['av']);
    final tc = take['tc'] as String? ?? r['tc'] as String?;
    final muted = theme.colorScheme.onSurfaceVariant;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Card(
        margin: EdgeInsets.zero,
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 16),
          child: Column(children: [
            RecordButton(
              recording: active,
              busy: _pending || stopping,
              onPressed: active || canStart ? _toggleRecording : null,
            ),
            const SizedBox(height: 6),
            AnimatedDefaultTextStyle(
              duration: const Duration(milliseconds: 250),
              style: theme.textTheme.headlineMedium!.copyWith(
                fontWeight: FontWeight.w600,
                color: active ? kRec : theme.colorScheme.onSurface,
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
              child: Text(clock(_elapsed)),
            ),
            const SizedBox(height: 2),
            Text(hint, textAlign: TextAlign.center, maxLines: 1, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodySmall),
            // REC-11, REC-15: the take's timecode and markers while recording, else the next take's name
            const SizedBox(height: 10),
            if (active && !stopping)
              Wrap(
                alignment: WrapAlignment.center,
                crossAxisAlignment: WrapCrossAlignment.center,
                spacing: 10,
                runSpacing: 6,
                children: [
                  if (tc != null)
                    Text('TC $tc',
                        style: theme.textTheme.titleSmall?.copyWith(
                            fontWeight: FontWeight.w600, fontFeatures: const [FontFeature.tabularFigures()])),
                  Text('${take['markers'] ?? 0} marker${take['markers'] == 1 ? '' : 's'}',
                      style: theme.textTheme.bodySmall),
                  FilledButton.tonalIcon(
                    style: FilledButton.styleFrom(visualDensity: VisualDensity.compact),
                    onPressed: _mark,
                    icon: const Icon(Icons.bookmark_add_outlined, size: 18),
                    label: const Text('Set marker'),
                  ),
                ],
              )
            else if (take['next'] != null)
              Text('Next take ${take['next']}',
                  textAlign: TextAlign.center,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall?.copyWith(color: muted)),
            if (rec['clock_synced'] == false)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                  const Icon(Icons.schedule_rounded, size: 15, color: kWarn),
                  const SizedBox(width: 6),
                  Flexible(
                    child: Text('Board clock not synced (NTP): the timecode may not match other devices',
                        maxLines: 2, style: theme.textTheme.bodySmall?.copyWith(color: kWarn)),
                  ),
                ]),
              ),
          ]),
        ),
      ),
      const SizedBox(height: 12),
      Card(
        margin: EdgeInsets.zero,
        child: ListTile(
          contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 4),
          leading: Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(color: theme.colorScheme.primary.withValues(alpha: 0.14), borderRadius: BorderRadius.circular(12)),
            child: Icon(Icons.tune_rounded, color: theme.colorScheme.primary),
          ),
          title: Text(rec['mode_text'] as String? ?? '–', style: const TextStyle(fontWeight: FontWeight.w600)),
          subtitle: Text('${asMap(settings['audio'])['record'] == true ? 'Audio on' : 'Audio off'} · EDID ${settings['edid'] ?? '–'}'),
          trailing: const Icon(Icons.chevron_right_rounded),
          onTap: () => showRecorderSettings(context, client),
        ),
      ),
      const SizedBox(height: 12),
      Card(
        margin: EdgeInsets.zero,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('STATUS', style: theme.textTheme.labelMedium?.copyWith(letterSpacing: 1.1, fontWeight: FontWeight.w600)),
            const SizedBox(height: 12),
            AnimatedSwitcher(
              duration: const Duration(milliseconds: 250),
              child: active
                  ? MetricGrid(key: const ValueKey('rec'), [
                      Metric('Size', size(r['size'] as num?)),
                      Metric('Data rate', mbit(r['rate'] as num?)),
                      Metric('Dropped', '${r['drops'] ?? 0}'),
                      Metric('Free', size(disk['free'] as num?)),
                      if (av['drift_ms'] != null) Metric('A/V sync', '${(av['drift_ms'] as num).round()} ms'),
                    ])
                  : MetricGrid(key: const ValueKey('idle'), [
                      Metric('Signal', sig['present'] == true ? '${sig['width']}×${sig['height']}' : '–',
                          sub: sig['present'] == true ? '${((sig['fps'] as num?) ?? 0).round()} fps' : null),
                      Metric('Format', sig['format'] as String? ?? '–'),
                      Metric('Free', size(disk['free'] as num?)),
                      Metric('Audio', asMap(rec['capture'])['audio'] == true ? 'Yes' : asMap(rec['caps'])['audio'] == true ? 'Ready' : 'No'),
                    ]),
            ),
            if (!active && rec['available'] == true && sig['present'] != true && sig['why'] != null) ...[
              const Divider(height: 24),
              Text(sig['why'] as String, style: theme.textTheme.bodySmall),
            ],
          ]),
        ),
      ),
      if (storage.isNotEmpty) ...[
        const SizedBox(height: 12),
        _StorageCard(storage: storage, recording: active, onTest: _testDisk),
      ],
      if (last.isNotEmpty && !active) ...[
        const SizedBox(height: 12),
        Card(
          margin: EdgeInsets.zero,
          child: ListTile(
            title: Text(last['file'] as String? ?? '–', maxLines: 1, overflow: TextOverflow.ellipsis),
            subtitle: Text([
              if (last['duration'] != null) clock(last['duration'] as num),
              if (last['size'] != null) size(last['size'] as num),
              ?last['reason'] as String?,
              ?last['error'] as String?,
            ].join(' · ')),
            trailing: TextButton(onPressed: widget.onOpenGallery, child: const Text('Gallery')),
          ),
        ),
      ],
    ]);
  }
}

/// Storage you can trust (REC-12): free space, the time left at the mode's rate, the disk's
/// measured speed, the warning, and the disk test.
class _StorageCard extends StatelessWidget {
  const _StorageCard({required this.storage, required this.recording, required this.onTest});
  final Map<String, dynamic> storage;
  final bool recording;
  final VoidCallback onTest;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final testing = storage['testing'] == true;
    final warning = storage['warning'] as String?;
    final speed = storage['speed'] as num?;
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 8, 16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(
              child: Text('STORAGE', style: theme.textTheme.labelMedium?.copyWith(letterSpacing: 1.1, fontWeight: FontWeight.w600)),
            ),
            TextButton.icon(
              onPressed: recording || testing ? null : onTest,
              icon: testing
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.speed_rounded, size: 18),
              label: Text(testing ? 'Testing…' : 'Test disk'),
            ),
          ]),
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: MetricGrid([
              Metric('Time left', timeLeft(storage['seconds_left'] as num?)),
              Metric('Free', size(storage['free'] as num?)),
              Metric(recording ? 'Writing' : 'Mode needs', perSec(storage['rate'] as num?)),
              Metric('Disk speed', speed == null ? 'Not measured' : perSec(speed)),
            ]),
          ),
          if (warning != null && warning.isNotEmpty) ...[
            const SizedBox(height: 10),
            Padding(
              padding: const EdgeInsets.only(right: 8),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Icon(Icons.warning_amber_rounded, size: 18, color: storage['ok'] == false ? kRec : kWarn),
                const SizedBox(width: 8),
                Expanded(
                  child: Text('${warning[0].toUpperCase()}${warning.substring(1)}',
                      style: theme.textTheme.bodySmall?.copyWith(color: storage['ok'] == false ? kRec : kWarn)),
                ),
              ]),
            ),
          ],
        ]),
      ),
    );
  }
}
