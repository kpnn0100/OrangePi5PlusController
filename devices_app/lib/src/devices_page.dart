import 'dart:async';

import 'package:flutter/material.dart';

import 'api.dart';
import 'device_page.dart';
import 'open.dart';
import 'servers_page.dart';
import 'widgets.dart';

IconData kindIcon(DeviceKind k) => switch (k) {
      DeviceKind.phone => Icons.smartphone,
      DeviceKind.backup => Icons.terminal,
      DeviceKind.computer => Icons.computer,
    };

/// The home page (MGR-02): the devices of the current device manager, online first, and the ports
/// that answered lately (MGR-05). Refreshes every 10 s while it is on screen.
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
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => DevicePage(api: api, server: server!.id, device: d)));
    if (mounted) _load();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final s = server;
    final devices = _devices ?? const <Device>[];
    final online = devices.where((d) => d.online).toList();
    final offline = devices.where((d) => !d.online).toList();
    final names = {for (final d in devices) d.id: d.name};
    return Scaffold(
      appBar: AppBar(
        titleSpacing: 8,
        title: InkWell(
          key: const Key('server-switch'),
          borderRadius: BorderRadius.circular(10),
          onTap: _switchServer,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            child: Row(mainAxisSize: MainAxisSize.min, children: [
              Flexible(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  const Text('Devices'),
                  if (s != null)
                    Text(s.label, overflow: TextOverflow.ellipsis,
                        style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                ]),
              ),
              const Icon(Icons.arrow_drop_down),
            ]),
          ),
        ),
        actions: [
          IconButton(tooltip: 'Refresh', icon: const Icon(Icons.refresh), onPressed: () => _load()),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () => _load(),
        child: ListView(padding: const EdgeInsets.fromLTRB(12, 4, 12, 24), children: [
          if (_error != null)
            Card(
              key: const Key('list-error'),
              color: theme.colorScheme.errorContainer,
              child: ListTile(
                leading: Icon(Icons.cloud_off, color: theme.colorScheme.onErrorContainer),
                title: Text(_error!.message, style: TextStyle(color: theme.colorScheme.onErrorContainer)),
                trailing: _error!.code == 'password'
                    ? TextButton(onPressed: _switchServer, child: const Text('Password'))
                    : TextButton(onPressed: () => _load(), child: const Text('Retry')),
              ),
            ),
          if (_recents.isNotEmpty) ...[
            _Header('Recent'),
            SizedBox(
              height: 84,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
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
            const Padding(padding: EdgeInsets.all(48), child: Center(child: CircularProgressIndicator())),
          if (_devices != null && devices.isEmpty)
            const Padding(
              padding: EdgeInsets.only(top: 48),
              child: EmptyState(
                icon: Icons.devices_other,
                text: 'No devices yet.\nA machine shows up here once its Internet access is on '
                    '(install.sh --relay, or its web page › Internet access).',
              ),
            ),
          if (online.isNotEmpty) _Header('Online · ${online.length}'),
          for (final d in online) _DeviceTile(device: d, onTap: () => _details(d), onOpen: d.web != null ? () => _open(d, d.web!) : null),
          if (offline.isNotEmpty) _Header('Offline · ${offline.length}'),
          for (final d in offline) _DeviceTile(device: d, onTap: () => _details(d)),
        ]),
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(6, 16, 6, 8),
      child: Text(text.toUpperCase(),
          style: theme.textTheme.labelMedium?.copyWith(
              letterSpacing: 1.1, color: theme.colorScheme.onSurfaceVariant, fontWeight: FontWeight.w600)),
    );
  }
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
    final theme = Theme.of(context);
    return SizedBox(
      width: 170,
      child: Card(
        margin: EdgeInsets.zero,
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
                StatusDot(online: online, size: 9),
                const SizedBox(width: 6),
                Expanded(child: Text(deviceName, maxLines: 1, overflow: TextOverflow.ellipsis, style: theme.textTheme.titleSmall)),
              ]),
              const SizedBox(height: 4),
              Text(recent.title.isEmpty ? 'Port ${recent.port}' : ':${recent.port} · ${recent.title}',
                  maxLines: 1, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodySmall),
              Text(ago(recent.at), style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            ]),
          ),
        ),
      ),
    );
  }
}

class _DeviceTile extends StatelessWidget {
  const _DeviceTile({required this.device, required this.onTap, this.onOpen});
  final Device device;
  final VoidCallback onTap;
  final VoidCallback? onOpen;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final d = device;
    final line1 = [if (d.hostname.isNotEmpty && d.hostname != d.name) d.hostname, if (d.slotLabel != null) 'slot ${d.slotLabel}',
      if (d.kind == DeviceKind.backup) 'backup terminal', if (d.shortVersion.isNotEmpty) 'v${d.shortVersion}'].join(' · ');
    final line2 = d.online
        ? ['online${d.since != null ? ' for ${span(d.since!)}' : ''}', if (d.viewers > 0) '${d.viewers} viewer${d.viewers == 1 ? '' : 's'}']
            .join(' · ')
        : 'last seen ${d.lastSeen != null ? ago(d.lastSeen!) : 'never'}';
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Card(
        margin: EdgeInsets.zero,
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          key: Key('device-${d.id}'),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 8, 12),
            child: Row(children: [
              Stack(clipBehavior: Clip.none, children: [
                Container(
                  width: 44,
                  height: 44,
                  decoration: BoxDecoration(
                    color: theme.colorScheme.surfaceContainerHigh,
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Icon(kindIcon(d.kind), color: d.online ? theme.colorScheme.primary : theme.colorScheme.onSurfaceVariant),
                ),
                Positioned(right: -2, bottom: -2, child: StatusDot(online: d.online, size: 13)),
              ]),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(d.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: theme.textTheme.titleMedium),
                  if (line1.isNotEmpty)
                    Text(line1, maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                  Row(children: [
                    Flexible(
                      child: Text(line2, maxLines: 1, overflow: TextOverflow.ellipsis,
                          style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                    ),
                    if (!d.hasKey) ...[
                      const SizedBox(width: 6),
                      Icon(Icons.key_off_outlined, size: 14, color: theme.colorScheme.onSurfaceVariant),
                      Text(' no key', style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                    ],
                  ]),
                ]),
              ),
              if (onOpen != null)
                FilledButton.tonal(key: Key('open-${d.id}'), onPressed: onOpen, child: const Text('Open'))
              else
                Icon(Icons.chevron_right, color: theme.colorScheme.onSurfaceVariant),
            ]),
          ),
        ),
      ),
    );
  }
}
