import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:xterm/xterm.dart';

import '../app_scope.dart';
import '../terminal_hub.dart';
import 'widgets.dart';

class TerminalPage extends StatefulWidget {
  const TerminalPage({super.key, required this.active});
  final bool active;

  @override
  State<TerminalPage> createState() => _TerminalPageState();
}

class _TerminalPageState extends State<TerminalPage> {
  final FocusNode _focus = FocusNode();
  bool _autoOpened = false;

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
  void didUpdateWidget(TerminalPage old) {
    super.didUpdateWidget(old);
    if (widget.active && !old.active) _ensureShell();
  }

  void _ensureShell() {
    final client = AppScope.of(context).client;
    if (!_autoOpened && client.isConnected && client.terminals.sessions.isEmpty) {
      _autoOpened = true;
      client.terminals.open();
    }
  }

  @override
  void dispose() {
    _focus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scope = AppScope.of(context);
    final client = scope.client;
    final hub = client.terminals;
    return ListenableBuilder(
      listenable: Listenable.merge([hub, client, scope.settings]),
      builder: (context, _) {
        final session = hub.current;
        return Column(children: [
          _TabsBar(
            hub: hub,
            canOpen: client.isConnected,
            snippets: _snippets,
            onSnippet: (cmd) => session?.sendText(cmd),
            fontSize: scope.settings.terminalFontSize,
            onFont: (d) => scope.settings.terminalFontSize = (scope.settings.terminalFontSize + d).clamp(9, 24),
          ),
          Expanded(
            child: session == null
                ? EmptyState(
                    icon: Icons.terminal,
                    text: client.isConnected ? 'No shell open' : 'Connect to open a shell',
                    action: client.isConnected
                        ? FilledButton.icon(
                            onPressed: hub.open, icon: const Icon(Icons.add), label: const Text('Open shell'))
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
                          focusNode: _focus,
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
      },
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
  });

  final TerminalHub hub;
  final bool canOpen;
  final Map<String, String> snippets;
  final ValueChanged<String> onSnippet;
  final double fontSize;
  final ValueChanged<double> onFont;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.surfaceContainer,
      child: SizedBox(
        height: 48,
        child: Row(children: [
          Expanded(
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
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

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    Widget btn(String label, VoidCallback onTap, {bool on = false, String? tooltip, IconData? icon}) => Padding(
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
                      ? Icon(icon, size: 20, color: on ? scheme.onPrimary : scheme.onSurface)
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
              btn('', () async {
                final data = await Clipboard.getData(Clipboard.kTextPlain);
                if (data?.text != null) session.terminal.paste(data!.text!);
              }, icon: Icons.content_paste, tooltip: 'Paste'),
            ],
          ),
        ),
      ),
    );
  }
}
