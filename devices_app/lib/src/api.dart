import 'dart:convert';

import 'package:flutter/services.dart';

/// The Kotlin side (android/.../core/Hub.kt) behind one method channel, "arstro/devices": the device
/// managers, the device lists, the access keys (kept encrypted there - the UI only sends a new one),
/// the recent ports and opening a port in the WebView (MGR-01..06).

/// A refusal with a code the UI reacts to: no_key, wrong_key, password, unreachable, refused, bad_input, bad_key.
class ApiError implements Exception {
  ApiError(this.code, this.message);
  final String code;
  final String message;
  @override
  String toString() => message;
}

class Server {
  Server({required this.id, required this.url, required this.name, required this.label, this.version = '', this.hasPassword = false});
  final String id;
  final String url;
  final String name;
  final String label;
  final String version;
  final bool hasPassword;

  /// The device manager's own name, else its address.
  String get title => name.isEmpty || name == 'Devices' ? label : '$name · $label';

  factory Server.fromJson(Map<String, dynamic> j) => Server(
        id: j['id'] as String,
        url: j['url'] as String,
        name: (j['name'] as String?) ?? '',
        label: (j['label'] as String?) ?? (j['url'] as String),
        version: (j['version'] as String?) ?? '',
        hasPassword: (j['hasPassword'] as bool?) ?? false,
      );
}

class ServerList {
  ServerList(this.current, this.servers);
  final String? current;
  final List<Server> servers;
  Server? get active => servers.where((s) => s.id == current).firstOrNull ?? servers.firstOrNull;
}

/// What the address probe found: the URL that answered, the device manager's name and version.
class Probe {
  Probe(this.url, this.name, this.version);
  final String url;
  final String name;
  final String version;
}

enum DeviceKind { computer, phone, backup }

/// What a device runs on, as it told the device manager (RLY-29): the id is a hash of the machine's
/// id, the same for its slots and its backup terminal. Devices of older versions send none.
class Machine {
  Machine({required this.id, this.model = '', this.cpu = '', this.cores, this.memory, this.os = '', this.kernel = '', this.arch = ''});
  final String id;
  final String model;
  final String cpu;
  final int? cores;
  final int? memory;
  final String os;
  final String kernel;
  final String arch;

  static Machine? fromJson(dynamic j) {
    if (j is! Map || j['id'] is! String || (j['id'] as String).isEmpty) return null;
    String text(String k) => j[k] is String ? j[k] as String : '';
    int? number(String k) => j[k] is num ? (j[k] as num).toInt() : null;
    return Machine(id: j['id'] as String, model: text('model'), cpu: text('cpu'), cores: number('cores'),
        memory: number('memory'), os: text('os'), kernel: text('kernel'), arch: text('arch'));
  }
}

class Device {
  Device({
    required this.id,
    required this.name,
    this.hostname = '',
    this.platform = '',
    this.version = '',
    this.slot,
    this.web,
    this.online = false,
    this.since,
    this.lastSeen,
    this.viewers = 0,
    this.hasKey = false,
    this.keyId,
    this.machine,
  });

  final String id;
  final String name;
  final String hostname;
  final String platform;
  final String version;
  final String? slot;
  final int? web;
  final bool online;
  final DateTime? since;
  final DateTime? lastSeen;
  final int viewers;
  final bool hasKey;
  final String? keyId;
  final Machine? machine;

  /// The version without the backup terminal's "backup-" prefix.
  String get shortVersion => version.startsWith('backup-') ? version.substring(7) : version;

  /// A slot worth showing (A, B): not the backup terminal's or a phone's.
  String? get slotLabel => slot == null || kind != DeviceKind.computer ? null : slot!.toUpperCase();

  DeviceKind get kind {
    if (slot == 'backup') return DeviceKind.backup;
    if (slot == 'phone' || platform.toLowerCase().contains('android')) return DeviceKind.phone;
    return DeviceKind.computer;
  }

  static DateTime? _time(dynamic v) => v is num && v > 0 ? DateTime.fromMillisecondsSinceEpoch((v * 1000).round()) : null;

  factory Device.fromJson(Map<String, dynamic> j) => Device(
        id: j['id'] as String,
        name: ((j['name'] as String?) ?? '').isEmpty ? j['id'] as String : j['name'] as String,
        hostname: (j['hostname'] as String?) ?? '',
        platform: (j['platform'] as String?) ?? '',
        version: (j['version'] as String?) ?? '',
        slot: j['slot'] as String?,
        web: (j['web'] as num?)?.toInt(),
        online: (j['online'] as bool?) ?? false,
        since: _time(j['since']),
        lastSeen: _time(j['last_seen']),
        viewers: (j['viewers'] as num?)?.toInt() ?? 0,
        hasKey: (j['hasKey'] as bool?) ?? false,
        keyId: j['keyId'] as String?,
        machine: Machine.fromJson(j['machine']),
      );
}

/// One item of the list (RLY-29): a machine's slots and its backup terminal together - slots first,
/// the backup last. A device that names no machine is an item of its own.
class MachineGroup {
  MachineGroup(this.id, this.name, this.machine, this.devices);
  final String id;
  final String name;
  final Machine? machine;
  final List<Device> devices;

  int get online => devices.where((d) => d.online).length;
}

/// The devices as the device manager groups them (relay `_machine_list`): online machines first, then by name.
List<MachineGroup> groupByMachine(List<Device> devices) {
  final groups = <String, List<Device>>{};
  for (final d in devices) {
    groups.putIfAbsent(d.machine != null ? 'm-${d.machine!.id}' : 'd-${d.id}', () => []).add(d);
  }
  String order(Device d) {
    final slot = (d.slot ?? '').toLowerCase();
    return '${slot == 'backup' ? 1 : 0}\u0000$slot\u0000${d.name.toLowerCase()}\u0000${d.id}';
  }

  final out = <MachineGroup>[];
  for (final MapEntry(:key, value: devs) in groups.entries) {
    devs.sort((a, b) => order(a).compareTo(order(b)));
    // the newest description: an online device, else the one seen last
    final fresh = devs.reduce((a, b) {
      if (a.online != b.online) return a.online ? a : b;
      return (b.lastSeen ?? DateTime(0)).isAfter(a.lastSeen ?? DateTime(0)) ? b : a;
    });
    final name = key.startsWith('m-') && fresh.hostname.isNotEmpty ? fresh.hostname : fresh.name;
    out.add(MachineGroup(key, name, fresh.machine, devs));
  }
  out.sort((a, b) {
    if ((a.online > 0) != (b.online > 0)) return a.online > 0 ? -1 : 1;
    final byName = a.name.toLowerCase().compareTo(b.name.toLowerCase());
    return byName != 0 ? byName : a.id.compareTo(b.id);
  });
  return out;
}

const _ramSizes = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048];

/// The RAM fitted: the kernel's total is a little less (60.4 GiB on a 64 GB machine).
String memoryText(int bytes) {
  final g = bytes / 1073741824;
  if (g < 0.9) return '${(bytes / 1048576).round()} MB';
  final size = _ramSizes.where((s) => s >= g && g >= s * 0.88).firstOrNull;
  return '${size ?? g.round()} GB';
}

/// What the machine is: model, processor, cores, memory, system.
List<String> machineBits(Machine? m, [Device? d]) => [
      if (m != null && m.model.isNotEmpty) m.model,
      if (m != null && m.cpu.isNotEmpty) m.cpu,
      if (m?.cores != null) '${m!.cores} ${m.cores == 1 ? 'core' : 'cores'}',
      if (m?.memory != null) memoryText(m!.memory!),
      if (m != null && m.os.isNotEmpty) '${m.os}${m.arch.isNotEmpty ? ' ${m.arch}' : ''}'
      else if (d != null && d.platform.isNotEmpty) d.platform,
    ];

/// A port that answered (MGR-05): newest first.
class Recent {
  Recent({required this.device, required this.name, required this.port, this.title = '', required this.at, this.count = 1, this.status = 0});
  final String device;
  final String name;
  final int port;
  final String title;
  final DateTime at;
  final int count;
  final int status;

  factory Recent.fromJson(Map<String, dynamic> j) => Recent(
        device: j['device'] as String,
        name: (j['name'] as String?) ?? (j['device'] as String),
        port: (j['port'] as num).toInt(),
        title: (j['title'] as String?) ?? '',
        at: DateTime.fromMillisecondsSinceEpoch((j['at'] as num?)?.toInt() ?? 0),
        count: (j['count'] as num?)?.toInt() ?? 1,
        status: (j['status'] as num?)?.toInt() ?? 0,
      );
}

class PortInfo {
  PortInfo(this.port, this.addr, this.proc);
  final int port;
  final String addr;
  final String proc;
}

/// The device's own description from a channel ("Check"): name, version, the allowed ports (null = any).
class DeviceCheck {
  DeviceCheck(this.name, this.version, this.ports, this.transport);
  final String name;
  final String version;
  final List<int>? ports;
  final String transport;
}

abstract class DevicesApi {
  Future<ServerList> servers();
  Future<Probe> probe(String input, {String? password});
  Future<String> saveServer({String? id, required String url, required String name, String version = '', String? password});
  Future<void> removeServer(String id);
  Future<void> select(String id);
  Future<List<Device>> devices(String server);
  Future<String> setKey(String server, String device, String key);
  Future<void> forgetKey(String server, String device);
  Future<DeviceCheck> check(String server, String device);
  Future<List<PortInfo>> ports(String server, String device);
  Future<List<Recent>> recents(String server);
  Future<void> removeRecent(String server, String device, int port);
  /// Checks the port answers, remembers it as recent, then shows it in the WebView.
  Future<void> open(String server, String device, String name, int port);
}

class ChannelApi implements DevicesApi {
  static const _ch = MethodChannel('arstro/devices');

  Future<T> _call<T>(String method, [Map<String, dynamic>? args]) async {
    try {
      return await _ch.invokeMethod(method, args) as T;
    } on PlatformException catch (e) {
      throw ApiError(e.code, e.message ?? e.code);
    }
  }

  Future<dynamic> _json(String method, [Map<String, dynamic>? args]) async => jsonDecode(await _call<String>(method, args));

  @override
  Future<ServerList> servers() async {
    final j = await _json('servers') as Map<String, dynamic>;
    return ServerList(j['current'] as String?,
        (j['servers'] as List).map((e) => Server.fromJson(e as Map<String, dynamic>)).toList());
  }

  @override
  Future<Probe> probe(String input, {String? password}) async {
    final j = await _json('probe', {'input': input, 'password': password}) as Map<String, dynamic>;
    return Probe(j['url'] as String, (j['name'] as String?) ?? '', (j['version'] as String?) ?? '');
  }

  @override
  Future<String> saveServer({String? id, required String url, required String name, String version = '', String? password}) =>
      _call<String>('saveServer', {'id': id, 'url': url, 'name': name, 'version': version, 'password': password});

  @override
  Future<void> removeServer(String id) => _call<void>('removeServer', {'id': id});

  @override
  Future<void> select(String id) => _call<void>('select', {'id': id});

  @override
  Future<List<Device>> devices(String server) async {
    final j = await _json('devices', {'server': server}) as Map<String, dynamic>;
    return (j['devices'] as List).map((e) => Device.fromJson(e as Map<String, dynamic>)).toList();
  }

  @override
  Future<String> setKey(String server, String device, String key) =>
      _call<String>('setKey', {'server': server, 'device': device, 'key': key});

  @override
  Future<void> forgetKey(String server, String device) => _call<void>('forgetKey', {'server': server, 'device': device});

  @override
  Future<DeviceCheck> check(String server, String device) async {
    final j = await _json('check', {'server': server, 'device': device}) as Map<String, dynamic>;
    final ports = j['ports'];
    return DeviceCheck((j['name'] as String?) ?? '', (j['version'] as String?) ?? '',
        ports is List ? ports.map((p) => (p as num).toInt()).toList() : null, (j['transport'] as String?) ?? '');
  }

  @override
  Future<List<PortInfo>> ports(String server, String device) async {
    final j = await _json('ports', {'server': server, 'device': device}) as List;
    return j.map((e) {
      final m = e as Map<String, dynamic>;
      return PortInfo((m['port'] as num).toInt(), (m['addr'] as String?) ?? '', (m['proc'] as String?) ?? '');
    }).toList();
  }

  @override
  Future<List<Recent>> recents(String server) async {
    final j = await _json('recents', {'server': server}) as List;
    return j.map((e) => Recent.fromJson(e as Map<String, dynamic>)).toList();
  }

  @override
  Future<void> removeRecent(String server, String device, int port) =>
      _call<void>('removeRecent', {'server': server, 'device': device, 'port': port});

  @override
  Future<void> open(String server, String device, String name, int port) =>
      _call<String>('open', {'server': server, 'device': device, 'name': name, 'port': port});
}
