import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../media.dart';
import '../../remote_client.dart';
import 'common.dart';
import 'record_button.dart';
import 'settings_sheet.dart';

/// Live tab (REC-01..07): preview over Wi-Fi, the record button, status and settings.
class LiveView extends StatefulWidget {
  const LiveView({super.key, required this.client, required this.active, required this.onOpenGallery});
  final RemoteClient client;
  final bool active;
  final VoidCallback onOpenGallery;

  @override
  State<LiveView> createState() => _LiveViewState();
}

class _LiveViewState extends State<LiveView> with WidgetsBindingObserver {
  late final PreviewController _preview = PreviewController(widget.client.media);
  bool _previewOn = true;
  bool _pending = false;
  bool _foreground = true;
  double _elapsedBase = 0;
  DateTime _elapsedAt = DateTime.now();
  Timer? _tick;

  RemoteClient get client => widget.client;
  MediaLink get media => client.media;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    SharedPreferences.getInstance().then((p) {
      if (mounted) setState(() => _previewOn = p.getBool('preview_on') ?? true);
      _sync();
    });
    media.addListener(_sync);
    client.topic('recorder').addListener(_onRecorder);
    _tick = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (mounted && _rec['recording']?['active'] == true) setState(() {});
    });
    _onRecorder();
  }

  @override
  void didUpdateWidget(LiveView old) {
    super.didUpdateWidget(old);
    if (old.active != widget.active) _sync();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    _sync();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    media.removeListener(_sync);
    client.topic('recorder').removeListener(_onRecorder);
    _tick?.cancel();
    _preview.dispose();
    super.dispose();
  }

  Map<String, dynamic> get _rec => asMap(client.topic('recorder').value);

  void _onRecorder() {
    final r = asMap(_rec['recording']);
    if (r['active'] == true) {
      _elapsedBase = (r['elapsed'] as num?)?.toDouble() ?? 0;
      _elapsedAt = DateTime.now();
    }
    _sync();
  }

  /// Preview only while it is looked at (REC-06: the Pi stops the encoder when nobody watches).
  void _sync() {
    final want = widget.active && _foreground && _previewOn && media.ready && _rec['available'] == true;
    if (want && !_preview.running) {
      _preview.start();
    } else if (!want && _preview.running) {
      _preview.stop();
    }
  }

  Future<void> _setPreviewOn(bool on) async {
    setState(() => _previewOn = on);
    (await SharedPreferences.getInstance()).setBool('preview_on', on);
    _sync();
  }

  Future<void> _toggleRecording() async {
    final active = asMap(_rec['recording'])['active'] == true;
    setState(() => _pending = true);
    await runOp(context, client, active ? 'recorder.stop' : 'recorder.start', const {}, null, const Duration(seconds: 60));
    if (mounted) setState(() => _pending = false);
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
      listenable: Listenable.merge([client.topic('recorder'), client.topic('recorder.settings'), media, _preview]),
      builder: (context, _) {
        final wide = MediaQuery.sizeOf(context).width >= 1000;
        final preview = _previewCard(context);
        final panel = _panel(context);
        return SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
          child: wide
              ? Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Expanded(child: preview),
                  const SizedBox(width: 16),
                  SizedBox(width: 340, child: panel),
                ])
              : Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  preview,
                  const SizedBox(height: 14),
                  panel,
                ]),
        );
      },
    );
  }

  // --------------------------------------------------------------- preview
  Widget _previewCard(BuildContext context) {
    final rec = _rec;
    final sig = asMap(rec['signal']);
    final r = asMap(rec['recording']);
    final settings = asMap(client.topic('recorder.settings').value);
    final quality = asMap(settings['preview'])['quality'] as String? ?? 'medium';
    final playing = _preview.playing && _previewOn;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      ClipRRect(
        borderRadius: BorderRadius.circular(18),
        child: AspectRatio(
          aspectRatio: 16 / 9,
          child: ColoredBox(
            color: Colors.black,
            child: Stack(fit: StackFit.expand, children: [
              if (_preview.textureId != null)
                AnimatedOpacity(
                  opacity: playing ? 1 : 0,
                  duration: const Duration(milliseconds: 400),
                  child: Center(
                    child: AspectRatio(aspectRatio: _preview.aspect, child: Texture(textureId: _preview.textureId!)),
                  ),
                ),
              AnimatedOpacity(
                opacity: playing ? 0 : 1,
                duration: const Duration(milliseconds: 300),
                child: IgnorePointer(ignoring: playing, child: _message(context, sig)),
              ),
              Positioned(left: 10, top: 10, child: _glass(
                  Row(mainAxisSize: MainAxisSize.min, children: [
                    Container(width: 7, height: 7, decoration: BoxDecoration(
                        color: playing ? kOk : Colors.grey, shape: BoxShape.circle)),
                    const SizedBox(width: 6),
                    Text(
                        (playing ? 'Live' : !_previewOn ? 'Paused' : sig['present'] == true ? 'Preview' : 'No signal') +
                            (playing && (asMap(rec['preview'])['viewers'] as num? ?? 0) > 1
                                ? ' · ${asMap(rec['preview'])['viewers']} watching'
                                : ''),
                        style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: Colors.white)),
                  ]))),
              if (sig['present'] == true && sig['width'] != null)
                Positioned(right: 10, top: 10, child: _glass(Text(
                    '${sig['width']}×${sig['height']} · ${((sig['fps'] as num?) ?? 0).round()} fps',
                    style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: Colors.white)))),
              if (r['active'] == true)
                Positioned(left: 10, bottom: 10, child: _glass(
                    Row(mainAxisSize: MainAxisSize.min, children: [
                      const _Blink(child: Icon(Icons.circle, size: 8, color: Colors.white)),
                      const SizedBox(width: 6),
                      Text('REC ${clock(_elapsed)}',
                          style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: Colors.white)),
                    ]),
                    color: kRec.withValues(alpha: 0.85))),
              if (playing)
                Positioned(
                  right: 6,
                  bottom: 6,
                  child: IconButton(
                    tooltip: 'Full screen',
                    color: Colors.white,
                    style: IconButton.styleFrom(backgroundColor: Colors.black45),
                    icon: const Icon(Icons.fullscreen_rounded),
                    onPressed: () => _fullscreen(context),
                  ),
                ),
            ]),
          ),
        ),
      ),
      const SizedBox(height: 10),
      Wrap(
        alignment: WrapAlignment.spaceBetween,
        crossAxisAlignment: WrapCrossAlignment.center,
        runSpacing: 8,
        spacing: 8,
        children: [
          InkWell(
            borderRadius: BorderRadius.circular(20),
            onTap: () => _setPreviewOn(!_previewOn),
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              Switch(value: _previewOn, onChanged: _setPreviewOn),
              const SizedBox(width: 4),
              const Text('Live preview'),
            ]),
          ),
          SegmentedButton<String>(
            showSelectedIcon: false,
            style: const ButtonStyle(visualDensity: VisualDensity.compact),
            segments: const [
              ButtonSegment(value: 'low', label: Text('360p')),
              ButtonSegment(value: 'medium', label: Text('720p')),
              ButtonSegment(value: 'high', label: Text('1080p')),
            ],
            selected: {quality},
            onSelectionChanged: (s) =>
                runOp(context, client, 'recorder.settings.set', {'settings': {'preview': {'quality': s.first}}}),
          ),
        ],
      ),
    ]);
  }

  Widget _glass(Widget child, {Color? color}) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(color: color ?? Colors.black.withValues(alpha: 0.6), borderRadius: BorderRadius.circular(20)),
        child: child,
      );

  Widget _message(BuildContext context, Map<String, dynamic> sig) {
    IconData? icon;
    String title;
    String detail = '';
    Widget? action;
    if (_rec['available'] != true) {
      icon = Icons.error_outline;
      title = 'Recorder not running';
      detail = client.isConnected ? 'The recorder on the Pi is starting or switched off.' : 'Not connected to the Pi.';
    } else if (!_previewOn) {
      icon = Icons.pause_circle_outline;
      title = 'Preview paused';
      detail = 'Recording works without it.';
      action = FilledButton.tonal(onPressed: () => _setPreviewOn(true), child: const Text('Resume'));
    } else if (!media.ready) {
      icon = Icons.wifi_off_rounded;
      title = media.checking ? 'Looking for the Pi on Wi-Fi…' : 'Preview needs Wi-Fi';
      detail = media.checking ? '' : (media.error ?? '');
      if (!media.checking) action = FilledButton.tonal(onPressed: media.refresh, child: const Text('Retry'));
    } else if (sig['present'] != true) {
      icon = Icons.desktop_access_disabled_outlined;
      title = 'No HDMI signal';
      detail = sig['why'] as String? ?? 'Connect a source to the HDMI input.';
    } else {
      title = _preview.state == 'reconnecting' ? 'Reconnecting…' : 'Starting preview…';
    }
    return LayoutBuilder(builder: (context, c) {
      final small = c.maxHeight < 200;
      return Padding(
        padding: EdgeInsets.fromLTRB(20, small ? 34 : 20, 20, 12),
        child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
          if (icon != null && !small) Icon(icon, size: 34, color: Colors.white54),
          if (icon == null) const SizedBox(width: 26, height: 26, child: CircularProgressIndicator(strokeWidth: 2.4)),
          const SizedBox(height: 8),
          Text(title, textAlign: TextAlign.center,
              style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600, fontSize: 15)),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 4),
            Flexible(
              child: Text(detail, textAlign: TextAlign.center, maxLines: small ? 2 : 4, overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Colors.white70, fontSize: 12.5)),
            ),
          ],
          if (action != null && !small) ...[const SizedBox(height: 10), action],
        ]),
      );
    });
  }

  void _fullscreen(BuildContext context) {
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
    Navigator.of(context).push(PageRouteBuilder(
      opaque: true,
      transitionDuration: const Duration(milliseconds: 250),
      pageBuilder: (_, _, _) => GestureDetector(
        onTap: () => Navigator.of(context).pop(),
        child: ColoredBox(
          color: Colors.black,
          child: Center(
            child: ListenableBuilder(
              listenable: _preview,
              builder: (_, _) => _preview.textureId == null
                  ? const SizedBox()
                  : AspectRatio(aspectRatio: _preview.aspect, child: Texture(textureId: _preview.textureId!)),
            ),
          ),
        ),
      ),
      transitionsBuilder: (_, a, _, child) => FadeTransition(opacity: a, child: child),
    )).then((_) => SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge));
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

class _Blink extends StatefulWidget {
  const _Blink({required this.child});
  final Widget child;

  @override
  State<_Blink> createState() => _BlinkState();
}

class _BlinkState extends State<_Blink> with SingleTickerProviderStateMixin {
  late final AnimationController _c =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 1200))..repeat(reverse: true);

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => FadeTransition(opacity: Tween(begin: 1.0, end: 0.3).animate(_c), child: widget.child);
}
