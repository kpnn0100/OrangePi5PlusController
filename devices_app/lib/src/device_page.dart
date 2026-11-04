import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'devices_page.dart' show kindIcon, roleOf;
import 'open.dart';
import 'theme.dart';
import 'widgets.dart';

/// One device (MGR-02, MGR-05): what the device manager knows about it, its access key, the ports
/// that answered lately, any other port, and the ports it listens on; a switch to the other devices
/// of its machine and what the machine is (MGR-07).
class DevicePage extends StatefulWidget {
  const DevicePage({super.key, required this.api, required this.server, required this.device, this.devices = const []});
  final DevicesApi api;
  final String server;
  final Device device;
  final List<Device> devices;                            // the list it was opened from (for the switch)

  @override
  State<DevicePage> createState() => _DevicePageState();
}

class _DevicePageState extends State<DevicePage> with WidgetsBindingObserver {
  late Device _d = widget.device;
  late List<Device> _all = widget.devices;
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
      if (mounted) {
        setState(() {
          _all = list;
          if (d != null) _d = d;
        });
      }
    } on ApiError {
      // the home page shows connection problems; keep what is shown
    }
  }

  /// The devices of this device's machine, slots first (MGR-07); fewer than two: no switch.
  List<Device> get _siblings =>
      groupByMachine(_all).where((g) => g.devices.any((x) => x.id == _d.id)).firstOrNull?.devices ?? const [];

  /// Shows another device of the machine in place (Main <-> Backup terminal).
  void _switchTo(Device x) {
    if (x.id == _d.id) return;
    setState(() {
      _d = x;
      _recents = const [];
      _check = null;
      _checkError = null;
      _ports = null;
      _portsError = null;
      _port.clear();
    });
    _refresh();
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
    final d = _d;
    const dim = TextStyle(fontSize: 13.5, color: Arstro.muted, height: 1.5);
    final kindName = switch (d.kind) {
      DeviceKind.phone => 'phone',
      DeviceKind.backup => 'backup terminal',
      DeviceKind.computer => 'web UI',
    };
    // next to the status pill: for how long, and who is watching (the pill says online / offline)
    final status = d.online
        ? [if (d.since != null) 'for ${span(d.since!)}', if (d.viewers > 0) '${d.viewers} viewer${d.viewers == 1 ? '' : 's'}']
            .join(' · ')
        : 'last seen ${d.lastSeen != null ? ago(d.lastSeen!) : 'never'}';
    final m = d.machine;
    final siblings = _siblings;
    final facts = [
      if (m != null && m.model.isNotEmpty) KeyValueRow('Model', m.model),
      if (m != null && m.cpu.isNotEmpty) KeyValueRow('Processor', m.cpu),
      if (m?.cores != null) KeyValueRow('Cores', '${m!.cores}'),
      if (m?.memory != null) KeyValueRow('Memory', memoryText(m!.memory!)),
      if (m != null && m.os.isNotEmpty) KeyValueRow('System', m.os),
      if (m != null && m.kernel.isNotEmpty) KeyValueRow('Kernel', m.kernel, mono: true),
      if (m != null && m.arch.isNotEmpty) KeyValueRow('Architecture', m.arch),
    ];
    final about = [
      if (m != null && m.model.isNotEmpty) m.model else if (d.platform.isNotEmpty) d.platform,
      if (d.kind == DeviceKind.backup) 'backup terminal',
      if (d.slotLabel != null) 'slot ${d.slotLabel}',
    ];
    return Scaffold(
      appBar: AppBar(title: Text(d.name)),
      body: ListView(padding: const EdgeInsets.fromLTRB(16, 20, 16, 32), children: [
        if (siblings.length > 1) ...[
          _Siblings(devices: siblings, current: d.id, onTap: _switchTo),
          const SizedBox(height: 20),
        ],
        Row(children: [
          KindBox(icon: kindIcon(d.kind), online: d.online, size: 44),
          const SizedBox(width: 14),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                StatusPill(online: d.online),
                const SizedBox(width: 10),
                Flexible(child: Text(status, maxLines: 1, overflow: TextOverflow.ellipsis, style: Arstro.meta)),
              ]),
              const SizedBox(height: 5),
              Text.rich(
                TextSpan(children: [
                  if (d.shortVersion.isNotEmpty)
                    TextSpan(text: 'v${d.shortVersion}', style: Arstro.monoStyle(size: 12, color: Arstro.text2)),
                  for (final a in about) TextSpan(text: '${d.shortVersion.isEmpty && a == about.first ? '' : ' · '}$a'),
                ]),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: Arstro.meta,
              ),
            ]),
          ),
        ]),
        if (d.web != null) ...[
          const SizedBox(height: 20),
          FilledButton.icon(
            key: const Key('open-web'),
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            onPressed: () => _open(d.web!),
            icon: const Icon(Icons.open_in_new),
            label: Text('Open the $kindName (port ${d.web})'),
          ),
        ],
        const SizedBox(height: 20),
        SectionCard(
          title: 'Access key',
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text.rich(
              key: const Key('key-state'),
              d.hasKey
                  ? TextSpan(children: [
                      const TextSpan(text: 'Saved on this phone (fingerprint '),
                      TextSpan(text: d.keyId ?? '?', style: Arstro.monoStyle(size: 13, color: Arstro.text)),
                      const TextSpan(text: ').'),
                    ])
                  : TextSpan(text: 'No key yet. ${keyHint(d)}'),
              style: const TextStyle(fontSize: 14, color: Arstro.text2, height: 1.5),
            ),
            if (_check != null) ...[
              const SizedBox(height: 10),
              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Padding(
                  padding: EdgeInsets.only(top: 2),
                  child: Icon(Icons.check_circle_outline, size: 17, color: Arstro.ok),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                      'The key works: ${_check!.name}${_check!.version.isNotEmpty ? ' v${_check!.version}' : ''}'
                      '${_check!.transport == 'http' ? ' (over HTTP)' : ''}.',
                      key: const Key('check-ok'),
                      style: const TextStyle(fontSize: 14, color: Arstro.text, height: 1.45)),
                ),
              ]),
              if (_check!.ports != null) ...[
                const SizedBox(height: 10),
                const Text('This device allows only these ports:', style: dim),
                const SizedBox(height: 6),
                Wrap(spacing: 6, runSpacing: 6, children: [
                  for (final p in _check!.ports!)
                    ActionChip(label: Text('$p', style: Arstro.monoStyle(size: 12.5)), onPressed: () => _open(p)),
                ]),
              ],
            ],
            if (_checkError != null) ...[
              const SizedBox(height: 10),
              Text(_checkError!, key: const Key('check-error'), style: const TextStyle(color: Arstro.err, fontSize: 13.5)),
            ],
            const SizedBox(height: 14),
            Wrap(spacing: 8, runSpacing: 8, children: [
              OutlinedButton(
                  key: const Key('set-key'), style: Arstro.small, onPressed: _setKey, child: Text(d.hasKey ? 'Replace' : 'Add key')),
              if (d.hasKey)
                OutlinedButton(
                  key: const Key('check'),
                  style: Arstro.small,
                  onPressed: _checking || !d.online ? null : _runCheck,
                  child: _checking
                      ? const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 1.75))
                      : const Text('Check'),
                ),
              if (d.hasKey)
                TextButton(
                  style: Arstro.small.copyWith(foregroundColor: const WidgetStatePropertyAll(Arstro.err)),
                  onPressed: _forgetKey,
                  child: const Text('Forget'),
                ),
            ]),
          ]),
        ),
        const SizedBox(height: 12),
        SectionCard(
          title: 'Recent ports',
          child: _recents.isEmpty
              ? const Text('Ports that answered show up here, newest first.', style: dim)
              : RowGroup(radius: Arstro.rButton, color: Colors.transparent, children: [
                  for (final r in _recents)
                    _PortRow(
                      key: Key('device-recent-${r.port}'),
                      port: r.port,
                      title: r.title.isEmpty ? 'Port ${r.port}' : r.title,
                      meta: '${ago(r.at)} · opened ${r.count}×',
                      onTap: () => _open(r.port),
                      trailing: IconButton(
                        tooltip: 'Remove from Recent',
                        visualDensity: VisualDensity.compact,
                        icon: const Icon(Icons.close, size: 16),
                        onPressed: () async {
                          await api.removeRecent(widget.server, d.id, r.port);
                          _refresh();
                        },
                      ),
                    ),
                ]),
        ),
        const SizedBox(height: 12),
        SectionCard(
          title: 'Open a port',
          child: Row(children: [
            Expanded(
              child: TextField(
                key: const Key('port'),
                controller: _port,
                keyboardType: TextInputType.number,
                style: Arstro.monoStyle(size: 14),
                inputFormatters: [FilteringTextInputFormatter.digitsOnly, LengthLimitingTextInputFormatter(5)],
                textInputAction: TextInputAction.go,
                onSubmitted: (_) => _openTyped(),
                decoration: const InputDecoration(hintText: 'e.g. 8080', isDense: true),
              ),
            ),
            const SizedBox(width: 8),
            OutlinedButton(
              key: const Key('open-port'),
              style: OutlinedButton.styleFrom(minimumSize: const Size(72, 44)),
              onPressed: _openTyped,
              child: const Text('Open'),
            ),
          ]),
        ),
        const SizedBox(height: 12),
        SectionCard(
          title: 'Listening ports',
          trailing: TextButton(
            key: const Key('load-ports'),
            style: Arstro.small,
            onPressed: _loadingPorts || !d.online || !d.hasKey ? null : _loadPorts,
            child: Text(_ports == null ? 'Show' : 'Refresh'),
          ),
          child: _loadingPorts
              ? const Padding(
                  padding: EdgeInsets.all(12),
                  child: Center(child: SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))),
                )
              : _portsError != null
                  ? Text(_portsError!, style: const TextStyle(color: Arstro.err, fontSize: 13.5))
                  : _ports == null
                      ? Text(d.online ? 'The TCP ports this device listens on.' : 'The device is offline.', style: dim)
                      : _ports!.isEmpty
                          ? const Text('Nothing listens (or the device allows none of them).', style: dim)
                          : RowGroup(radius: Arstro.rButton, color: Colors.transparent, children: [
                              for (final p in _ports!)
                                _PortRow(
                                  port: p.port,
                                  title: p.proc.isEmpty ? '-' : p.proc,
                                  meta: p.addr,
                                  monoMeta: true,
                                  onTap: () => _open(p.port),
                                  trailing: const Padding(
                                    padding: EdgeInsets.only(right: 8),
                                    child: Icon(Icons.chevron_right, size: 18, color: Arstro.muted),
                                  ),
                                ),
                            ]),
        ),
        if (facts.isNotEmpty) ...[
          const SizedBox(height: 12),
          SectionCard(
            key: const Key('machine-card'),
            title: 'Machine',
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 7),
            child: Column(children: [
              for (final (i, row) in facts.indexed) ...[if (i > 0) const Divider(), row],
            ]),
          ),
        ],
        const SizedBox(height: 12),
        SectionCard(
          title: 'Details',
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 7),
          child: Column(children: [
            for (final (i, row) in [
              KeyValueRow('Device id', d.id, mono: true, selectable: true),
              if (d.hostname.isNotEmpty) KeyValueRow('Host name', d.hostname),
              if (d.platform.isNotEmpty && (m == null || m.os.isEmpty)) KeyValueRow('System', d.platform),
              if (d.shortVersion.isNotEmpty) KeyValueRow('Version', d.shortVersion, mono: true),
              if (d.slotLabel != null) KeyValueRow('Slot', d.slotLabel!),
              KeyValueRow(d.online ? 'Online for' : 'Last seen',
                  d.online ? (d.since != null ? span(d.since!) : '-') : (d.lastSeen != null ? ago(d.lastSeen!) : 'never')),
              if (d.online) KeyValueRow('Viewers', '${d.viewers}'),
            ].indexed) ...[if (i > 0) const Divider(), row],
          ]),
        ),
      ]),
    );
  }
}

/// A port in a list: the number in mono, what answers there, and an action on the right.
class _PortRow extends StatelessWidget {
  const _PortRow({super.key, required this.port, required this.title, required this.meta, required this.onTap,
      this.trailing, this.monoMeta = false});
  final int port;
  final String title;
  final String meta;
  final bool monoMeta;
  final VoidCallback onTap;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 52),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
          child: Row(children: [
            SizedBox(width: 58, child: Text('$port', style: Arstro.monoStyle(size: 13.5, weight: FontWeight.w500))),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 14, height: 1.35)),
                Text(meta, maxLines: 1, overflow: TextOverflow.ellipsis,
                    style: monoMeta ? Arstro.monoStyle(size: 11.5, color: Arstro.muted) : Arstro.meta),
              ]),
            ),
            ?trailing,
          ]),
        ),
      ),
    );
  }
}

/// The devices of one machine as a switch (MGR-07): a status dot and the role of each, the shown one
/// selected - from Main to the Backup terminal when the main server is down.
class _Siblings extends StatelessWidget {
  const _Siblings({required this.devices, required this.current, required this.onTap});
  final List<Device> devices;
  final String current;
  final void Function(Device) onTap;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(3),
      decoration: BoxDecoration(
        color: Arstro.surface,
        border: Border.all(color: Arstro.line),
        borderRadius: BorderRadius.circular(Arstro.rCard),
      ),
      child: Row(children: [
        for (final (i, x) in devices.indexed) ...[
          if (i > 0) const SizedBox(width: 3),
          Expanded(child: _segment(x)),
        ],
      ]),
    );
  }

  Widget _segment(Device x) {
    final role = roleOf(x);
    final active = x.id == current;
    return Material(
      color: active ? Arstro.surface3 : Colors.transparent,
      borderRadius: BorderRadius.circular(Arstro.rButton - 1),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        key: Key('sib-${x.id}'),
        onTap: () => onTap(x),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            StatusDot(online: x.online, size: 6),
            const SizedBox(width: 8),
            Flexible(
              child: Text(role.name, maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 13.5, fontWeight: FontWeight.w500, height: 1.3, color: active ? Arstro.text : Arstro.text2)),
            ),
            if (role.tag != null) ...[const SizedBox(width: 6), Tag(role.tag!)],
          ]),
        ),
      ),
    );
  }
}
