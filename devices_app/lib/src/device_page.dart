import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'devices_page.dart' show kindIcon;
import 'open.dart';
import 'widgets.dart';

/// One device (MGR-02, MGR-05): what the device manager knows about it, its access key, the ports
/// that answered lately, any other port, and the ports it listens on.
class DevicePage extends StatefulWidget {
  const DevicePage({super.key, required this.api, required this.server, required this.device});
  final DevicesApi api;
  final String server;
  final Device device;

  @override
  State<DevicePage> createState() => _DevicePageState();
}

class _DevicePageState extends State<DevicePage> with WidgetsBindingObserver {
  late Device _d = widget.device;
  List<Recent> _recents = const [];
  DeviceCheck? _check;
  String? _checkError;
  bool _checking = false;
  List<PortInfo>? _ports;
  String? _portsError;
  bool _loadingPorts = false;
  final _port = TextEditingController();
  Timer? _timer;

  DevicesApi get api => widget.api;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _refresh();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) {
      if (ModalRoute.of(context)?.isCurrent ?? false) _refresh();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    _port.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _refresh();
  }

  Future<void> _refresh() async {
    try {
      final recents = (await api.recents(widget.server)).where((r) => r.device == _d.id).toList();
      if (mounted) setState(() => _recents = recents);
      final list = await api.devices(widget.server);
      final d = list.where((x) => x.id == _d.id).firstOrNull;
      if (d != null && mounted) setState(() => _d = d);
    } on ApiError {
      // the home page shows connection problems; keep what is shown
    }
  }

  Future<void> _open(int port) async {
    await openPort(context, api, widget.server, _d, port);
    if (mounted) _refresh();
  }

  Future<void> _setKey() async {
    if (await showKeySheet(context, api, widget.server, _d)) {
      setState(() {
        _check = null;
        _checkError = null;
      });
      await _refresh();
    }
  }

  Future<void> _forgetKey() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Forget the access key?'),
        content: Text('You will need it again to open ${_d.name} from this phone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Forget')),
        ],
      ),
    );
    if (ok != true) return;
    await api.forgetKey(widget.server, _d.id);
    setState(() => _check = null);
    await _refresh();
  }

  Future<void> _runCheck() async {
    setState(() {
      _checking = true;
      _checkError = null;
    });
    try {
      final c = await api.check(widget.server, _d.id);
      if (mounted) setState(() => _check = c);
    } on ApiError catch (e) {
      if (mounted) setState(() => _checkError = e.message);
    } finally {
      if (mounted) setState(() => _checking = false);
    }
  }

  Future<void> _loadPorts() async {
    setState(() {
      _loadingPorts = true;
      _portsError = null;
    });
    try {
      final p = await api.ports(widget.server, _d.id);
      p.sort((a, b) => a.port.compareTo(b.port));
      if (mounted) setState(() => _ports = p);
    } on ApiError catch (e) {
      if (mounted) setState(() => _portsError = e.message);
    } finally {
      if (mounted) setState(() => _loadingPorts = false);
    }
  }

  void _openTyped() {
    final p = int.tryParse(_port.text.trim());
    if (p == null || p < 1 || p > 65535) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('A port is a number from 1 to 65535.')));
      return;
    }
    _open(p);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final d = _d;
    final dim = theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant);
    final kindName = switch (d.kind) {
      DeviceKind.phone => 'phone',
      DeviceKind.backup => 'backup terminal',
      DeviceKind.computer => 'web UI',
    };
    return Scaffold(
      appBar: AppBar(title: Text(d.name)),
      body: ListView(padding: const EdgeInsets.fromLTRB(12, 4, 12, 32), children: [
        Card(
          margin: EdgeInsets.zero,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Icon(kindIcon(d.kind), size: 30, color: d.online ? theme.colorScheme.primary : theme.colorScheme.onSurfaceVariant),
                const SizedBox(width: 12),
                Expanded(child: Text(d.name, style: theme.textTheme.titleLarge)),
                StatusDot(online: d.online, size: 12),
                const SizedBox(width: 6),
                Text(d.online ? 'online' : 'offline', style: dim),
              ]),
              const SizedBox(height: 12),
              KeyValueRow('Device id', d.id, mono: true, selectable: true),
              if (d.hostname.isNotEmpty) KeyValueRow('Host name', d.hostname),
              if (d.platform.isNotEmpty) KeyValueRow('System', d.platform),
              if (d.shortVersion.isNotEmpty) KeyValueRow('Version', d.shortVersion),
              if (d.slotLabel != null) KeyValueRow('Slot', d.slotLabel!),
              KeyValueRow(d.online ? 'Online for' : 'Last seen',
                  d.online ? (d.since != null ? span(d.since!) : '-') : (d.lastSeen != null ? ago(d.lastSeen!) : 'never')),
              if (d.online) KeyValueRow('Viewers', '${d.viewers}'),
            ]),
          ),
        ),
        if (d.web != null) ...[
          const SizedBox(height: 12),
          FilledButton.icon(
            key: const Key('open-web'),
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(52)),
            onPressed: () => _open(d.web!),
            icon: const Icon(Icons.open_in_new),
            label: Text('Open the $kindName (port ${d.web})'),
          ),
        ],
        const SizedBox(height: 12),
        SectionCard(
          title: 'Access key',
          icon: Icons.key,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(d.hasKey ? 'Saved on this phone (fingerprint ${d.keyId ?? '?'}).' : 'No key yet. ${keyHint(d)}',
                key: const Key('key-state')),
            if (_check != null) ...[
              const SizedBox(height: 8),
              Row(children: [
                const Icon(Icons.verified, size: 18, color: Color(0xFF3FB950)),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                      'The key works: ${_check!.name}${_check!.version.isNotEmpty ? ' v${_check!.version}' : ''}'
                      '${_check!.transport == 'http' ? ' (over HTTP)' : ''}.',
                      key: const Key('check-ok')),
                ),
              ]),
              if (_check!.ports != null) ...[
                const SizedBox(height: 6),
                Text('This device allows only these ports:', style: dim),
                const SizedBox(height: 4),
                Wrap(spacing: 6, children: [
                  for (final p in _check!.ports!) ActionChip(label: Text('$p'), onPressed: () => _open(p)),
                ]),
              ],
            ],
            if (_checkError != null) ...[
              const SizedBox(height: 8),
              Text(_checkError!, key: const Key('check-error'), style: TextStyle(color: theme.colorScheme.error)),
            ],
            const SizedBox(height: 12),
            Wrap(spacing: 8, runSpacing: 8, children: [
              FilledButton.tonal(key: const Key('set-key'), onPressed: _setKey, child: Text(d.hasKey ? 'Replace' : 'Add key')),
              if (d.hasKey)
                OutlinedButton(
                  key: const Key('check'),
                  onPressed: _checking || !d.online ? null : _runCheck,
                  child: _checking
                      ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Text('Check'),
                ),
              if (d.hasKey) TextButton(onPressed: _forgetKey, child: const Text('Forget')),
            ]),
          ]),
        ),
        const SizedBox(height: 12),
        SectionCard(
          title: 'Recent ports',
          icon: Icons.history,
          padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
          child: _recents.isEmpty
              ? Padding(
                  padding: const EdgeInsets.only(bottom: 8, right: 8),
                  child: Text('Ports that answered show up here, newest first.', style: dim),
                )
              : Column(children: [
                  for (final r in _recents)
                    ListTile(
                      key: Key('device-recent-${r.port}'),
                      contentPadding: EdgeInsets.zero,
                      leading: SizedBox(
                        width: 56,
                        child: Text('${r.port}', style: theme.textTheme.titleMedium?.copyWith(fontFamily: 'monospace')),
                      ),
                      title: Text(r.title.isEmpty ? 'Port ${r.port}' : r.title, maxLines: 1, overflow: TextOverflow.ellipsis),
                      subtitle: Text('${ago(r.at)} · opened ${r.count}×'),
                      trailing: IconButton(
                        tooltip: 'Remove from Recent',
                        icon: const Icon(Icons.close, size: 18),
                        onPressed: () async {
                          await api.removeRecent(widget.server, d.id, r.port);
                          _refresh();
                        },
                      ),
                      onTap: () => _open(r.port),
                    ),
                ]),
        ),
        const SizedBox(height: 12),
        SectionCard(
          title: 'Open a port',
          icon: Icons.input,
          child: Row(children: [
            Expanded(
              child: TextField(
                key: const Key('port'),
                controller: _port,
                keyboardType: TextInputType.number,
                inputFormatters: [FilteringTextInputFormatter.digitsOnly, LengthLimitingTextInputFormatter(5)],
                textInputAction: TextInputAction.go,
                onSubmitted: (_) => _openTyped(),
                decoration: const InputDecoration(hintText: 'e.g. 8080', border: OutlineInputBorder(), isDense: true),
              ),
            ),
            const SizedBox(width: 10),
            FilledButton(key: const Key('open-port'), onPressed: _openTyped, child: const Text('Open')),
          ]),
        ),
        const SizedBox(height: 12),
        SectionCard(
          title: 'Listening ports',
          icon: Icons.lan_outlined,
          trailing: TextButton(
            key: const Key('load-ports'),
            onPressed: _loadingPorts || !d.online || !d.hasKey ? null : _loadPorts,
            child: Text(_ports == null ? 'Show' : 'Refresh'),
          ),
          child: _loadingPorts
              ? const Padding(padding: EdgeInsets.all(12), child: Center(child: CircularProgressIndicator()))
              : _portsError != null
                  ? Text(_portsError!, style: TextStyle(color: theme.colorScheme.error))
                  : _ports == null
                      ? Text(d.online ? 'The TCP ports this device listens on.' : 'The device is offline.', style: dim)
                      : _ports!.isEmpty
                          ? Text('Nothing listens (or the device allows none of them).', style: dim)
                          : Column(children: [
                              for (final p in _ports!)
                                ListTile(
                                  dense: true,
                                  contentPadding: EdgeInsets.zero,
                                  leading: SizedBox(
                                    width: 56,
                                    child: Text('${p.port}', style: theme.textTheme.titleSmall?.copyWith(fontFamily: 'monospace')),
                                  ),
                                  title: Text(p.proc.isEmpty ? '-' : p.proc),
                                  subtitle: Text(p.addr),
                                  trailing: const Icon(Icons.chevron_right),
                                  onTap: () => _open(p.port),
                                ),
                            ]),
        ),
      ]),
    );
  }
}
