import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../media.dart';
import '../remote_client.dart';
import 'recorder/common.dart';

/// Remote screen (SCR-01..04): the Pi's desktop, live over Wi-Fi, controlled by touch -
/// tap = click, double tap = double click, long press = right click, drag = drag with the
/// left button, two fingers = scroll. Everything is mapped to the same desktop pixel.
class ScreenView extends StatefulWidget {
  const ScreenView({super.key, required this.client, required this.active});
  final RemoteClient client;
  final bool active;

  @override
  State<ScreenView> createState() => _ScreenViewState();
}

class _ScreenViewState extends State<ScreenView> with WidgetsBindingObserver {
  late final PreviewController _preview = PreviewController(widget.client.media, path: 'ws/screen');
  bool _control = true;
  bool _foreground = true;
  bool _dragging = false;
  Offset? _doubleTapAt;
  Timer? _moveTimer;
  List<int>? _pendingMove;

  RemoteClient get client => widget.client;
  Map<String, dynamic> get _st => asMap(client.topic('screen').value);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    client.media.addListener(_sync);
    client.topic('screen').addListener(_sync);
    _sync();
  }

  @override
  void didUpdateWidget(ScreenView old) {
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
    client.media.removeListener(_sync);
    client.topic('screen').removeListener(_sync);
    _moveTimer?.cancel();
    if (_dragging) client.notify('in.btn', {'b': 'left', 'a': 'up'});
    _preview.dispose();
    super.dispose();
  }

  void _sync() {
    final want = widget.active && _foreground && client.media.ready && _st['available'] != false;
    if (want && !_preview.running) {
      _preview.start();
    } else if (!want && _preview.running) {
      _preview.stop();
    }
  }

  // ------------------------------------------------------------ mapping
  List<int>? _toScreen(Offset p, Size box) {
    final scr = _st['screen'];
    if (scr is! List || scr.length < 2) return null;
    final sw = (scr[0] as num).toDouble(), sh = (scr[1] as num).toDouble();
    if (sw <= 0 || sh <= 0) return null;
    final k = (box.width / sw) < (box.height / sh) ? box.width / sw : box.height / sh;
    final ox = (box.width - sw * k) / 2, oy = (box.height - sh * k) / 2;
    final x = (p.dx - ox) / k, y = (p.dy - oy) / k;
    if (x < 0 || y < 0 || x >= sw || y >= sh) return null;
    return [x.round(), y.round()];
  }

  void _moveTo(List<int> p, {bool now = false}) {
    _pendingMove = p;
    if (now) {
      _moveTimer?.cancel();
      _moveTimer = null;
      _flushMove();
    } else {
      _moveTimer ??= Timer(const Duration(milliseconds: 16), _flushMove);   // ~60 moves/s max
    }
  }

  void _flushMove() {
    _moveTimer = null;
    final p = _pendingMove;
    _pendingMove = null;
    if (p != null) client.notify('in.move_to', {'x': p[0], 'y': p[1]});
  }

  void _click(Offset pos, Size box, String button, String action) {
    final p = _toScreen(pos, box);
    if (p == null) return;
    HapticFeedback.selectionClick();
    _moveTo(p, now: true);
    client.notify('in.btn', {'b': button, 'a': action});
  }

  // --------------------------------------------------------------- build
  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([client.topic('screen'), client.media, _preview]),
      builder: (context, _) {
        final st = _st;
        final scr = st['screen'] is List ? st['screen'] as List : null;
        final playing = _preview.playing;
        final theme = Theme.of(context);
        return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Wrap(
            crossAxisAlignment: WrapCrossAlignment.center,
            spacing: 10,
            runSpacing: 6,
            children: [
              Tag(playing ? 'Live' : st['state'] == 'error' ? 'No picture' : 'Screen',
                  color: playing ? kOk : null, dot: true),
              if (scr != null) Text('${scr[0]}×${scr[1]}', style: theme.textTheme.bodySmall),
              InkWell(
                onTap: () => setState(() => _control = !_control),
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  Switch(value: _control, onChanged: (v) => setState(() => _control = v)),
                  const Text('Control'),
                ]),
              ),
              SegmentedButton<String>(
                showSelectedIcon: false,
                style: const ButtonStyle(visualDensity: VisualDensity.compact),
                segments: const [
                  ButtonSegment(value: 'low', label: Text('Low')),
                  ButtonSegment(value: 'medium', label: Text('Medium')),
                  ButtonSegment(value: 'high', label: Text('High')),
                ],
                selected: {st['quality'] as String? ?? 'medium'},
                onSelectionChanged: (s) =>
                    runOp(context, client, 'screen.settings.set', {'settings': {'quality': s.first}}),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Expanded(
            child: ClipRRect(
              borderRadius: BorderRadius.circular(16),
              child: ColoredBox(
                color: Colors.black,
                child: LayoutBuilder(builder: (context, c) {
                  final box = Size(c.maxWidth, c.maxHeight);
                  return GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onTapUp: _control ? (d) => _click(d.localPosition, box, 'left', 'click') : null,
                    onDoubleTapDown: _control ? (d) => _doubleTapAt = d.localPosition : null,
                    onDoubleTap: _control
                        ? () {
                            if (_doubleTapAt != null) _click(_doubleTapAt!, box, 'left', 'double');
                          }
                        : null,
                    onLongPressStart: _control ? (d) => _click(d.localPosition, box, 'right', 'click') : null,
                    onScaleStart: _control
                        ? (d) {
                            if (d.pointerCount == 1) {
                              final p = _toScreen(d.localFocalPoint, box);
                              if (p == null) return;
                              _moveTo(p, now: true);
                              client.notify('in.btn', {'b': 'left', 'a': 'down'});
                              _dragging = true;
                            }
                          }
                        : null,
                    onScaleUpdate: _control
                        ? (d) {
                            if (d.pointerCount >= 2) {
                              if (_dragging) {
                                client.notify('in.btn', {'b': 'left', 'a': 'up'});
                                _dragging = false;
                              }
                              final dy = -d.focalPointDelta.dy / 22, dx = -d.focalPointDelta.dx / 22;
                              if (dy.abs() > 0.01 || dx.abs() > 0.01) client.notify('in.scroll', {'dx': dx, 'dy': dy});
                            } else if (_dragging) {
                              final p = _toScreen(d.localFocalPoint, box);
                              if (p != null) _moveTo(p);
                            }
                          }
                        : null,
                    onScaleEnd: _control
                        ? (_) {
                            if (_dragging) {
                              _flushMove();
                              client.notify('in.btn', {'b': 'left', 'a': 'up'});
                              _dragging = false;
                            }
                          }
                        : null,
                    child: Stack(fit: StackFit.expand, children: [
                      if (_preview.textureId != null)
                        AnimatedOpacity(
                          opacity: playing ? 1 : 0,
                          duration: const Duration(milliseconds: 300),
                          child: Center(
                            child: AspectRatio(
                              aspectRatio: scr != null ? (scr[0] as num) / (scr[1] as num) : _preview.aspect,
                              child: Texture(textureId: _preview.textureId!),
                            ),
                          ),
                        ),
                      if (!playing) Center(child: FittedBox(fit: BoxFit.scaleDown, child: _message(context, st))),
                    ]),
                  );
                }),
              ),
            ),
          ),
          const SizedBox(height: 6),
          Text(
            _control
                ? 'Tap = click · double tap = double click · long press = right click · drag = drag · two fingers = scroll'
                : 'View only - switch Control on to use the Pi',
            textAlign: TextAlign.center,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: theme.textTheme.bodySmall,
          ),
        ]);
      },
    );
  }

  Widget _message(BuildContext context, Map<String, dynamic> st) {
    final media = client.media;
    String title, detail = '';
    IconData? icon;
    if (st['available'] == false) {
      icon = Icons.desktop_access_disabled_outlined;
      title = 'No desktop';
      detail = st['error'] as String? ?? 'The Pi has no desktop session.';
    } else if (!media.ready) {
      icon = Icons.wifi_off_rounded;
      title = media.checking ? 'Looking for the Pi on Wi-Fi…' : 'The screen needs Wi-Fi';
      detail = media.checking ? '' : (media.error ?? '');
    } else if (st['state'] == 'error') {
      icon = Icons.error_outline;
      title = 'No picture';
      detail = st['error'] as String? ?? '';
    } else {
      title = 'Connecting to the desktop…';
    }
    return Padding(
      padding: const EdgeInsets.all(20),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 420),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
        if (icon != null) Icon(icon, size: 34, color: Colors.white54)
        else const SizedBox(width: 26, height: 26, child: CircularProgressIndicator(strokeWidth: 2.4)),
        const SizedBox(height: 8),
        Text(title, textAlign: TextAlign.center, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
        if (detail.isNotEmpty) ...[
          const SizedBox(height: 4),
          Text(detail, textAlign: TextAlign.center, maxLines: 3, overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Colors.white70, fontSize: 12.5)),
        ],
        if (!media.ready && !media.checking) ...[
          const SizedBox(height: 10),
          FilledButton.tonal(onPressed: media.refresh, child: const Text('Retry')),
        ],
        ]),
      ),
    );
  }
}
