import 'package:arstro_devices/main.dart';
import 'package:arstro_devices/src/api.dart';
import 'package:arstro_devices/src/device_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// The native pages of Arstro Devices against a fake Kotlin side (the real one is covered by the
/// JVM tests and the emulator test, docs/requirements.md MGR).
class FakeApi implements DevicesApi {
  final calls = <String>[];
  List<Server> serverList = [];
  String? current;
  String? needPassword;                 // probe wants this password
  final keys = <String, String>{};
  String goodKey = 'ak1_${'A' * 43}';
  final recentList = <Recent>[];
  Set<int> answering = {8180, 8181, 22};
  List<Device> deviceList = [];

  Device _withKey(Device d) => Device(
      id: d.id, name: d.name, hostname: d.hostname, platform: d.platform, version: d.version, slot: d.slot, web: d.web,
      online: d.online, since: d.since, lastSeen: d.lastSeen, viewers: d.viewers, hasKey: keys.containsKey(d.id),
      keyId: keys.containsKey(d.id) ? 'kid123' : null, machine: d.machine);

  @override
  Future<ServerList> servers() async => ServerList(current, serverList);

  @override
  Future<Probe> probe(String input, {String? password}) async {
    calls.add('probe $input');
    if (input.trim().isEmpty) throw ApiError('bad_input', 'type the device manager\'s address');
    if (needPassword != null && password != needPassword) {
      throw ApiError('password', password == null ? 'This device manager has a viewer password.' : 'Wrong viewer password.');
    }
    return Probe('https://${input.trim()}/arstro/internal/devices', 'Devices', '1.1.3');
  }

  @override
  Future<String> saveServer({String? id, required String url, required String name, String version = '', String? password}) async {
    calls.add('save $url pw=${password != null}');
    final s = Server(id: id ?? 's1', url: url, name: name, label: Uri.parse(url).host, version: version, hasPassword: password != null);
    serverList = [...serverList.where((x) => x.id != s.id), s];
    current = s.id;
    return s.id;
  }

  @override
  Future<void> removeServer(String id) async => serverList.removeWhere((s) => s.id == id);

  @override
  Future<void> select(String id) async => current = id;

  @override
  Future<List<Device>> devices(String server) async => deviceList.map(_withKey).toList();

  @override
  Future<String> setKey(String server, String device, String key) async {
    calls.add('setKey $device');
    if (!key.startsWith('ak1_')) throw ApiError('bad_key', 'an access key starts with ak1_');
    keys[device] = key;
    return 'kid123';
  }

  @override
  Future<void> forgetKey(String server, String device) async {
    calls.add('forgetKey $device');
    keys.remove(device);
  }

  @override
  Future<DeviceCheck> check(String server, String device) async {
    calls.add('check $device');
    if (keys[device] != goodKey) throw ApiError('wrong_key', 'The access key is wrong for this device.');
    return DeviceCheck('PC', '2.6.0', null, 'http');
  }

  @override
  Future<List<PortInfo>> ports(String server, String device) async =>
      [PortInfo(8180, '0.0.0.0', 'python3'), PortInfo(22, '0.0.0.0', 'sshd')];

  @override
  Future<List<Recent>> recents(String server) async => List.of(recentList);

  @override
  Future<void> removeRecent(String server, String device, int port) async =>
      recentList.removeWhere((r) => r.device == device && r.port == port);

  @override
  Future<void> open(String server, String device, String name, int port) async {
    calls.add('open $device $port');
    if (!keys.containsKey(device)) throw ApiError('no_key', 'No access key for this device yet.');
    if (keys[device] != goodKey) throw ApiError('wrong_key', 'The access key is wrong for this device.');
    if (!answering.contains(port)) throw ApiError('refused', 'nothing listens on port $port');
    // the port answered: it becomes the newest recent (Hub.open does this on the Kotlin side)
    recentList.removeWhere((r) => r.device == device && r.port == port);
    recentList.insert(0, Recent(device: device, name: name, port: port, at: DateTime.now()));
  }
}

final now = DateTime.now();

List<Device> sampleDevices() => [
      Device(id: 'aviron1-a-xpr0at', name: 'aviron1', hostname: 'aviron1', platform: 'Linux x86_64', version: '2.6.0',
          slot: 'a', web: 8180, online: true, since: now.subtract(const Duration(hours: 3)), viewers: 1),
      Device(id: 'aviron1-backup-rdht75', name: 'aviron1 (backup terminal)', hostname: 'aviron1', slot: 'backup', web: 8181, version: 'backup-1.0.0',
          online: true, since: now.subtract(const Duration(hours: 20))),
      Device(id: 'pi-a-loamop', name: 'orangepi5plus', hostname: 'orangepi5plus', version: '2.6.0', slot: 'a', web: 8080,
          online: false, lastSeen: now.subtract(const Duration(days: 2))),
      Device(id: 'tecno-phone-ab12cd', name: 'TECNO CM5', hostname: 'TECNO CM5', slot: 'phone', version: '2.4.0', web: 8080, online: true),
    ];

final pc = Machine(id: '53ecff00aa11bb22cc33dd44ee55ff66', model: 'MSI PRO X870-P WIFI', cpu: 'AMD Ryzen 9 9900X', cores: 24,
    memory: 64833409024, os: 'Ubuntu 22.04.5 LTS', kernel: '6.12.94-generic', arch: 'x86_64');
final pi = Machine(id: '0a1b2c3d4e5f60718293a4b5c6d7e8f9', model: 'Orange Pi 5 Plus', cpu: 'Rockchip RK3588', cores: 8,
    memory: 16508760064, os: 'Ubuntu 22.04.5 LTS', arch: 'aarch64');

/// Devices of the current versions (RLY-29): each tells the device manager its machine.
List<Device> machineDevices() => [
      Device(id: 'aviron1-backup-rdht75', name: 'aviron1 (backup terminal)', hostname: 'aviron1', slot: 'backup', web: 8181,
          version: 'backup-1.2.0', online: true, since: now.subtract(const Duration(hours: 20)), machine: pc),
      Device(id: 'aviron1-a-xpr0at', name: 'aviron1', hostname: 'aviron1', platform: 'Linux x86_64', version: '2.5.0', slot: 'a',
          web: 8180, online: false, lastSeen: now.subtract(const Duration(minutes: 5)), machine: pc),
      Device(id: 'pi-a-loamop', name: 'orangepi5plus', hostname: 'orangepi5plus', version: '2.5.0', slot: 'a', web: 8080,
          online: true, machine: pi),
      Device(id: 'tecno-phone-ab12cd', name: 'TECNO CM5', hostname: 'TECNO CM5', slot: 'phone', version: '2.4.0', web: 8080, online: true),
    ];

/// The device page's own list (the home page's lists stay in the tree under it).
final devicePageList = find.descendant(of: find.byType(DevicePage), matching: find.byType(Scrollable)).first;

Future<void> pumpApp(WidgetTester tester, FakeApi api, {Size size = const Size(390, 844)}) async {
  tester.view.physicalSize = size * 3;
  tester.view.devicePixelRatio = 3;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(DevicesApp(api: api));
  await tester.pumpAndSettle();
}

FakeApi withServer() => FakeApi()
  ..serverList = [Server(id: 's1', url: 'https://arstro.example.com/arstro/internal/devices', name: 'Devices', label: 'arstro.example.com')]
  ..current = 's1'
  ..deviceList = sampleDevices();

void main() {
  // covers: MGR-01
  testWidgets('first run: just the domain finds the device manager, a password only when it has one', (tester) async {
    final api = FakeApi()..needPassword = 'viewer-pw';
    await pumpApp(tester, api);
    expect(find.text('Add a device manager'), findsOneWidget);
    expect(find.byKey(const Key('password')), findsNothing);
    await tester.enterText(find.byKey(const Key('address')), 'arstro.example.com');
    await tester.tap(find.byKey(const Key('connect')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('password')), findsOneWidget);
    expect(find.text('This device manager has a viewer password.'), findsOneWidget);
    await tester.enterText(find.byKey(const Key('password')), 'viewer-pw');
    api.deviceList = sampleDevices();
    await tester.tap(find.byKey(const Key('connect')));
    await tester.pumpAndSettle();
    expect(api.calls, contains('save https://arstro.example.com/arstro/internal/devices pw=true'));
    expect(find.text('arstro.example.com'), findsOneWidget);          // the home page's subtitle
    expect(find.text('aviron1'), findsWidgets);
  });

  // covers: MGR-02
  testWidgets('the list: online first, kinds, no-key marks, offline last', (tester) async {
    final api = withServer()..keys['aviron1-a-xpr0at'] = 'ak1_${'A' * 43}';
    await pumpApp(tester, api);
    expect(find.text('ONLINE · 3'), findsOneWidget);
    expect(find.text('OFFLINE · 1'), findsOneWidget);
    final online = tester.getTopLeft(find.byKey(const Key('device-tecno-phone-ab12cd'))).dy;
    final offline = tester.getTopLeft(find.byKey(const Key('device-pi-a-loamop'))).dy;
    expect(online, lessThan(offline));
    expect(find.byIcon(Icons.smartphone), findsOneWidget);
    expect(find.byIcon(Icons.terminal), findsOneWidget);
    expect(find.text(' no key'), findsNWidgets(3));                   // every device but the one with a key
    expect(find.textContaining('last seen 2 d ago'), findsOneWidget);
    expect(find.textContaining('slot PHONE'), findsNothing);
    expect(find.text('aviron1 · backup terminal · v1.0.0'), findsOneWidget);
    expect(find.text('slot A · v2.6.0'), findsNWidgets(2));           // the host name equals the name: not twice
    expect(find.byKey(const Key('open-pi-a-loamop')), findsNothing);  // offline: no Open button
  });

  // covers: MGR-02, MGR-04, MGR-05
  testWidgets('Open without a key asks for it, checks it, opens the web port and lists it as recent', (tester) async {
    final api = withServer();
    await pumpApp(tester, api);
    expect(find.text('RECENT'), findsNothing);
    await tester.tap(find.byKey(const Key('open-aviron1-a-xpr0at')));
    await tester.pumpAndSettle();
    expect(find.text('Access key of aviron1'), findsOneWidget);
    expect(find.textContaining('arstro-remote relay key'), findsOneWidget);
    // a wrong key is refused and not kept
    await tester.enterText(find.byKey(const Key('key')), 'ak1_${'B' * 43}');
    await tester.tap(find.byKey(const Key('save-key')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('key-error')), findsOneWidget);
    expect(api.keys.containsKey('aviron1-a-xpr0at'), isFalse);
    await tester.enterText(find.byKey(const Key('key')), api.goodKey);
    await tester.tap(find.byKey(const Key('save-key')));
    await tester.pumpAndSettle();
    expect(api.calls.where((c) => c.startsWith('open')), ['open aviron1-a-xpr0at 8180']);
    expect(find.text('RECENT'), findsOneWidget);
    expect(find.byKey(const Key('recent-aviron1-a-xpr0at-8180')), findsOneWidget);
  });

  // covers: MGR-05
  testWidgets('a recent port opens with one tap; a port that does not answer is not remembered', (tester) async {
    final api = withServer()..keys['aviron1-a-xpr0at'] = 'ak1_${'A' * 43}';
    api.recentList.add(Recent(device: 'aviron1-a-xpr0at', name: 'aviron1', port: 8181, title: 'Backup', at: now));
    await pumpApp(tester, api);
    await tester.tap(find.byKey(const Key('recent-aviron1-a-xpr0at-8181')));
    await tester.pumpAndSettle();
    expect(api.calls.last, 'open aviron1-a-xpr0at 8181');
    // the device page: a typed port that nothing answers -> the reason, not a recent
    await tester.tap(find.byKey(const Key('device-aviron1-a-xpr0at')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('device-recent-8181')), findsOneWidget);
    await tester.scrollUntilVisible(find.byKey(const Key('port')), 200, scrollable: devicePageList);
    await tester.enterText(find.byKey(const Key('port')), '9999');
    await tester.tap(find.byKey(const Key('open-port')));
    await tester.pumpAndSettle();
    expect(find.text('nothing listens on port 9999'), findsOneWidget);
    expect(api.recentList.any((r) => r.port == 9999), isFalse);
    await tester.enterText(find.byKey(const Key('port')), '22');
    await tester.tap(find.byKey(const Key('open-port')));
    await tester.pumpAndSettle();
    expect(api.recentList.first.port, 22);
  });

  // covers: MGR-04
  testWidgets('a wrong saved key offers to replace it', (tester) async {
    final api = withServer()..keys['aviron1-a-xpr0at'] = 'ak1_${'C' * 43}';
    await pumpApp(tester, api);
    await tester.tap(find.byKey(const Key('open-aviron1-a-xpr0at')));
    await tester.pumpAndSettle();
    expect(find.text('The access key is wrong for this device.'), findsOneWidget);
    await tester.tap(find.text('Replace key'));
    await tester.pumpAndSettle();
    expect(find.text('Access key of aviron1'), findsOneWidget);
  });

  // covers: MGR-02
  testWidgets('device page: key check, listening ports, forget', (tester) async {
    final api = withServer()..keys['aviron1-a-xpr0at'] = 'ak1_${'A' * 43}';
    await pumpApp(tester, api);
    await tester.tap(find.byKey(const Key('device-aviron1-a-xpr0at')));
    await tester.pumpAndSettle();
    expect(find.textContaining('fingerprint kid123'), findsOneWidget);
    await tester.tap(find.byKey(const Key('check')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('check-ok')), findsOneWidget);
    await tester.scrollUntilVisible(find.byKey(const Key('load-ports')), 200, scrollable: devicePageList);
    await tester.tap(find.byKey(const Key('load-ports')));
    await tester.pumpAndSettle();
    expect(find.text('sshd'), findsOneWidget);
    await tester.tap(find.text('sshd'));
    await tester.pumpAndSettle();
    expect(api.calls.last, 'open aviron1-a-xpr0at 22');
    await tester.scrollUntilVisible(find.text('Forget'), -200, scrollable: devicePageList);
    await tester.tap(find.text('Forget'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Forget'));
    await tester.pumpAndSettle();
    expect(api.keys, isEmpty);
    expect(find.byKey(const Key('key-state')), findsOneWidget);
    expect(find.textContaining('No key yet'), findsOneWidget);
  });

  // covers: MGR-01
  testWidgets('switching device managers from the title', (tester) async {
    final api = withServer();
    api.serverList.add(Server(id: 's2', url: 'http://192.0.2.10:8090/arstro/internal/devices', name: 'Devices', label: '192.0.2.10:8090'));
    await pumpApp(tester, api);
    await tester.tap(find.byKey(const Key('server-switch')));
    await tester.pumpAndSettle();
    expect(find.text('Device managers'), findsOneWidget);
    api.deviceList = [];
    await tester.tap(find.text('192.0.2.10:8090'));
    await tester.pumpAndSettle();
    expect(api.current, 's2');
    expect(find.textContaining('No devices yet'), findsOneWidget);
  });

  // covers: MGR-02, UX-01
  testWidgets('fits a 360 px wide phone', (tester) async {
    final api = withServer()..keys['aviron1-a-xpr0at'] = 'ak1_${'A' * 43}';
    api.recentList.add(Recent(device: 'aviron1-a-xpr0at', name: 'aviron1', port: 8180, title: 'Arstro Remote · aviron1', at: now));
    await pumpApp(tester, api, size: const Size(360, 640));
    expect(tester.takeException(), isNull);
    await tester.tap(find.byKey(const Key('device-aviron1-a-xpr0at')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });

  // covers: MGR-07
  test('devices are grouped by machine like the device manager does; RAM in the sizes sold', () {
    final groups = groupByMachine(machineDevices());
    expect(groups.map((g) => g.name), ['aviron1', 'orangepi5plus', 'TECNO CM5']);
    expect(groups.first.devices.map((d) => d.id), ['aviron1-a-xpr0at', 'aviron1-backup-rdht75']);   // slots first
    expect(groups.first.online, 1);
    expect(groups.first.id, 'm-${pc.id}');
    expect(groups.last.id, 'd-tecno-phone-ab12cd');                    // no machine: an item of its own
    expect(machineBits(pc), ['MSI PRO X870-P WIFI', 'AMD Ryzen 9 9900X', '24 cores', '64 GB', 'Ubuntu 22.04.5 LTS x86_64']);
    expect(memoryText(16508760064), '16 GB');
    expect(memoryText(8 * 1073741824 - 300 * 1048576), '8 GB');
    expect(memoryText(512 * 1048576), '512 MB');
    expect(Machine.fromJson({'id': ''}), isNull);
    expect(Machine.fromJson({'id': 'abcdef0123', 'cores': 'x'})!.cores, isNull);
  });

  // covers: MGR-07
  testWidgets('one item per machine: Main and Backup terminal together, the switch and the Machine card', (tester) async {
    final api = withServer()..deviceList = machineDevices();
    await pumpApp(tester, api);
    expect(find.text('ONLINE · 3'), findsOneWidget);                   // 3 items for 4 devices
    expect(find.text('OFFLINE · 1'), findsNothing);
    final item = find.byKey(Key('machine-m-${pc.id}'));
    expect(item, findsOneWidget);
    expect(find.descendant(of: item, matching: find.text('aviron1')), findsOneWidget);
    expect(find.descendant(of: item, matching: find.text('1 of 2 online')), findsOneWidget);
    expect(find.text('MSI PRO X870-P WIFI · AMD Ryzen 9 9900X · 24 cores · 64 GB · Ubuntu 22.04.5 LTS x86_64'), findsOneWidget);
    expect(find.descendant(of: item, matching: find.byKey(const Key('device-aviron1-a-xpr0at'))), findsOneWidget);
    expect(find.descendant(of: item, matching: find.byKey(const Key('device-aviron1-backup-rdht75'))), findsOneWidget);
    expect(find.descendant(of: item, matching: find.text('Main')), findsOneWidget);
    expect(find.descendant(of: item, matching: find.text('slot A')), findsOneWidget);
    expect(find.descendant(of: item, matching: find.text('Backup terminal')), findsOneWidget);
    final main = tester.getTopLeft(find.byKey(const Key('device-aviron1-a-xpr0at'))).dy;
    final backup = tester.getTopLeft(find.byKey(const Key('device-aviron1-backup-rdht75'))).dy;
    expect(main, lessThan(backup));                                    // Main first, even while it is down
    expect(find.byKey(const Key('open-aviron1-a-xpr0at')), findsNothing);       // offline: no Open
    expect(find.byKey(const Key('open-aviron1-backup-rdht75')), findsOneWidget);
    // a machine on its own: one row with its model; a device that names no machine: as before
    expect(find.byKey(Key('machine-m-${pi.id}')), findsNothing);
    expect(find.text('Orange Pi 5 Plus · slot A · v2.5.0'), findsOneWidget);
    expect(find.byKey(const Key('device-tecno-phone-ab12cd')), findsOneWidget);

    // the device page: switch from the backup to Main, and what the machine is
    await tester.tap(find.byKey(const Key('device-aviron1-backup-rdht75')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('sib-aviron1-a-xpr0at')), findsOneWidget);
    expect(find.byKey(const Key('sib-aviron1-backup-rdht75')), findsOneWidget);
    expect(find.text('Open the backup terminal (port 8181)'), findsOneWidget);
    await tester.tap(find.byKey(const Key('sib-aviron1-a-xpr0at')));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(AppBar, 'aviron1'), findsOneWidget);
    expect(find.text('Open the web UI (port 8180)'), findsOneWidget);
    expect(find.textContaining('last seen 5 min ago'), findsOneWidget);
    await tester.scrollUntilVisible(find.byKey(const Key('machine-card')), 200, scrollable: devicePageList);
    expect(find.descendant(of: find.byKey(const Key('machine-card')), matching: find.text('64 GB')), findsOneWidget);
    expect(find.descendant(of: find.byKey(const Key('machine-card')), matching: find.text('AMD Ryzen 9 9900X')), findsOneWidget);
    expect(find.text('Linux x86_64'), findsNothing);                   // the machine's system instead
    // the phone has no machine: no switch, no Machine card
    await tester.pageBack();
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('device-tecno-phone-ab12cd')));
    await tester.pumpAndSettle();
    expect(find.byType(DevicePage), findsOneWidget);
    expect(find.byKey(const Key('sib-tecno-phone-ab12cd')), findsNothing);
    expect(find.byKey(const Key('machine-card')), findsNothing);
  });

  // covers: MGR-07, UX-01
  testWidgets('a machine fits a 360 px wide phone', (tester) async {
    final api = withServer()..deviceList = machineDevices();
    await pumpApp(tester, api, size: const Size(360, 640));
    expect(tester.takeException(), isNull);
    await tester.tap(find.byKey(const Key('device-aviron1-a-xpr0at')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.byKey(const Key('sib-aviron1-backup-rdht75')), findsOneWidget);
  });
}
