import 'dart:async';

import 'package:flutter/material.dart';

import '../app_scope.dart';
import '../bt/bluetooth.dart';
import '../remote_client.dart';
import 'dashboard_page.dart';
import 'recorder/recorder_page.dart';
import 'remote_page.dart';
import 'settings_page.dart';
import 'terminal_page.dart';
import 'wifi_page.dart';

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _Dest {
  const _Dest(this.label, this.icon, this.selectedIcon);
  final String label;
  final IconData icon;
  final IconData selectedIcon;
}

const _dests = [
  _Dest('Dashboard', Icons.speed_outlined, Icons.speed),
  _Dest('Recorder', Icons.videocam_outlined, Icons.videocam),
  _Dest('Wi-Fi', Icons.wifi_outlined, Icons.wifi),
  _Dest('Terminal', Icons.terminal_outlined, Icons.terminal),
  _Dest('Remote', Icons.mouse_outlined, Icons.mouse),
];

class _HomePageState extends State<HomePage> {
  int _index = 0;

  void _select(int i) {
    setState(() => _index = i);
    // Keep the screen awake while it shows the live picture, a terminal or the touchpad.
    AppScope.of(context).client.bt.keepScreenOn(i == 1 || i >= 3).catchError((_) {});
    FocusManager.instance.primaryFocus?.unfocus();
  }

  @override
  void dispose() {
    Bluetooth.instance.keepScreenOn(false).catchError((_) {});
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final client = AppScope.of(context).client;
    final wide = MediaQuery.sizeOf(context).width >= 840;
    final pages = [
      const DashboardPage(),
      RecorderPage(active: _index == 1),
      const WifiPage(),
      TerminalPage(active: _index == 3),
      RemotePage(active: _index == 4),
    ];
    final body = Column(children: [
      _LinkBanner(client: client),
      // hidden tabs keep their state but stop animating (battery)
      Expanded(
        child: IndexedStack(index: _index, children: [
          for (var i = 0; i < pages.length; i++) TickerMode(enabled: i == _index, child: pages[i]),
        ]),
      ),
    ]);
    return ListenableBuilder(
      listenable: client,
      builder: (context, _) => PopScope(
        // Back on the main screen backgrounds the app instead of closing it, so
        // the Bluetooth link (and the shells) stay up.
        canPop: false,
        onPopInvokedWithResult: (didPop, _) {
          if (!didPop) Bluetooth.instance.moveToBack().catchError((_) {});
        },
        child: Scaffold(
        appBar: AppBar(
          titleSpacing: wide ? 24 : null,
          title: Row(children: [
            _LinkDot(state: client.state),
            const SizedBox(width: 10),
            Flexible(child: Text(client.hostname, overflow: TextOverflow.ellipsis)),
            if (client.latencyMs != null && client.isConnected) ...[
              const SizedBox(width: 10),
              Text('${client.latencyMs} ms',
                  style: Theme.of(context).textTheme.labelMedium?.copyWith(
                        color: Theme.of(context).colorScheme.onSurfaceVariant,
                      )),
            ],
          ]),
          actions: [
            IconButton(
              tooltip: 'Settings',
              icon: const Icon(Icons.settings_outlined),
              onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const SettingsPage())),
            ),
            IconButton(
              tooltip: 'Disconnect',
              icon: const Icon(Icons.link_off),
              onPressed: () => client.disconnect(),
            ),
            const SizedBox(width: 4),
          ],
        ),
        body: wide
            ? Row(children: [
                NavigationRail(
                  selectedIndex: _index,
                  onDestinationSelected: _select,
                  labelType: NavigationRailLabelType.all,
                  destinations: [
                    for (final d in _dests)
                      NavigationRailDestination(
                          icon: Icon(d.icon), selectedIcon: Icon(d.selectedIcon), label: Text(d.label)),
                  ],
                ),
                const VerticalDivider(width: 1),
                Expanded(child: body),
              ])
            : body,
        bottomNavigationBar: wide
            ? null
            : NavigationBar(
                selectedIndex: _index,
                onDestinationSelected: _select,
                destinations: [
                  for (final d in _dests)
                    NavigationDestination(icon: Icon(d.icon), selectedIcon: Icon(d.selectedIcon), label: d.label),
                ],
              ),
        ),
      ),
    );
  }
}

class _LinkDot extends StatelessWidget {
  const _LinkDot({required this.state});
  final LinkState state;

  @override
  Widget build(BuildContext context) {
    final color = switch (state) {
      LinkState.connected => const Color(0xFF4CAF50),
      LinkState.connecting || LinkState.reconnecting => const Color(0xFFFFB300),
      _ => const Color(0xFFEF5350),
    };
    return Container(
      width: 10,
      height: 10,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle, boxShadow: [
        BoxShadow(color: color.withValues(alpha: 0.6), blurRadius: 6),
      ]),
    );
  }
}

/// Shown while the link is down; counts down to the next reconnect attempt.
class _LinkBanner extends StatefulWidget {
  const _LinkBanner({required this.client});
  final RemoteClient client;

  @override
  State<_LinkBanner> createState() => _LinkBannerState();
}

class _LinkBannerState extends State<_LinkBanner> {
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && widget.client.state == LinkState.reconnecting) setState(() {});
    });
  }

  @override
  void dispose() {
    _tick?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: widget.client,
      builder: (context, _) {
        final c = widget.client;
        if (c.state != LinkState.reconnecting && c.state != LinkState.connecting) return const SizedBox.shrink();
        final scheme = Theme.of(context).colorScheme;
        final wait = c.nextRetryAt?.difference(DateTime.now()).inSeconds;
        final text = c.state == LinkState.connecting || wait == null || wait <= 0
            ? 'Reconnecting to ${c.device?.label}…'
            : 'Connection lost. Retrying in ${wait}s (attempt ${c.reconnectAttempt})';
        return Material(
          color: scheme.tertiaryContainer,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Row(children: [
              const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(text, style: TextStyle(color: scheme.onTertiaryContainer, fontWeight: FontWeight.w600)),
                  if (c.error != null)
                    Text(c.error!,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(color: scheme.onTertiaryContainer, fontSize: 12)),
                ]),
              ),
              TextButton(onPressed: c.retryNow, child: const Text('Retry now')),
            ]),
          ),
        );
      },
    );
  }
}
