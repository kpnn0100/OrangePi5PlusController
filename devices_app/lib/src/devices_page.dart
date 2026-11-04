import 'dart:async';

import 'package:flutter/material.dart';

import 'api.dart';
import 'device_page.dart';
import 'open.dart';
import 'servers_page.dart';
import 'theme.dart';
import 'widgets.dart';

IconData kindIcon(DeviceKind k) => switch (k) {
      DeviceKind.phone => Icons.smartphone,
      DeviceKind.backup => Icons.terminal,
      DeviceKind.computer => Icons.computer_outlined,
    };

/// The device's place on its machine (RLY-29): "Main" (a slot), "Backup terminal", or its own name.
({String name, String? tag, IconData icon}) roleOf(Device d) {
  final slot = (d.slot ?? '').toLowerCase();
  if (slot == 'backup') return (name: 'Backup terminal', tag: null, icon: Icons.terminal);
  if (RegExp(r'^[a-z]$').hasMatch(slot)) return (name: 'Main', tag: 'slot ${slot.toUpperCase()}', icon: Icons.web_asset);
  return (name: d.name, tag: null, icon: kindIcon(d.kind));
}

/// "online for 3 h · 1 viewer" or "last seen 2 d ago".
String onlineText(Device d) => d.online
    ? ['online${d.since != null ? ' for ${span(d.since!)}' : ''}', if (d.viewers > 0) '${d.viewers} viewer${d.viewers == 1 ? '' : 's'}']
        .join(' · ')
    : 'last seen ${d.lastSeen != null ? ago(d.lastSeen!) : 'never'}';

/// The home page (MGR-02): the devices of the current device manager, one item per machine (MGR-07),
/// online first, and the ports that answered lately (MGR-05). Refreshes every 10 s while it is on screen.
class DevicesPage extends StatefulWidget {
  const DevicesPage({super.key, required this.api, this.refreshEvery = const Duration(seconds: 10)});
  final DevicesApi api;
  final Duration refreshEvery;

  @override
  State<DevicesPage> createState() => _DevicesPageState();
}

class _DevicesPageState extends State<DevicesPage> with WidgetsBindingObserver {
  ServerList? _servers;
  List<Device>? _devices;
  List<Recent> _recents = const [];
  ApiError? _error;
  bool _loading = false;
  Timer? _timer;
  bool _visible = true;

  DevicesApi get api => widget.api;
  Server? get server => _servers?.active;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load(servers: true);
    _timer = Timer.periodic(widget.refreshEvery, (_) {
      if (_visible && (ModalRoute.of(context)?.isCurrent ?? true)) _load();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _visible = state == AppLifecycleState.resumed;
    if (_visible) _load();                              // back from a device's page: new recents
  }

  Future<void> _load({bool servers = false}) async {
    if (_loading) return;
    _loading = true;
    try {
      if (servers || _servers == null) {
        final s = await api.servers();
        if (!mounted) return;
        setState(() => _servers = s);
        if (s.servers.isEmpty) {
          await _addFirst();
          return;
        }
      }
      final sid = server!.id;
      final recents = await api.recents(sid);
      if (mounted) setState(() => _recents = recents);
      final devices = await api.devices(sid);
      if (!mounted || server?.id != sid) return;
      setState(() {
        _devices = devices;
        _error = null;
      });
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      _loading = false;
    }
  }

  Future<void> _addFirst() async {
    _loading = false;
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => ServerPage(api: api, firstRun: true)));
    if (mounted) await _load(servers: true);
  }

  Future<void> _switchServer() async {
    if (_servers == null) return;
    if (await showServersSheet(context, api, _servers!)) {
      setState(() {
        _devices = null;
        _recents = const [];
        _error = null;
      });
      await _load(servers: true);
    }
  }

  Future<void> _open(Device d, int port) async {
    await openPort(context, api, server!.id, d, port);
    if (mounted) _load();
  }

  Future<void> _openRecent(Recent r) async {
    final d = _devices?.where((d) => d.id == r.device).firstOrNull ?? Device(id: r.device, name: r.name, hasKey: true);
    await _open(d, r.port);
  }

  Future<void> _details(Device d) async {
    await Navigator.of(context)
        .push(MaterialPageRoute(builder: (_) => DevicePage(api: api, server: server!.id, device: d, devices: _devices ?? const [])));
    if (mounted) _load();
  }

  @override
  Widget build(BuildContext context) {
    final s = server;
    final devices = _devices ?? const <Device>[];
    final groups = groupByMachine(devices);
    final online = groups.where((g) => g.online > 0).toList();
    final offline = groups.where((g) => g.online == 0).toList();
    final names = {for (final d in devices) d.id: d.name};
    return Scaffold(
      appBar: AppBar(
        titleSpacing: 8,
        title: InkWell(
          key: const Key('server-switch'),
          borderRadius: BorderRadius.circular(Arstro.rButton),
          onTap: _switchServer,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              const ArstroMark(size: 28),
              const SizedBox(width: 12),
              Flexible(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  const Text('Devices'),
                  if (s != null)
                    Text(s.label, overflow: TextOverflow.ellipsis, style: Arstro.meta.copyWith(height: 1.3)),
                ]),
              ),
              const SizedBox(width: 2),
              const Icon(Icons.expand_more, size: 18, color: Arstro.muted),
            ]),
          ),
        ),
        actions: [
          IconButton(tooltip: 'Refresh', icon: const Icon(Icons.refresh), onPressed: () => _load()),
          const SizedBox(width: 4),
        ],
      ),
      body: RefreshIndicator(
        color: Arstro.text,
        backgroundColor: Arstro.surface2,
        onRefresh: () => _load(),
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 32), children: [
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 16),
              child: ErrorBanner(
                key: const Key('list-error'),
                icon: Icons.cloud_off_outlined,
                text: _error!.message,
                action: _error!.code == 'password'
                    ? TextButton(onPressed: _switchServer, child: const Text('Password'))
                    : TextButton(onPressed: () => _load(), child: const Text('Retry')),
              ),
            ),
          if (_recents.isNotEmpty) ...[
            _Header('Recent'),
            SizedBox(
              height: 82,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                clipBehavior: Clip.none,
                itemCount: _recents.length.clamp(0, 12),
                separatorBuilder: (_, _) => const SizedBox(width: 8),
                itemBuilder: (_, i) {
                  final r = _recents[i];
                  return _RecentChip(
                    recent: r,
                    deviceName: names[r.device] ?? r.name,
                    online: devices.any((d) => d.id == r.device && d.online),
                    onTap: () => _openRecent(r),
                    onRemove: () async {
                      await api.removeRecent(s!.id, r.device, r.port);
                      _load();
                    },
                  );
                },
              ),
            ),
          ],
          if (_devices == null && _error == null)
            const Padding(
              padding: EdgeInsets.all(48),
              child: Center(child: SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2))),
            ),
          if (_devices != null && devices.isEmpty)
            const Padding(
              padding: EdgeInsets.only(top: 24),
              child: EmptyState(
                icon: Icons.devices_other_outlined,
                text: 'No devices yet.\nA machine shows up here once its Internet access is on '
                    '(install.sh --relay, or its web page › Internet access).',
              ),
            ),
          if (online.isNotEmpty) ...[
            _Header('Online · ${online.length}'),
            RowGroup(children: [for (final g in online) _item(g)]),
          ],
          if (offline.isNotEmpty) ...[
            _Header('Offline · ${offline.length}'),
            RowGroup(children: [for (final g in offline) _item(g)]),
          ],
        ]),
      ),
    );
  }

  /// A machine with several devices is one item (MGR-07); a device on its own is one row.
  Widget _item(MachineGroup g) {
    VoidCallback? opener(Device d) => d.online && d.web != null ? () => _open(d, d.web!) : null;
    if (g.devices.length == 1) {
      final d = g.devices.single;
      return _DeviceTile(device: d, onTap: () => _details(d), onOpen: opener(d));
    }
    return _MachineItem(group: g, onTap: _details, opener: opener);
  }
}

class _Header extends StatelessWidget {
  const _Header(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Overline(text, padding: const EdgeInsets.fromLTRB(2, 24, 2, 10));
}

class _RecentChip extends StatelessWidget {
  const _RecentChip({required this.recent, required this.deviceName, required this.online, required this.onTap, required this.onRemove});
  final Recent recent;
  final String deviceName;
  final bool online;
  final VoidCallback onTap;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 168,
      child: Material(
        color: Arstro.surface,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Arstro.rCard), side: const BorderSide(color: Arstro.line)),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          key: Key('recent-${recent.device}-${recent.port}'),
          onTap: onTap,
          onLongPress: () async {
            final ok = await showDialog<bool>(
              context: context,
              builder: (c) => AlertDialog(
                title: Text('Remove $deviceName :${recent.port} from Recent?'),
                actions: [
                  TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
                  FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Remove')),
                ],
              ),
            );
            if (ok == true) onRemove();
          },
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisAlignment: MainAxisAlignment.center, children: [
              Row(children: [
                StatusDot(online: online, size: 6),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(deviceName, maxLines: 1, overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w500, height: 1.3)),
                ),
              ]),
              const SizedBox(height: 5),
              Text.rich(
                TextSpan(children: [
                  if (recent.title.isEmpty)
                    TextSpan(text: 'Port ${recent.port}')
                  else ...[
                    TextSpan(text: ':${recent.port}', style: Arstro.monoStyle(size: 12, color: Arstro.text2)),
                    TextSpan(text: ' · ${recent.title}'),
                  ],
                ]),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12.5, color: Arstro.text2, height: 1.35),
              ),
              Text(ago(recent.at), style: Arstro.meta.copyWith(fontSize: 12, height: 1.35)),
            ]),
          ),
        ),
      ),
    );
  }
}

/// One device in the list, like a row of the web list: the kind icon in a box, the name, what it
/// runs, how long it has been on (or when it was last seen), and Open for its own web page.
class _DeviceTile extends StatelessWidget {
  const _DeviceTile({required this.device, required this.onTap, this.onOpen});
  final Device device;
  final VoidCallback onTap;
  final VoidCallback? onOpen;

  @override
  Widget build(BuildContext context) {
    final d = device;
    final bits = <InlineSpan>[];
    void add(InlineSpan b) {
      if (bits.isNotEmpty) bits.add(const TextSpan(text: ' · '));
      bits.add(b);
    }

    if (d.hostname.isNotEmpty && d.hostname != d.name) add(TextSpan(text: d.hostname));
    if (d.machine != null && d.machine!.model.isNotEmpty) add(TextSpan(text: d.machine!.model));
    if (d.slotLabel != null) add(TextSpan(text: 'slot ${d.slotLabel}'));
    if (d.kind == DeviceKind.backup) add(const TextSpan(text: 'backup terminal'));
    if (d.shortVersion.isNotEmpty) add(TextSpan(text: 'v${d.shortVersion}', style: Arstro.monoStyle(size: 11.5, color: Arstro.muted)));
    final line2 = onlineText(d);
    return InkWell(
      key: Key('device-${d.id}'),
      onTap: onTap,
      child: Padding(
        padding: EdgeInsets.fromLTRB(14, 12, onOpen != null ? 12 : 10, 12),
        child: Row(children: [
          KindBox(icon: kindIcon(d.kind), online: d.online),
          const SizedBox(width: 14),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(d.name, maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontSize: 15, fontWeight: FontWeight.w500, height: 1.35, color: d.online ? Arstro.text : Arstro.text2)),
              if (bits.isNotEmpty)
                Text.rich(TextSpan(children: bits), maxLines: 1, overflow: TextOverflow.ellipsis, style: Arstro.meta),
              const SizedBox(height: 1),
              Row(children: [
                StatusDot(online: d.online, size: 6),
                const SizedBox(width: 7),
                Flexible(child: Text(line2, maxLines: 1, overflow: TextOverflow.ellipsis, style: Arstro.meta)),
                if (!d.hasKey) ..._noKey,
              ]),
            ]),
          ),
          const SizedBox(width: 8),
          if (onOpen != null)
            OutlinedButton(
              key: Key('open-${d.id}'),
              style: Arstro.small,
              onPressed: onOpen,
              child: const Text('Open'),
            )
          else
            const Icon(Icons.chevron_right, size: 18, color: Arstro.muted),
        ]),
      ),
    );
  }
}

/// The marker of a device the app has no access key for.
const _noKey = [
  SizedBox(width: 10),
  Icon(Icons.key_off_outlined, size: 13, color: Arstro.muted),
  Text(' no key', style: Arstro.meta),
];

/// A machine with several devices (MGR-07, like the web list): its name, what it is and how many of
/// its devices are online, then one row per device - Main (its slot) and the Backup terminal.
class _MachineItem extends StatelessWidget {
  const _MachineItem({required this.group, required this.onTap, required this.opener});
  final MachineGroup group;
  final void Function(Device) onTap;
  final VoidCallback? Function(Device) opener;

  static const _inset = 64.0;                            // under the name: 14 + the kind box + 14

  @override
  Widget build(BuildContext context) {
    final g = group;
    final on = g.online > 0;
    final bits = machineBits(g.machine, g.devices.first);
    return Column(key: Key('machine-${g.id}'), crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(14, 12, 12, 11),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          KindBox(icon: Icons.computer_outlined, online: on),
          const SizedBox(width: 14),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Expanded(
                  child: Text(g.name, maxLines: 1, overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 15, fontWeight: FontWeight.w500, height: 1.35, color: on ? Arstro.text : Arstro.text2)),
                ),
                const SizedBox(width: 8),
                StatusPill.machine(g.online, g.devices.length, key: Key('machine-pill-${g.id}')),
              ]),
              if (bits.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(bits.join(' · '), key: Key('machine-about-${g.id}'), maxLines: 2, overflow: TextOverflow.ellipsis,
                      style: Arstro.meta),
                ),
            ]),
          ),
        ]),
      ),
      for (final d in g.devices) ...[
        const Divider(indent: _inset),
        _MemberRow(device: d, onTap: () => onTap(d), onOpen: opener(d)),
      ],
    ]);
  }
}

/// One device of a machine: its role (Main · slot A, Backup terminal), version, status and Open.
class _MemberRow extends StatelessWidget {
  const _MemberRow({required this.device, required this.onTap, this.onOpen});
  final Device device;
  final VoidCallback onTap;
  final VoidCallback? onOpen;

  @override
  Widget build(BuildContext context) {
    final d = device;
    final role = roleOf(d);
    final color = d.online ? Arstro.text : Arstro.text2;
    return InkWell(
      key: Key('device-${d.id}'),
      onTap: onTap,
      child: Padding(
        padding: EdgeInsets.fromLTRB(_MachineItem._inset, 10, onOpen != null ? 12 : 10, 10),
        child: Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Icon(role.icon, size: 15, color: d.online ? Arstro.text2 : Arstro.muted),
                const SizedBox(width: 8),
                Flexible(
                  child: Text(role.name, maxLines: 1, overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 14.5, fontWeight: FontWeight.w500, height: 1.35, color: color)),
                ),
                if (role.tag != null) ...[const SizedBox(width: 8), Tag(role.tag!)],
              ]),
              const SizedBox(height: 2),
              Row(children: [
                StatusDot(online: d.online, size: 6),
                const SizedBox(width: 7),
                Flexible(
                  child: Text.rich(
                    TextSpan(children: [
                      if (d.shortVersion.isNotEmpty) ...[
                        TextSpan(text: 'v${d.shortVersion}', style: Arstro.monoStyle(size: 11.5, color: Arstro.muted)),
                        const TextSpan(text: ' · '),
                      ],
                      TextSpan(text: onlineText(d)),
                    ]),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Arstro.meta,
                  ),
                ),
                if (!d.hasKey) ..._noKey,
              ]),
            ]),
          ),
          const SizedBox(width: 8),
          if (onOpen != null)
            OutlinedButton(key: Key('open-${d.id}'), style: Arstro.small, onPressed: onOpen, child: const Text('Open'))
          else
            const Icon(Icons.chevron_right, size: 18, color: Arstro.muted),
        ]),
      ),
    );
  }
}
