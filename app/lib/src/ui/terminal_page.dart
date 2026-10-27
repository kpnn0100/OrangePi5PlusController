import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:xterm/xterm.dart';

import '../app_scope.dart';
import '../remote_client.dart';
import '../slot_links.dart';
import '../terminal_hub.dart';
import 'widgets.dart';

/// Shortcuts a terminal keeps (TERM-07): copy / paste with Shift like a desktop terminal, so
/// Ctrl+A, Ctrl+C, Ctrl+V still reach the shell (start of line, interrupt, literal next).
final Map<ShortcutActivator, Intent> terminalShortcuts = {
  const SingleActivator(LogicalKeyboardKey.keyC, control: true, shift: true): CopySelectionTextIntent.copy,
  const SingleActivator(LogicalKeyboardKey.keyV, control: true, shift: true):
      const PasteTextIntent(SelectionChangedCause.keyboard),
  const SingleActivator(LogicalKeyboardKey.insert, control: true): CopySelectionTextIntent.copy,
  const SingleActivator(LogicalKeyboardKey.insert, shift: true): const PasteTextIntent(SelectionChangedCause.keyboard),
};

class TerminalPage extends StatefulWidget {
  const TerminalPage({super.key, required this.active});
  final bool active;

  @override
  State<TerminalPage> createState() => _TerminalPageState();
}

class _TerminalPageState extends State<TerminalPage> {
  final FocusNode _focus = FocusNode();
  bool _autoOpened = false;

  /// The slot whose shells are shown (TERM-06); null = the Bluetooth link's own.
  String? _slot;

  static const _snippets = <String, String>{
    'IP addresses': 'ip -br addr\n',
    'System monitor (htop)': 'htop\n',
    'Disk usage': 'df -h\n',
    'Memory': 'free -h\n',
    'Wi-Fi networks': 'nmcli dev wifi list\n',
    'Arstro service status': 'arstro-remote status\n',
    'Open Bluetooth pairing (10 min)': 'arstro-remote pair 600\n',
    'Follow system log': 'journalctl -f\n',
  };

  @override
  void initState() {
    super.initState();
    HardwareKeyboard.instance.addHandler(_onKey);
  }

  @override
  void didUpdateWidget(TerminalPage old) {
    super.didUpdateWidget(old);
    if (widget.active && !old.active) {
      // after this frame: both notify listeners all over the tree (the slots, the media link)
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        _ensureShell();
        AppScope.of(context).client.slots.refresh();
      });
    }
  }

  void _ensureShell() {
    final client = AppScope.of(context).client;
    if (!_autoOpened && client.isConnected && client.terminals.sessions.isEmpty) {
      _autoOpened = true;
      client.terminals.open();
    }
  }

  /// F11 enters / leaves full screen while the terminal is shown; the shell never gets it.
  bool _onKey(KeyEvent e) {
    if (!widget.active || e.logicalKey != LogicalKeyboardKey.f11 || !mounted) return false;
    if (e is KeyDownEvent) {
      final full = AppScope.of(context).settings.immersive;
      full.value = !full.value;
    }
    return true;
  }

  @override
  void dispose() {
    HardwareKeyboard.instance.removeHandler(_onKey);
    _focus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scope = AppScope.of(context);
    final client = scope.client;
    final links = client.slots;
    return ListenableBuilder(
      listenable: Listenable.merge([links, client, scope.settings, scope.settings.immersive]),
      builder: (context, _) {
        final entries = links.entries;
        final entry = entries.firstWhere((e) => e.slot == _slot, orElse: () => entries.first);
        final c = entry.client;
        final full = scope.settings.immersive.value;
        return Column(children: [
          if (entries.length > 1 && !full)
            _SlotBar(entries: entries, selected: entry, onSelect: (e) => setState(() => _slot = e.slot)),
          Expanded(
            child: c == null
                ? EmptyState(
                    icon: Icons.wifi_off,
                    text: entry.note ?? '${entry.label} is not reachable',
                    action: OutlinedButton.icon(
                        onPressed: links.refresh, icon: const Icon(Icons.refresh), label: const Text('Try again')),
                  )
                : ListenableBuilder(
                    listenable: Listenable.merge([c, c.terminals]),
                    builder: (context, _) => _shells(context, scope, entry, c, full),
                  ),
          ),
        ]);
      },
    );
  }

  Widget _shells(BuildContext context, AppScope scope, SlotEntry entry, RemoteClient client, bool full) {
    final hub = client.terminals;
    final session = hub.current;
    final connecting = !client.isConnected && !entry.self;
    return Column(children: [
      // TERM-08: the tabs stay in full screen too (a slim bar: switch, + new shell, leave)
      _TabsBar(
        hub: hub,
        canOpen: client.isConnected,
        snippets: _snippets,
        onSnippet: (cmd) => session?.sendText(cmd),
        fontSize: scope.settings.terminalFontSize,
        onFont: (d) => scope.settings.terminalFontSize = (scope.settings.terminalFontSize + d).clamp(9, 24),
        full: full,
        onFull: full
            ? () => scope.settings.immersive.value = false
            : session == null
                ? null
                : () => scope.settings.immersive.value = true,
      ),
      Expanded(
        child: session == null
            ? EmptyState(
                icon: connecting ? Icons.wifi_find : Icons.terminal,
                text: connecting
                    ? (client.error != null ? '${entry.label}: ${client.error}' : 'Connecting to ${entry.label} over Wi-Fi…')
                    : client.isConnected
                        ? 'No shell open on ${entry.label}'
                        : 'Connect to open a shell',
                action: client.isConnected
                    ? FilledButton.icon(onPressed: hub.open, icon: const Icon(Icons.add), label: const Text('Open shell'))
                    : null,
              )
            : Stack(children: [
                Positioned.fill(
                  child: Container(
                    color: TerminalThemes.defaultTheme.background,
                    padding: const EdgeInsets.all(4),
                    child: TerminalView(
                      session.terminal,
                      key: ObjectKey(session),
                      controller: session.controller,
                      focusNode: _focus,
                      shortcuts: terminalShortcuts,
                      textStyle: TerminalStyle(fontSize: scope.settings.terminalFontSize),
                      keyboardType: TextInputType.visiblePassword,
                      deleteDetection: true,
                      readOnly: session.status != TermStatus.live,
                    ),
                  ),
                ),
                if (session.status == TermStatus.ended)
                  Positioned(
                    right: 16,
                    bottom: 16,
                    child: FilledButton.icon(
                      onPressed: client.isConnected ? () => hub.restart(session) : null,
                      icon: const Icon(Icons.restart_alt),
                      label: const Text('Restart shell'),
                    ),
                  ),
                if (session.status == TermStatus.detached)
                  const Positioned(
                    right: 16,
                    top: 12,
                    child: Chip(avatar: Icon(Icons.link_off, size: 18), label: Text('Waiting for link…')),
                  ),
              ]),
      ),
      if (session != null)
        _ExtraKeys(
          session: session,
          onKeyboard: () {
            if (_focus.hasFocus) {
              _focus.unfocus();
            } else {
              _focus.requestFocus();
              SystemChannels.textInput.invokeMethod('TextInput.show');
            }
          },
        ),
    ]);
  }
}

/// TERM-06: which slot's shells are shown - the Bluetooth link's own, or another slot over Wi-Fi.
class _SlotBar extends StatelessWidget {
  const _SlotBar({required this.entries, required this.selected, required this.onSelect});
  final List<SlotEntry> entries;
  final SlotEntry selected;
  final ValueChanged<SlotEntry> onSelect;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    Color dot(SlotEntry e) {
      final c = e.client;
      if (c == null) return scheme.outline;
      if (c.isConnected) return const Color(0xFF4CAF50);
      return c.state == LinkState.failed ? const Color(0xFFEF5350) : const Color(0xFFFFB300);
    }

    return Material(
      color: scheme.surfaceContainerLow,
      child: SizedBox(
        height: 44,
        child: ListView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          children: [
            for (final e in entries)
              Padding(
                padding: const EdgeInsets.only(right: 6),
                child: ChoiceChip(
                  selected: identical(e, selected) || e.slot == selected.slot,
                  showCheckmark: false,
                  avatar: Icon(Icons.circle, size: 10, color: dot(e)),
                  label: Text('${e.label} · ${e.via}'),
                  tooltip: e.note ?? (e.self ? 'This phone\'s Bluetooth link' : 'Reached over Wi-Fi'),
                  onSelected: (_) => onSelect(e),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _TabsBar extends StatelessWidget {
  const _TabsBar({
    required this.hub,
    required this.canOpen,
    required this.snippets,
    required this.onSnippet,
    required this.fontSize,
    required this.onFont,
    required this.onFull,
    this.full = false,
  });

  final TerminalHub hub;
  final bool canOpen;
  final Map<String, String> snippets;
  final ValueChanged<String> onSnippet;
  final double fontSize;
  final ValueChanged<double> onFont;
  final VoidCallback? onFull;

  /// Full screen: a slim bar on the terminal's own background, under the status bar - the tabs,
  /// + and the way out; the quick commands and text size wait for the normal view.
  final bool full;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: full ? TerminalThemes.defaultTheme.background : scheme.surfaceContainer,
      child: Container(
        padding: EdgeInsets.only(top: full ? MediaQuery.paddingOf(context).top : 0),
        height: (full ? 40 : 48) + (full ? MediaQuery.paddingOf(context).top : 0),
        child: Row(children: [
          Expanded(
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: EdgeInsets.symmetric(horizontal: 8, vertical: full ? 3 : 6),
              children: [
                for (var i = 0; i < hub.sessions.length; i++)
                  Padding(
                    padding: const EdgeInsets.only(right: 6),
                    child: InputChip(
                      selected: i == hub.active,
                      showCheckmark: false,
                      avatar: Icon(_statusIcon(hub.sessions[i].status), size: 16,
                          color: _statusColor(hub.sessions[i].status)),
                      label: Text(hub.sessions[i].label, overflow: TextOverflow.ellipsis),
                      onPressed: () => hub.select(i),
                      onDeleted: () => hub.close(hub.sessions[i]),
                      deleteButtonTooltipMessage: 'Close shell',
                    ),
                  ),
                IconButton(
                  tooltip: 'New shell',
                  onPressed: canOpen ? hub.open : null,
                  icon: const Icon(Icons.add),
                ),
              ],
            ),
          ),
          if (!full) ...[
          PopupMenuButton<String>(
            tooltip: 'Quick commands',
            icon: const Icon(Icons.bolt),
            enabled: hub.current?.status == TermStatus.live,
            onSelected: onSnippet,
            itemBuilder: (_) => [
              for (final e in snippets.entries) PopupMenuItem(value: e.value, child: Text(e.key)),
            ],
          ),
          IconButton(tooltip: 'Smaller text', onPressed: () => onFont(-1), icon: const Icon(Icons.text_decrease)),
          IconButton(tooltip: 'Larger text', onPressed: () => onFont(1), icon: const Icon(Icons.text_increase)),
          ],
          full
              ? IconButton(tooltip: 'Leave full screen (F11)', onPressed: onFull, icon: const Icon(Icons.fullscreen_exit))
              : IconButton(tooltip: 'Full screen (F11)', onPressed: onFull, icon: const Icon(Icons.fullscreen)),
          const SizedBox(width: 4),
        ]),
      ),
    );
  }

  IconData _statusIcon(TermStatus s) => switch (s) {
        TermStatus.live => Icons.circle,
        TermStatus.opening => Icons.more_horiz,
        TermStatus.detached => Icons.link_off,
        TermStatus.ended => Icons.stop_circle_outlined,
      };

  Color _statusColor(TermStatus s) => switch (s) {
        TermStatus.live => const Color(0xFF4CAF50),
        TermStatus.opening || TermStatus.detached => const Color(0xFFFFB300),
        TermStatus.ended => const Color(0xFF9E9E9E),
      };
}

/// Keys a phone keyboard lacks, plus sticky Ctrl/Alt.
class _ExtraKeys extends StatelessWidget {
  const _ExtraKeys({required this.session, required this.onKeyboard});
  final TermSession session;
  final VoidCallback onKeyboard;

  void _key(TerminalKey k) {
    HapticFeedback.selectionClick();
    final ctrl = session.ctrl, alt = session.alt;
    session.ctrl = false;
    session.alt = false;
    session.terminal.keyInput(k, ctrl: ctrl, alt: alt);
    session.hub.refresh();
  }

  void _text(String s) {
    HapticFeedback.selectionClick();
    session.terminal.textInput(s);
  }

  /// TERM-07: what was selected (long-press, then drag) goes to the phone's clipboard.
  Future<void> _copy(BuildContext context) async {
    final sel = session.controller.selection;
    if (sel == null) return;
    final text = session.terminal.buffer.getText(sel).trimRight();
    await Clipboard.setData(ClipboardData(text: text));
    session.controller.clearSelection();
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text('Copied ${text.length} character${text.length == 1 ? '' : 's'}'),
          duration: const Duration(milliseconds: 1400)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    Widget btn(String label, VoidCallback? onTap, {bool on = false, String? tooltip, IconData? icon}) => Padding(
          padding: const EdgeInsets.symmetric(horizontal: 2),
          child: Tooltip(
            message: tooltip ?? label,
            child: Material(
              color: on ? scheme.primary : scheme.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(8),
              child: InkWell(
                borderRadius: BorderRadius.circular(8),
                onTap: onTap,
                child: Container(
                  constraints: const BoxConstraints(minWidth: 46),
                  height: 40,
                  alignment: Alignment.center,
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                  child: icon != null
                      ? Icon(icon,
                          size: 20,
                          color: on
                              ? scheme.onPrimary
                              : onTap == null
                                  ? scheme.onSurface.withValues(alpha: 0.35)
                                  : scheme.onSurface)
                      : Text(label,
                          style: TextStyle(
                            fontWeight: FontWeight.w600,
                            color: on ? scheme.onPrimary : scheme.onSurface,
                          )),
                ),
              ),
            ),
          ),
        );
    return Material(
      color: scheme.surfaceContainer,
      child: SafeArea(
        top: false,
        child: SizedBox(
          height: 52,
          child: ListView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 6),
            children: [
              btn('', onKeyboard, icon: Icons.keyboard, tooltip: 'Show / hide keyboard'),
              ListenableBuilder(
                listenable: session.controller,
                builder: (context, _) => btn('', session.controller.selection == null ? null : () => _copy(context),
                    icon: Icons.content_copy, tooltip: 'Copy the selection (Ctrl+Shift+C)'),
              ),
              btn('', () async {
                final data = await Clipboard.getData(Clipboard.kTextPlain);
                if (data?.text != null) session.terminal.paste(data!.text!);
              }, icon: Icons.content_paste, tooltip: 'Paste (Ctrl+Shift+V)'),
              btn('ESC', () => _key(TerminalKey.escape)),
              btn('TAB', () => _key(TerminalKey.tab)),
              btn('CTRL', () {
                session.ctrl = !session.ctrl;
                session.hub.refresh();
              }, on: session.ctrl, tooltip: 'Ctrl (applies to the next key)'),
              btn('ALT', () {
                session.alt = !session.alt;
                session.hub.refresh();
              }, on: session.alt, tooltip: 'Alt (applies to the next key)'),
              btn('', () => _key(TerminalKey.arrowLeft), icon: Icons.keyboard_arrow_left, tooltip: 'Left'),
              btn('', () => _key(TerminalKey.arrowUp), icon: Icons.keyboard_arrow_up, tooltip: 'Up'),
              btn('', () => _key(TerminalKey.arrowDown), icon: Icons.keyboard_arrow_down, tooltip: 'Down'),
              btn('', () => _key(TerminalKey.arrowRight), icon: Icons.keyboard_arrow_right, tooltip: 'Right'),
              btn('^C', () => _text('\x03'), tooltip: 'Interrupt (Ctrl+C)'),
              btn('^D', () => _text('\x04'), tooltip: 'End of input (Ctrl+D)'),
              btn('^Z', () => _text('\x1a'), tooltip: 'Suspend (Ctrl+Z)'),
              btn('HOME', () => _key(TerminalKey.home)),
              btn('END', () => _key(TerminalKey.end)),
              btn('PGUP', () => _key(TerminalKey.pageUp)),
              btn('PGDN', () => _key(TerminalKey.pageDown)),
              for (final c in const ['|', '/', '-', '~', '_', '&', ';', '>', '*'])
                btn(c, () => _text(c)),
            ],
          ),
        ),
      ),
    );
  }
}
