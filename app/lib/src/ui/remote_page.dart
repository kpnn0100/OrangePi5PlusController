import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_scope.dart';
import '../remote_client.dart';

/// Mouse + keyboard for the Pi desktop.
///
/// The pad only moves the pointer (and scrolls with two fingers); clicks are
/// separate press-and-hold buttons, so you can drag by holding Left with one
/// finger while moving on the pad with another.
class RemotePage extends StatelessWidget {
  const RemotePage({super.key, required this.active});
  final bool active;

  @override
  Widget build(BuildContext context) {
    final client = AppScope.of(context).client;
    return ListenableBuilder(
      listenable: client,
      builder: (context, _) {
        final warning = client.isConnected && !client.inputAvailable
            ? 'The Pi desktop (X11) is not reachable, so mouse and keyboard input will fail.'
            : null;
        return LayoutBuilder(builder: (context, box) {
          final wide = box.maxWidth >= 900;
          final mouse = Column(children: [
            const Expanded(child: _Touchpad()),
            const SizedBox(height: 12),
            const SizedBox(height: 96, child: _MouseButtons()),
          ]);
          return Column(children: [
            if (warning != null)
              MaterialBanner(
                content: Text(warning),
                leading: const Icon(Icons.warning_amber),
                actions: const [SizedBox.shrink()],
              ),
            Expanded(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: wide
                    ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                        Expanded(child: mouse),
                        const SizedBox(width: 12),
                        SizedBox(width: min(520, box.maxWidth * 0.38), child: const _KeyboardPanel()),
                      ])
                    : Column(children: [
                        Expanded(child: mouse),
                        const SizedBox(height: 12),
                        ConstrainedBox(
                          constraints: BoxConstraints(maxHeight: box.maxHeight * 0.42),
                          child: const _KeyboardPanel(compact: true),
                        ),
                      ]),
              ),
            ),
          ]);
        });
      },
    );
  }
}

// ------------------------------------------------------------------ touchpad
class _Touchpad extends StatefulWidget {
  const _Touchpad();

  @override
  State<_Touchpad> createState() => _TouchpadState();
}

class _TouchpadState extends State<_Touchpad> {
  final Map<int, Offset> _points = {};
  final Map<int, Offset> _downAt = {};
  DateTime? _downTime;
  int _maxPointers = 0;
  double _moved = 0;
  double _scrollAccX = 0, _scrollAccY = 0;
  bool _scrollZone = false;

  RemoteClient get _client => AppScope.of(context).client;

  double _gain(double dist) {
    final s = AppScope.of(context).settings.pointerSpeed;
    // Mild acceleration: slow moves stay precise, fast flicks cross the screen.
    return s * (1.0 + 0.9 * ((dist - 1.5) / 12).clamp(0.0, 1.8));
  }

  void _down(PointerDownEvent e, bool zone) {
    if (_points.isEmpty) {
      _downTime = DateTime.now();
      _maxPointers = 0;
      _moved = 0;
      _scrollZone = zone;
    }
    _points[e.pointer] = e.localPosition;
    _downAt[e.pointer] = e.localPosition;
    _maxPointers = max(_maxPointers, _points.length);
    setState(() {});
  }

  void _move(PointerMoveEvent e) {
    final prev = _points[e.pointer];
    if (prev == null) return;
    final delta = e.localPosition - prev;
    _points[e.pointer] = e.localPosition;
    _moved += delta.distance;
    final settings = AppScope.of(context).settings;
    if (_scrollZone || _points.length >= 2) {
      // With two fingers each one reports the motion; use the average.
      final share = _scrollZone ? 1.0 : 1.0 / _points.length;
      final dir = settings.naturalScroll ? -1.0 : 1.0;
      final perClick = 28 / settings.scrollSpeed;
      _scrollAccY += delta.dy * share * dir / perClick;
      _scrollAccX += (_scrollZone ? 0 : delta.dx * share * dir / perClick);
      if (_scrollAccY.abs() >= 0.2 || _scrollAccX.abs() >= 0.2) {
        _client.scroll(_scrollAccX, _scrollAccY);
        _scrollAccX = _scrollAccY = 0;
      }
    } else {
      final g = _gain(delta.distance);
      _client.mouseMove(delta.dx * g, delta.dy * g);
    }
    setState(() {});
  }

  void _up(PointerEvent e) {
    _points.remove(e.pointer);
    _downAt.remove(e.pointer);
    if (_points.isEmpty) {
      final quick = _downTime != null && DateTime.now().difference(_downTime!).inMilliseconds < 220;
      final settings = AppScope.of(context).settings;
      if (quick && _moved < 10 && settings.tapToClick && !_scrollZone) {
        _client.mouseButton(_maxPointers >= 2 ? 'right' : 'left', 'click');
        if (settings.haptics) HapticFeedback.lightImpact();
      }
      _scrollAccX = _scrollAccY = 0;
    }
    setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return LayoutBuilder(builder: (context, box) {
      const stripW = 64.0;
      return ClipRRect(
        borderRadius: BorderRadius.circular(24),
        child: Row(children: [
          Expanded(
            child: Semantics(
              label: 'Touchpad',
              child: Listener(
                behavior: HitTestBehavior.opaque,
                onPointerDown: (e) => _down(e, false),
                onPointerMove: _move,
                onPointerUp: _up,
                onPointerCancel: _up,
                child: CustomPaint(
                  painter: _PadPainter(
                    points: _scrollZone ? const [] : _points.values.toList(),
                    base: scheme.surfaceContainerHigh,
                    dot: scheme.outlineVariant.withValues(alpha: 0.35),
                    touch: scheme.primary,
                  ),
                  child: Center(
                    child: IgnorePointer(
                      child: Padding(
                        padding: const EdgeInsets.all(8),
                        // shrinks on short pads (phone landscape + keyboard)
                        child: FittedBox(
                          fit: BoxFit.scaleDown,
                          child: Column(mainAxisSize: MainAxisSize.min, children: [
                            Icon(Icons.touch_app_outlined,
                                size: 40, color: scheme.onSurfaceVariant.withValues(alpha: 0.35)),
                            const SizedBox(height: 8),
                            Text(
                              'Drag to move · two fingers to scroll',
                              style: TextStyle(color: scheme.onSurfaceVariant.withValues(alpha: 0.5)),
                            ),
                          ]),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
          Container(width: 1, color: scheme.outlineVariant),
          SizedBox(
            width: stripW,
            child: Semantics(
              label: 'Scroll strip',
              child: Listener(
                behavior: HitTestBehavior.opaque,
                onPointerDown: (e) => _down(e, true),
                onPointerMove: _move,
                onPointerUp: _up,
                onPointerCancel: _up,
                child: Container(
                  color: _scrollZone && _points.isNotEmpty ? scheme.primaryContainer : scheme.surfaceContainerHighest,
                  child: LayoutBuilder(builder: (context, c) {
                    final color = scheme.onSurfaceVariant;
                    if (c.maxHeight < 64) return Center(child: Icon(Icons.swap_vert, color: color));
                    return Padding(
                      padding: const EdgeInsets.symmetric(vertical: 8),
                      child: Column(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                        Icon(Icons.keyboard_arrow_up, color: color),
                        if (c.maxHeight >= 180)
                          RotatedBox(
                            quarterTurns: 3,
                            child: Text('SCROLL', style: TextStyle(letterSpacing: 3, color: color, fontSize: 12)),
                          ),
                        Icon(Icons.keyboard_arrow_down, color: color),
                      ]),
                    );
                  }),
                ),
              ),
            ),
          ),
        ]),
      );
    });
  }
}

class _PadPainter extends CustomPainter {
  _PadPainter({required this.points, required this.base, required this.dot, required this.touch});
  final List<Offset> points;
  final Color base, dot, touch;

  @override
  void paint(Canvas canvas, Size size) {
    canvas.drawRect(Offset.zero & size, Paint()..color = base);
    final p = Paint()..color = dot;
    const step = 28.0;
    for (var x = step / 2; x < size.width; x += step) {
      for (var y = step / 2; y < size.height; y += step) {
        canvas.drawCircle(Offset(x, y), 1.4, p);
      }
    }
    for (final pt in points) {
      canvas.drawCircle(pt, 34, Paint()..color = touch.withValues(alpha: 0.18));
      canvas.drawCircle(pt, 12, Paint()..color = touch.withValues(alpha: 0.55));
    }
  }

  @override
  bool shouldRepaint(_PadPainter old) => true;
}

// ------------------------------------------------------------ mouse buttons
class _MouseButtons extends StatefulWidget {
  const _MouseButtons();

  @override
  State<_MouseButtons> createState() => _MouseButtonsState();
}

class _MouseButtonsState extends State<_MouseButtons> {
  final Set<String> _held = {};
  bool _dragLock = false;

  RemoteClient get _client => AppScope.of(context).client;

  void _press(String b) {
    if (_held.contains(b)) return;
    setState(() => _held.add(b));
    if (b == 'left' && _dragLock) return; // already held by the lock
    _client.mouseButton(b, 'down');
    if (AppScope.of(context).settings.haptics) HapticFeedback.lightImpact();
  }

  void _release(String b) {
    if (!_held.remove(b)) return;
    setState(() {});
    if (b == 'left' && _dragLock) return;
    _client.mouseButton(b, 'up');
  }

  void _toggleLock() {
    setState(() => _dragLock = !_dragLock);
    _client.mouseButton('left', _dragLock ? 'down' : 'up');
    if (AppScope.of(context).settings.haptics) HapticFeedback.mediumImpact();
  }

  @override
  Widget build(BuildContext context) {
    return Row(children: [
      Expanded(flex: 5, child: _HoldButton(label: 'Left', held: _held.contains('left') || _dragLock,
          onDown: () => _press('left'), onUp: () => _release('left'))),
      const SizedBox(width: 10),
      Expanded(flex: 2, child: _HoldButton(label: 'Middle', held: _held.contains('middle'),
          onDown: () => _press('middle'), onUp: () => _release('middle'))),
      const SizedBox(width: 10),
      Expanded(flex: 5, child: _HoldButton(label: 'Right', held: _held.contains('right'),
          onDown: () => _press('right'), onUp: () => _release('right'))),
      const SizedBox(width: 10),
      SizedBox(
        width: 84,
        child: _HoldButton(
          label: _dragLock ? 'Release' : 'Drag lock',
          icon: _dragLock ? Icons.lock : Icons.lock_open,
          held: _dragLock,
          toggle: true,
          onDown: _toggleLock,
          onUp: () {},
        ),
      ),
    ]);
  }
}

class _HoldButton extends StatelessWidget {
  const _HoldButton({
    required this.label,
    required this.held,
    required this.onDown,
    required this.onUp,
    this.icon,
    this.toggle = false,
  });

  final String label;
  final bool held;
  final bool toggle;
  final IconData? icon;
  final VoidCallback onDown;
  final VoidCallback onUp;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      button: true,
      label: '$label mouse button',
      child: Listener(
        onPointerDown: (_) => onDown(),
        onPointerUp: (_) => onUp(),
        onPointerCancel: (_) => onUp(),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 80),
          decoration: BoxDecoration(
            color: held ? scheme.primary : (toggle ? scheme.secondaryContainer : scheme.surfaceContainerHighest),
            borderRadius: BorderRadius.circular(20),
            border: Border.all(color: held ? scheme.primary : scheme.outlineVariant),
          ),
          alignment: Alignment.center,
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            if (icon != null) Icon(icon, color: held ? scheme.onPrimary : scheme.onSecondaryContainer),
            Text(label,
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontWeight: FontWeight.w700,
                  fontSize: icon != null ? 12 : 16,
                  color: held ? scheme.onPrimary : scheme.onSurface,
                )),
          ]),
        ),
      ),
    );
  }
}

// ----------------------------------------------------------------- keyboard
class _KeyboardPanel extends StatefulWidget {
  const _KeyboardPanel({this.compact = false});
  final bool compact;

  @override
  State<_KeyboardPanel> createState() => _KeyboardPanelState();
}

class _KeyboardPanelState extends State<_KeyboardPanel> {
  // The live field always holds this sentinel; what the keyboard adds or
  // deletes around it is forwarded key by key.
  static const _sentinel = '  ';
  final _live = TextEditingController(text: _sentinel);
  final _liveFocus = FocusNode();
  final _compose = TextEditingController();
  bool _enterAfterSend = true;
  final Set<String> _mods = {};
  final Set<String> _locked = {};
  bool _showF = false;

  RemoteClient get _client => AppScope.of(context).client;

  @override
  void dispose() {
    _live.dispose();
    _liveFocus.dispose();
    _compose.dispose();
    super.dispose();
  }

  List<String> _takeMods() {
    final m = _mods.toList();
    setState(() => _mods.removeWhere((x) => !_locked.contains(x)));
    return m;
  }

  void _tapMod(String m) {
    setState(() {
      if (_locked.contains(m)) {
        _locked.remove(m);
        _mods.remove(m);
      } else if (_mods.contains(m)) {
        _mods.remove(m);
      } else {
        _mods.add(m);
      }
    });
  }

  void _lockMod(String m) {
    setState(() {
      _mods.add(m);
      _locked.add(m);
    });
    HapticFeedback.mediumImpact();
  }

  void _key(String k, {List<String> extra = const []}) {
    HapticFeedback.selectionClick();
    _client.key(k, mods: {..._takeMods(), ...extra}.toList());
  }

  void _onLiveChanged(String value) {
    if (value == _sentinel) return;
    if (value.length < _sentinel.length && _sentinel.startsWith(value)) {
      for (var i = 0; i < _sentinel.length - value.length; i++) {
        _client.key('BackSpace', mods: _takeMods());
      }
    } else {
      var added = value.startsWith(_sentinel) ? value.substring(_sentinel.length) : value.trim();
      if (added.isNotEmpty) {
        final mods = _takeMods();
        if (mods.isEmpty) {
          _client.typeText(added);
        } else {
          for (final ch in added.split('')) {
            _client.key(ch, mods: mods);
          }
        }
      }
    }
    _live.value = const TextEditingValue(text: _sentinel, selection: TextSelection.collapsed(offset: 2));
  }

  void _sendCompose() {
    final text = _compose.text;
    if (text.isNotEmpty) _client.typeText(text);
    if (_enterAfterSend) _client.key('Return');
    _compose.clear();
    HapticFeedback.selectionClick();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;

    Widget key(String label, String k, {IconData? icon, int flex = 1, List<String> extra = const [], String? tip}) =>
        Expanded(
          flex: flex,
          child: Padding(
            padding: const EdgeInsets.all(3),
            child: Tooltip(
              message: tip ?? label,
              child: Material(
                color: scheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
                child: InkWell(
                  borderRadius: BorderRadius.circular(10),
                  onTap: () => _key(k, extra: extra),
                  child: SizedBox(
                    height: 44,
                    child: Center(
                      child: icon != null
                          ? Icon(icon, size: 20)
                          : Text(label, style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
                    ),
                  ),
                ),
              ),
            ),
          ),
        );

    Widget mod(String label, String m) {
      final on = _mods.contains(m);
      final locked = _locked.contains(m);
      return Expanded(
        child: Padding(
          padding: const EdgeInsets.all(3),
          child: Tooltip(
            message: 'Tap: next key only · long press: lock',
            child: Material(
              color: on ? scheme.primary : scheme.secondaryContainer,
              borderRadius: BorderRadius.circular(10),
              child: InkWell(
                borderRadius: BorderRadius.circular(10),
                onTap: () => _tapMod(m),
                onLongPress: () => _lockMod(m),
                child: SizedBox(
                  height: 44,
                  child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                    if (locked) Icon(Icons.lock, size: 12, color: scheme.onPrimary),
                    Text(label,
                        style: TextStyle(
                            fontWeight: FontWeight.w700, color: on ? scheme.onPrimary : scheme.onSecondaryContainer)),
                  ]),
                ),
              ),
            ),
          ),
        ),
      );
    }

    Widget shortcut(String label, String k, List<String> mods, IconData icon) => ActionChip(
          avatar: Icon(icon, size: 18),
          label: Text(label),
          onPressed: () {
            HapticFeedback.selectionClick();
            _client.key(k, mods: mods);
          },
        );

    final liveField = TextField(
      controller: _live,
      focusNode: _liveFocus,
      keyboardType: TextInputType.visiblePassword,
      autocorrect: false,
      enableSuggestions: false,
      enableIMEPersonalizedLearning: false,
      textInputAction: TextInputAction.send,
      onChanged: _onLiveChanged,
      onSubmitted: (_) {
        _client.key('Return', mods: _takeMods());
        _liveFocus.requestFocus();
      },
      onEditingComplete: () {},
      decoration: InputDecoration(
        prefixIcon: const Icon(Icons.keyboard),
        // The field always holds a sentinel, so a hint would never show: use a label.
        labelText: 'Live typing – tap here, every key goes straight to the Pi',
        floatingLabelBehavior: FloatingLabelBehavior.always,
        filled: true,
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(14), borderSide: BorderSide.none),
      ),
    );

    final composeField = Row(children: [
      Expanded(
        child: TextField(
          controller: _compose,
          minLines: 1,
          maxLines: 3,
          textInputAction: TextInputAction.send,
          onSubmitted: (_) => _sendCompose(),
          decoration: InputDecoration(
            hintText: 'Compose text (any language) and send',
            isDense: true,
            border: OutlineInputBorder(borderRadius: BorderRadius.circular(14)),
          ),
        ),
      ),
      const SizedBox(width: 8),
      IconButton.filled(tooltip: 'Type on the Pi', onPressed: _sendCompose, icon: const Icon(Icons.send)),
    ]);

    final keys = Column(mainAxisSize: MainAxisSize.min, children: [
      Row(children: [mod('Ctrl', 'ctrl'), mod('Alt', 'alt'), mod('Shift', 'shift'), mod('Super', 'super')]),
      Row(children: [
        key('Esc', 'Escape'),
        key('Tab', 'Tab'),
        key('', 'BackSpace', icon: Icons.backspace_outlined, tip: 'Backspace'),
        key('Del', 'Delete'),
        key('Enter', 'Return', icon: Icons.keyboard_return, tip: 'Enter'),
      ]),
      Row(children: [
        key('Home', 'Home'),
        key('', 'Up', icon: Icons.keyboard_arrow_up, tip: 'Up'),
        key('End', 'End'),
        key('PgUp', 'Page_Up'),
        key('Ins', 'Insert'),
      ]),
      Row(children: [
        key('', 'Left', icon: Icons.keyboard_arrow_left, tip: 'Left'),
        key('', 'Down', icon: Icons.keyboard_arrow_down, tip: 'Down'),
        key('', 'Right', icon: Icons.keyboard_arrow_right, tip: 'Right'),
        key('PgDn', 'Page_Down'),
        key('Space', 'space'),
      ]),
      if (_showF) ...[
        Row(children: [for (var i = 1; i <= 6; i++) key('F$i', 'F$i')]),
        Row(children: [for (var i = 7; i <= 12; i++) key('F$i', 'F$i')]),
      ],
      Align(
        alignment: Alignment.centerLeft,
        child: TextButton.icon(
          onPressed: () => setState(() => _showF = !_showF),
          icon: Icon(_showF ? Icons.expand_less : Icons.expand_more),
          label: Text(_showF ? 'Hide F1–F12' : 'F1–F12'),
        ),
      ),
    ]);

    final shortcuts = Wrap(spacing: 8, runSpacing: 4, children: [
      shortcut('Copy', 'c', ['ctrl'], Icons.copy),
      shortcut('Paste', 'v', ['ctrl'], Icons.paste),
      shortcut('Cut', 'x', ['ctrl'], Icons.content_cut),
      shortcut('Undo', 'z', ['ctrl'], Icons.undo),
      shortcut('Select all', 'a', ['ctrl'], Icons.select_all),
      shortcut('Save', 's', ['ctrl'], Icons.save_outlined),
      shortcut('Switch window', 'Tab', ['alt'], Icons.tab),
      shortcut('Close window', 'F4', ['alt'], Icons.close),
      shortcut('App menu', 'Super_L', [], Icons.apps),
      shortcut('Terminal', 't', ['ctrl', 'alt'], Icons.terminal),
      shortcut('Vol −', 'XF86AudioLowerVolume', [], Icons.volume_down),
      shortcut('Vol +', 'XF86AudioRaiseVolume', [], Icons.volume_up),
      shortcut('Mute', 'XF86AudioMute', [], Icons.volume_off),
    ]);

    return Card(
      margin: EdgeInsets.zero,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Row(children: [
            Icon(Icons.keyboard_alt_outlined, size: 18, color: scheme.primary),
            const SizedBox(width: 8),
            Text('KEYBOARD',
                style: theme.textTheme.labelMedium?.copyWith(letterSpacing: 1.1, fontWeight: FontWeight.w600)),
          ]),
          const SizedBox(height: 10),
          liveField,
          const SizedBox(height: 10),
          composeField,
          Row(children: [
            Checkbox(value: _enterAfterSend, onChanged: (v) => setState(() => _enterAfterSend = v ?? false)),
            const Flexible(child: Text('Press Enter after sending', overflow: TextOverflow.ellipsis)),
          ]),
          keys,
          const SizedBox(height: 4),
          Text('SHORTCUTS',
              style: theme.textTheme.labelMedium?.copyWith(letterSpacing: 1.1, fontWeight: FontWeight.w600)),
          const SizedBox(height: 8),
          shortcuts,
        ],
      ),
    );
  }
}
