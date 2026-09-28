import 'dart:async';

import 'package:flutter/material.dart';

import '../app_scope.dart';
import '../bt/bluetooth.dart';
import '../remote_client.dart';
import 'settings_page.dart';

class ConnectPage extends StatefulWidget {
  const ConnectPage({super.key, this.allowAutoConnect = true});
  final bool allowAutoConnect;

  @override
  State<ConnectPage> createState() => _ConnectPageState();
}

class _ConnectPageState extends State<ConnectPage> {
  final Bluetooth bt = Bluetooth.instance;
  BtState? _bt;
  List<BtDevice> _bonded = [];
  final Map<String, BtDevice> _found = {};
  bool _scanning = false;
  bool _permissionDenied = false;
  StreamSubscription? _sub;
  static bool _autoConnectTried = false;

  RemoteClient get client => AppScope.of(context).client;

  @override
  void initState() {
    super.initState();
    _sub = bt.events.listen(_onEvent);
    WidgetsBinding.instance.addPostFrameCallback((_) => _init());
  }

  @override
  void dispose() {
    _sub?.cancel();
    if (_scanning) bt.cancelDiscovery();
    super.dispose();
  }

  Future<void> _init() async {
    var st = await bt.state();
    if (st.supported && !st.permissions) {
      final ok = await bt.requestPermissions();
      _permissionDenied = !ok;
      st = await bt.state();
    }
    if (!mounted) return;
    setState(() => _bt = st);
    if (st.enabled && st.permissions) {
      await _loadBonded();
      _maybeAutoConnect();
    }
  }

  void _maybeAutoConnect() {
    final scope = AppScope.of(context);
    final last = scope.client.lastDevice;
    if (_autoConnectTried || !widget.allowAutoConnect || !scope.settings.autoConnect || last == null) return;
    _autoConnectTried = true;
    final dev = _bonded.firstWhere((d) => d.address == last.address, orElse: () => last);
    _connect(dev);
  }

  Future<void> _loadBonded() async {
    final list = await bt.bondedDevices();
    list.sort((a, b) {
      if (a.isArstro != b.isArstro) return a.isArstro ? -1 : 1;
      return a.label.toLowerCase().compareTo(b.label.toLowerCase());
    });
    if (mounted) setState(() => _bonded = list);
  }

  void _onEvent(Map<dynamic, dynamic> ev) {
    if (!mounted) return;
    switch (ev['event']) {
      case 'found':
        final d = BtDevice.fromMap(ev['device'] as Map);
        if (d.bonded) return;
        setState(() {
          final old = _found[d.address];
          _found[d.address] = old == null ? d : d.copyWith(name: d.name ?? old.name, rssi: d.rssi ?? old.rssi);
        });
      case 'discovery':
        setState(() => _scanning = ev['state'] == 'started');
      case 'bond':
        _loadBonded();
        if (ev['state'] == 'bonded') setState(() => _found.remove(ev['address']));
      case 'adapter':
        bt.state().then((s) {
          if (!mounted) return;
          setState(() => _bt = s);
          if (s.enabled) _loadBonded();
        });
    }
  }

  Future<void> _scan() async {
    if (_scanning) {
      await bt.cancelDiscovery();
      return;
    }
    setState(() => _found.clear());
    final ok = await bt.startDiscovery();
    if (!ok && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Could not start scanning')));
    }
  }

  Future<void> _connect(BtDevice d) async {
    if (_scanning) await bt.cancelDiscovery();
    await client.connect(d);
  }

  Future<void> _connectByAddress() async {
    final addr = await showDialog<String>(context: context, builder: (_) => const _AddressDialog());
    if (addr != null) _connect(BtDevice(address: addr));
  }

  Future<void> _unpair(BtDevice d) async {
    final ok = await bt.unpair(d.address);
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ok ? 'Forgot ${d.label}' : 'Could not remove pairing')));
    _loadBonded();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListenableBuilder(
      listenable: client,
      builder: (context, _) {
        final busy = client.state == LinkState.connecting;
        return Scaffold(
          appBar: AppBar(
            title: const Text('Arstro Remote'),
            actions: [
              IconButton(
                tooltip: 'Settings',
                icon: const Icon(Icons.settings_outlined),
                onPressed: () =>
                    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const SettingsPage())),
              ),
            ],
          ),
          body: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 760),
              child: RefreshIndicator(
                onRefresh: _loadBonded,
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
                  children: [
                    _Hero(theme: theme),
                    const SizedBox(height: 16),
                    if (client.state == LinkState.connecting || client.state == LinkState.failed)
                      _StatusCard(client: client, onRetry: client.retryNow),
                    ..._body(theme, busy),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  List<Widget> _body(ThemeData theme, bool busy) {
    final st = _bt;
    if (st == null) return [const Center(child: Padding(padding: EdgeInsets.all(32), child: CircularProgressIndicator()))];
    if (!st.supported) {
      return [const _InfoCard(icon: Icons.bluetooth_disabled, text: 'This device has no Bluetooth.')];
    }
    if (!st.permissions) {
      return [
        _InfoCard(
          icon: Icons.lock_outline,
          text: _permissionDenied
              ? 'Bluetooth permission was denied. Allow "Nearby devices" to find and connect to your Pi.'
              : 'Bluetooth permission is needed to find and connect to your Pi.',
          action: FilledButton.icon(
            onPressed: _init,
            icon: const Icon(Icons.lock_open),
            label: const Text('Grant permission'),
          ),
        ),
      ];
    }
    if (!st.enabled) {
      return [
        _InfoCard(
          icon: Icons.bluetooth_disabled,
          text: 'Bluetooth is off.',
          action: FilledButton.icon(
            onPressed: () async {
              await bt.requestEnable();
              _init();
            },
            icon: const Icon(Icons.bluetooth),
            label: const Text('Turn on Bluetooth'),
          ),
        ),
      ];
    }
    final found = _found.values.toList()
      ..sort((a, b) {
        if (a.isArstro != b.isArstro) return a.isArstro ? -1 : 1;
        if ((a.name == null) != (b.name == null)) return a.name == null ? 1 : -1;
        return (b.rssi ?? -200).compareTo(a.rssi ?? -200);
      });
    return [
      _SectionHeader('Paired devices', trailing: IconButton(
        tooltip: 'Refresh',
        onPressed: _loadBonded,
        icon: const Icon(Icons.refresh),
      )),
      if (_bonded.isEmpty)
        const Padding(
          padding: EdgeInsets.symmetric(vertical: 8),
          child: Text('No paired devices yet. Scan below to find your Pi.'),
        ),
      for (final d in _bonded)
        _DeviceTile(
          device: d,
          connecting: busy && client.device?.address == d.address,
          enabled: !busy,
          onTap: () => _connect(d),
          onForget: () => _unpair(d),
        ),
      const SizedBox(height: 20),
      _SectionHeader('Nearby devices',
          trailing: FilledButton.tonalIcon(
            onPressed: busy ? null : _scan,
            icon: _scanning
                ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.bluetooth_searching),
            label: Text(_scanning ? 'Stop' : 'Scan'),
          )),
      if (found.isEmpty)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 8),
          child: Text(_scanning ? 'Searching…' : 'Tap Scan to look for your Pi (it appears as "Arstro-…").'),
        ),
      for (final d in found)
        _DeviceTile(
          device: d,
          connecting: busy && client.device?.address == d.address,
          enabled: !busy,
          onTap: () => _connect(d),
        ),
      Align(
        alignment: Alignment.centerLeft,
        child: TextButton.icon(
          onPressed: busy ? null : _connectByAddress,
          icon: const Icon(Icons.keyboard_outlined),
          label: const Text('Connect by Bluetooth address…'),
        ),
      ),
      const SizedBox(height: 16),
      const _HelpCard(),
    ];
  }
}

class _Hero extends StatelessWidget {
  const _Hero({required this.theme});
  final ThemeData theme;

  @override
  Widget build(BuildContext context) {
    final scheme = theme.colorScheme;
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(20),
        gradient: LinearGradient(
          colors: [scheme.primaryContainer, scheme.tertiaryContainer],
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
        ),
      ),
      child: Row(children: [
        Container(
          width: 56,
          height: 56,
          decoration: BoxDecoration(color: scheme.primary, borderRadius: BorderRadius.circular(16)),
          child: Icon(Icons.settings_remote, color: scheme.onPrimary, size: 32),
        ),
        const SizedBox(width: 16),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('Arstro Remote System',
                style: theme.textTheme.titleLarge?.copyWith(
                    fontWeight: FontWeight.w700, color: scheme.onPrimaryContainer)),
            const SizedBox(height: 4),
            Text('Monitor, configure Wi-Fi, open a shell and drive the desktop of your Orange Pi over Bluetooth.',
                style: theme.textTheme.bodyMedium?.copyWith(color: scheme.onPrimaryContainer)),
          ]),
        ),
      ]),
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.client, required this.onRetry});
  final RemoteClient client;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final failed = client.state == LinkState.failed;
    return Card(
      color: failed ? theme.colorScheme.errorContainer : theme.colorScheme.secondaryContainer,
      margin: const EdgeInsets.only(bottom: 16),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(children: [
          if (failed)
            Icon(Icons.error_outline, color: theme.colorScheme.onErrorContainer)
          else
            const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 3)),
          const SizedBox(width: 16),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(failed ? 'Could not connect to ${client.device?.label}' : 'Connecting to ${client.device?.label}…',
                  style: theme.textTheme.titleMedium),
              if (failed && client.error != null) ...[
                const SizedBox(height: 4),
                Text(client.error!, style: theme.textTheme.bodySmall),
              ],
              if (!failed) ...[
                const SizedBox(height: 4),
                Text('Accept the pairing request if Android asks for it.', style: theme.textTheme.bodySmall),
              ],
            ]),
          ),
          if (failed) TextButton(onPressed: onRetry, child: const Text('Retry'))
          else TextButton(onPressed: client.cancelConnect, child: const Text('Cancel')),
        ]),
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  const _SectionHeader(this.title, {this.trailing});
  final String title;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(children: [
        Expanded(child: Text(title, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600))),
        ?trailing,
      ]),
    );
  }
}

class _DeviceTile extends StatelessWidget {
  const _DeviceTile({
    required this.device,
    required this.onTap,
    required this.connecting,
    required this.enabled,
    this.onForget,
  });

  final BtDevice device;
  final VoidCallback onTap;
  final VoidCallback? onForget;
  final bool connecting;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: ListTile(
        enabled: enabled || connecting,
        onTap: enabled ? onTap : null,
        leading: CircleAvatar(
          backgroundColor: device.isArstro ? scheme.primary : scheme.surfaceContainerHighest,
          foregroundColor: device.isArstro ? scheme.onPrimary : scheme.onSurfaceVariant,
          child: Icon(device.isArstro ? Icons.developer_board : Icons.bluetooth),
        ),
        title: Text(device.label, style: const TextStyle(fontWeight: FontWeight.w600)),
        subtitle: Text([
          device.address,
          if (device.rssi != null) '${device.rssi} dBm',
          if (device.isArstro) 'Arstro Pi',
        ].join(' · ')),
        trailing: connecting
            ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 3))
            : Row(mainAxisSize: MainAxisSize.min, children: [
                if (onForget != null)
                  PopupMenuButton<String>(
                    tooltip: 'More',
                    onSelected: (v) => v == 'forget' ? onForget!() : null,
                    itemBuilder: (_) => const [PopupMenuItem(value: 'forget', child: Text('Forget pairing'))],
                  ),
                Icon(device.bonded ? Icons.link : Icons.add_link, color: scheme.primary),
              ]),
      ),
    );
  }
}

class _InfoCard extends StatelessWidget {
  const _InfoCard({required this.icon, required this.text, this.action});
  final IconData icon;
  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(children: [
          Icon(icon, size: 40),
          const SizedBox(height: 12),
          Text(text, textAlign: TextAlign.center),
          if (action != null) ...[const SizedBox(height: 16), action!],
        ]),
      ),
    );
  }
}

class _HelpCard extends StatelessWidget {
  const _HelpCard();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      color: theme.colorScheme.surfaceContainerLow,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(Icons.help_outline, size: 18, color: theme.colorScheme.primary),
            const SizedBox(width: 8),
            Text('First time?', style: theme.textTheme.titleSmall),
          ]),
          const SizedBox(height: 8),
          const Text('1. The Pi accepts new phones for 10 minutes after it boots. '
              'To open pairing again run  arstro-remote pair  on the Pi.\n'
              '2. Tap Scan, pick "Arstro-<hostname>" and accept the pairing request.\n'
              '3. Next time the app reconnects automatically.'),
        ]),
      ),
    );
  }
}

class _AddressDialog extends StatefulWidget {
  const _AddressDialog();

  @override
  State<_AddressDialog> createState() => _AddressDialogState();
}

class _AddressDialogState extends State<_AddressDialog> {
  final _ctl = TextEditingController();
  static final _mac = RegExp(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$');

  String get _value {
    // Developer option: tcp:host:port reaches the daemon through an SSH tunnel
    // (see README). Outbound only; the Pi itself never listens on TCP.
    if (_ctl.text.trim().startsWith('tcp:')) return _ctl.text.trim();
    final hex = _ctl.text.toUpperCase().replaceAll(RegExp(r'[^0-9A-F]'), '');
    if (hex.length != 12) return _ctl.text.toUpperCase().trim();
    return [for (var i = 0; i < 12; i += 2) hex.substring(i, i + 2)].join(':');
  }

  @override
  void dispose() {
    _ctl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final ok = _mac.hasMatch(_value) || (RegExp(r'^tcp:[\w.-]+:\d+$').hasMatch(_value));
    return AlertDialog(
      title: const Text('Connect by address'),
      content: SizedBox(
        width: 420,
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Use this when the Pi does not show up in the scan. '
              'Run  arstro-remote status  on the Pi to see its address.'),
          const SizedBox(height: 16),
          TextField(
            controller: _ctl,
            autofocus: true,
            textCapitalization: TextCapitalization.characters,
            decoration: InputDecoration(
              labelText: 'Bluetooth address',
              hintText: '00:11:22:AA:BB:CC',
              border: const OutlineInputBorder(),
              errorText: _ctl.text.isEmpty || ok ? null : 'Format: 12 hex digits, e.g. 00:11:22:AA:BB:CC',
            ),
            onChanged: (_) => setState(() {}),
            onSubmitted: (_) => ok ? Navigator.pop(context, _value) : null,
          ),
        ]),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(onPressed: ok ? () => Navigator.pop(context, _value) : null, child: const Text('Connect')),
      ],
    );
  }
}
