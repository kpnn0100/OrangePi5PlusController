// The machine's hotspot from the app (WIFI-08) and the app's one link to the machine, Bluetooth
// (CON-06): the card starts the hotspot over the link (asking first when the machine is on a
// Wi-Fi network), follows the pushed state, hands the network to the phone's Wi-Fi and the web
// address to the phone's browser - and the release app has no way to the network itself.
import 'dart:io';

import 'package:arstro_remote/main.dart';
import 'package:arstro_remote/src/app_scope.dart';
import 'package:arstro_remote/src/remote_client.dart';
import 'package:arstro_remote/src/ui/recorder/common.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'layout_test.dart' show ErrorLog, connectedClient, mockChannels, setSize, sizes;

TestDefaultBinaryMessenger get messenger => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

/// The fixture's answers (the machine before anything changed).
late Future<dynamic> Function(String, Map<String, dynamic>) fixture;

Future<RemoteClient> openWifi(WidgetTester tester, List<(String, Map<String, dynamic>)> asked) async {
  final client = await connectedClient();
  fixture = client.requestOverride!;
  client.requestOverride = (op, p) async {
    asked.add((op, p));
    if (op == 'wifi.hotspot.start') {
      final h = {...asMap(await fixture('wifi.hotspot', const {})), ...p, 'active': true, 'ip': '10.42.0.1',
                 'web': 'http://10.42.0.1:8080/', 'clients': 0};
      if (p['band'] == '5') h['band'] = '5 GHz';
      return {'message': 'Hotspot ${h['ssid']} is on', 'hotspot': h};
    }
    if (op == 'wifi.hotspot.stop') return {'message': 'Hotspot off, back on Home'};
    if (op == 'wifi.status') return client.topic('wifi').value;      // what the machine pushed last
    return fixture(op, p);
  };
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
  await tester.pump(const Duration(milliseconds: 300));
  // the tab in the navigation (a bar on phones, a rail on wide screens - the dashboard says Wi-Fi too)
  final nav = find.byType(NavigationRail).evaluate().isNotEmpty ? find.byType(NavigationRail) : find.byType(NavigationBar);
  await tester.tap(find.descendant(of: nav, matching: find.text('Wi-Fi')));
  await tester.pump(const Duration(milliseconds: 500));
  return client;
}

Future<void> tapOn(WidgetTester tester, Finder f) async {
  await tester.ensureVisible(f);
  await tester.pump(const Duration(milliseconds: 200));
  await tester.tap(f);
  await tester.pump(const Duration(milliseconds: 400));
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(mockChannels);

  for (final size in ['phone portrait', 'small phone', 'tablet']) {
    // covers: WIFI-08, CON-06, UX-03
    testWidgets('hotspot: start over Bluetooth, join it, open the web UI, stop - on $size', (tester) async {
      await setSize(tester, sizes[size]!);
      final log = ErrorLog();
      final asked = <(String, Map<String, dynamic>)>[];
      final phone = <MethodCall>[];
      messenger.setMockMethodCallHandler(const MethodChannel('arstro/phone'), (call) async {
        phone.add(call);
        return call.method == 'joinWifi' ? 'saved' : true;
      });
      final client = await openWifi(tester, asked);
      log.found(find.text('Arstro-mypi'), 'the hotspot name');
      log.found(find.textContaining('Off · 2.4 GHz'), 'off, its band');
      expect(tester.widget<Text>(find.byKey(const Key('hotspot-password'))).data, isNot(contains('k7mvq')),
          reason: 'the password is hidden until asked for');
      log.check('hotspot off');

      // the machine is on "Home": starting asks first, then goes over the Bluetooth link
      await tapOn(tester, find.byKey(const Key('hotspot-start')));
      log.found(find.text('Start the hotspot?'), 'the question');
      log.found(find.textContaining('The Pi leaves Home'), 'why it asks');
      await tester.tap(find.widgetWithText(FilledButton, 'Start'));
      await tester.pump(const Duration(milliseconds: 500));
      expect(asked.where((a) => a.$1 == 'wifi.hotspot.start').single.$2, <String, dynamic>{});

      // the machine pushes the new Wi-Fi state (WIFI-07): every view follows it
      client.topic('wifi').value = {
        ...asMap(client.topic('wifi').value),
        'connected': false,
        'ssid': null,
        'hotspot': {'active': true, 'ssid': 'Arstro-mypi', 'band': '2.4 GHz', 'ip': '10.42.0.1', 'clients': 1,
                    'web': 'http://10.42.0.1:8080/'},
      };
      await tester.pump(const Duration(milliseconds: 400));
      log.found(find.text('Hotspot Arstro-mypi'), 'the status card says the machine runs a hotspot');
      log.found(find.textContaining('On · 2.4 GHz · 1 device joined'), 'on, who joined');
      log.found(find.text('http://10.42.0.1:8080/'), 'the web address inside the hotspot');
      await tapOn(tester, find.byTooltip('Show'));
      expect(tester.widget<Text>(find.byKey(const Key('hotspot-password'))).data, 'k7mvq2xh9tpa');
      log.check('hotspot on');

      await tapOn(tester, find.text('Join on this phone'));
      expect(phone.last.method, 'joinWifi');
      expect(phone.last.arguments, {'ssid': 'Arstro-mypi', 'password': 'k7mvq2xh9tpa'});
      await tapOn(tester, find.text('Open web UI'));
      expect(phone.last.method, 'openUrl');
      expect(phone.last.arguments, {'url': 'http://10.42.0.1:8080/'});

      await tapOn(tester, find.byKey(const Key('hotspot-stop')));
      expect(asked.any((a) => a.$1 == 'wifi.hotspot.stop'), isTrue);
      client.topic('wifi').value = asMap(await fixture('wifi.status', const {}));
      await tester.pump(const Duration(milliseconds: 400));
      log.found(find.byKey(const Key('hotspot-start')), 'off again');
      log.check('hotspot stopped');
      await tester.pump(const Duration(seconds: 5));
      log.done();
    });
  }

  // covers: WIFI-08
  testWidgets('hotspot settings: checked in the app, then name, password and band go to the machine', (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final log = ErrorLog();
    final asked = <(String, Map<String, dynamic>)>[];
    final client = await openWifi(tester, asked);
    client.topic('wifi').value = {...asMap(client.topic('wifi').value), 'connected': false, 'ssid': null};
    await tester.pump(const Duration(milliseconds: 200));
    await tapOn(tester, find.text('Settings'));
    final fields = find.byType(TextField);
    await tester.enterText(fields.at(1), 'short');
    await tester.pump();
    log.found(find.text('8 to 63 characters'), 'a short password is refused');
    expect(tester.widget<FilledButton>(find.widgetWithText(FilledButton, 'Save and start')).onPressed, isNull);
    await tester.enterText(fields.at(0), 'Set 4 hotspot');
    await tester.enterText(fields.at(1), 'a good long one');
    await tester.tap(find.text('5 GHz'));
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Save and start'));
    await tester.pump(const Duration(milliseconds: 500));
    // not on a network: nothing to leave, so no question
    expect(find.text('Start the hotspot?'), findsNothing);
    expect(asked.where((a) => a.$1 == 'wifi.hotspot.start').single.$2,
        {'ssid': 'Set 4 hotspot', 'password': 'a good long one', 'band': '5'});
    log.check('settings');
    await tester.pump(const Duration(seconds: 5));
    log.done();
  });

  // covers: CON-06
  test('the release app reaches the machine over Bluetooth only', () {
    final manifest = File('android/app/src/main/AndroidManifest.xml').readAsStringSync();
    expect(manifest, isNot(contains('android.permission.INTERNET')), reason: 'no network permission in the release');
    expect(manifest, isNot(contains('usesCleartextTraffic')));
    final pubspec = File('pubspec.yaml').readAsStringSync();
    for (final pkg in ['webview_flutter', 'video_player', 'http:', 'web_socket']) {
      expect(pubspec, isNot(contains(pkg)), reason: '$pkg reaches the network');
    }
    for (final f in Directory('lib').listSync(recursive: true).whereType<File>().where((f) => f.path.endsWith('.dart'))) {
      final src = f.readAsStringSync();
      for (final api in ['WebSocket', 'HttpClient', 'Image.network', 'NetworkImage', 'networkUrl']) {
        expect(src, isNot(contains(api)), reason: '${f.path} uses $api');
      }
      if (src.contains('Socket.connect')) {
        expect(f.path, endsWith('transport.dart'), reason: '${f.path} opens a socket');
        expect(src, contains("kDebugMode && address.startsWith('tcp:')"), reason: 'the TCP pipe is debug-only');
      }
    }
  });
}
