// Renders every screen at phone, tablet and ATS sizes; any RenderFlex overflow or
// exception fails the test.
import 'dart:convert';
import 'dart:io';

import 'package:arstro_remote/main.dart';
import 'package:arstro_remote/src/app_scope.dart';
import 'package:arstro_remote/src/bt/bluetooth.dart';
import 'package:arstro_remote/src/remote_client.dart';
import 'package:arstro_remote/src/ui/connect_page.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

const sizes = {
  'phone portrait': Size(390, 844),
  'small phone': Size(360, 640),
  'phone landscape': Size(844, 390),
  'tablet': Size(1280, 800),
  'ATS 1920x1080': Size(1920, 1080),
};

void mockChannels() {
  final m = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  m.setMockStreamHandler(const EventChannel('arstro/bt/events'), MockStreamHandler.inline(onListen: (_, _) {}));
  m.setMockStreamHandler(const EventChannel('arstro/bt/data'), MockStreamHandler.inline(onListen: (_, _) {}));
  m.setMockMethodCallHandler(const MethodChannel('arstro/bt'), (call) async {
    switch (call.method) {
      case 'getState':
        return {'supported': true, 'enabled': true, 'permissions': true, 'sdk': 35, 'model': 'test'};
      case 'bondedDevices':
        return [
          {'name': 'Arstro-mypi', 'address': '00:11:22:33:44:55', 'bonded': true},
          {'name': 'Some headphones with a very long Bluetooth name indeed', 'address': '00:11:22:33:44:55', 'bonded': true},
        ];
      default:
        return true;
    }
  });
}

Future<RemoteClient> connectedClient() async {
  SharedPreferences.setMockInitialValues({'auto_connect': false});
  final client = RemoteClient(await SharedPreferences.getInstance());
  client.device = const BtDevice(address: '00:11:22:33:44:55', name: 'Arstro-mypi');
  client.server = {'hostname': 'mypi', 'version': '1.0.0', 'proto': 1, 'input': {'available': true}};
  client.state = LinkState.connected;
  final stats = jsonDecode(File('test/fixtures/stats.json').readAsStringSync()) as Map<String, dynamic>;
  for (var i = 0; i < 40; i++) {
    client.cpuHistory.add(10 + (i % 7) * 6);
    client.tempHistory.add(45 + (i % 5).toDouble());
  }
  client.stats.value = stats;
  return client;
}

Future<void> setSize(WidgetTester tester, Size size) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
}

/// Collects framework errors (overflows etc.) with their source location. The
/// binding insists that FlutterError.onError is restored before a test fails,
/// so [check] restores it before failing and [done] at the end of the test.
class ErrorLog {
  ErrorLog() {
    _previous = FlutterError.onError;
    FlutterError.onError = (d) {
      final text = d.toString();
      final at = RegExp(r'lib/src/[\w/]+\.dart:\d+:\d+').firstMatch(text)?.group(0) ?? '?';
      errors.add('${d.exceptionAsString().split('\n').first}  <-  $at');
    };
  }

  final errors = <String>[];
  FlutterExceptionHandler? _previous;

  void check(String where) {
    if (errors.isEmpty) return;
    done();
    fail('$where:\n  ${errors.toSet().join('\n  ')}');
  }

  /// Like expect(..., findsWidgets) but restores the handler before failing.
  void found(Finder f, String what) {
    if (f.evaluate().isEmpty) {
      done();
      fail('not found: $what');
    }
  }

  void done() => FlutterError.onError = _previous;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(mockChannels);

  for (final entry in sizes.entries) {
    testWidgets('connect page fits on ${entry.key}', (tester) async {
      await setSize(tester, entry.value);
      final log = ErrorLog();
      SharedPreferences.setMockInitialValues({'auto_connect': false});
      final prefs = await SharedPreferences.getInstance();
      final client = RemoteClient(prefs);
      await tester.pumpWidget(AppScope(
        client: client,
        settings: Settings(prefs),
        child: const MaterialApp(home: ConnectPage(allowAutoConnect: false)),
      ));
      await tester.pump(const Duration(milliseconds: 300));
      log.found(find.text('Arstro-mypi'), 'paired Pi');
      await tester.scrollUntilVisible(find.text('Connect by Bluetooth address…'), 200);
      log.found(find.text('Connect by Bluetooth address…'), 'connect by address');
      log.check('connect page');
      log.done();
    });

    testWidgets('all tabs fit on ${entry.key}', (tester) async {
      await setSize(tester, entry.value);
      final log = ErrorLog();
      final client = await connectedClient();
      final prefs = await SharedPreferences.getInstance();
      await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
      await tester.pump(const Duration(milliseconds: 400));
      // Dashboard
      log.found(find.text('10.0.0.42'), 'IP on dashboard');
      log.check('dashboard');
      for (final tab in ['Wi-Fi', 'Terminal', 'Remote', 'Dashboard']) {
        await tester.tap(find.text(tab).last);
        await tester.pump(const Duration(milliseconds: 400));
        log.check(tab);
      }
      await tester.tap(find.text('Remote').last);
      await tester.pump(const Duration(milliseconds: 300));
      log.found(find.text('Left'), 'left button');
      log.found(find.text('Right'), 'right button');
      log.found(find.text('Drag to move · two fingers to scroll'), 'touchpad');
      // let pending request timeouts (no transport in tests) run out
      await tester.pump(const Duration(seconds: 70));
      log.check('after timeouts');
      log.done();
    });
  }

  testWidgets('settings page fits on a small phone', (tester) async {
    await setSize(tester, const Size(360, 640));
    final log = ErrorLog();
    final client = await connectedClient();
    final prefs = await SharedPreferences.getInstance();
    await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.byTooltip('Settings').first);
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    log.found(find.text('Pointer speed'), 'settings');
    log.check('settings');
    log.done();
    await tester.pump(const Duration(seconds: 70));
  });
}
