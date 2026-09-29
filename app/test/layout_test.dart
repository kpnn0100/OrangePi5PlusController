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
  final rec = jsonDecode(File('test/fixtures/recorder.json').readAsStringSync()) as Map<String, dynamic>;
  for (final t in ['recorder', 'recorder.settings', 'jobs', 'gallery', 'pairing', 'controllers', 'screen']) {
    client.topic(t).value = rec[t];
  }
  client.requestOverride = (op, params) async {
    if (op == 'gallery.get') {
      final takes = (rec['gallery.list'] as Map)['takes'] as List;
      return takes.firstWhere((t) => t['id'] == params['take']);
    }
    return rec[op] ?? <String, dynamic>{};
  };
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
    // covers: UX-03, UX-01
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

    // covers: UX-03, UX-01, STAT-01, INP-04, WIFI-01, TERM-01, SCR-04
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
      for (final tab in ['Recorder', 'Wi-Fi', 'Terminal', 'Remote', 'Dashboard']) {
        await tester.tap(find.text(tab).last);
        await tester.pump(const Duration(milliseconds: 400));
        log.check(tab);
      }
      await tester.tap(find.text('Remote').last);
      await tester.pump(const Duration(milliseconds: 300));
      log.found(find.text('Left'), 'left button');
      log.found(find.text('Right'), 'right button');
      log.found(find.text('Drag to move · two fingers to scroll'), 'touchpad');
      // Remote › Screen (SCR-04)
      await tester.tap(find.text('Screen').last);
      await tester.pump(const Duration(milliseconds: 400));
      log.found(find.text('1024×768'), 'remote screen size');
      log.check('remote screen');
      await tester.tap(find.text('Touchpad').last);
      await tester.pump(const Duration(milliseconds: 300));
      // let pending request timeouts (no transport in tests) run out
      await tester.pump(const Duration(seconds: 70));
      log.check('after timeouts');
      log.done();
    });
  }

  for (final entry in sizes.entries) {
    // covers: UX-03, UX-02, REC-01, REC-03, REC-04, REC-07, GAL-01, GAL-02, GAL-05, CON-02
    testWidgets('recorder: live, settings, gallery and a take fit on ${entry.key}', (tester) async {
      await setSize(tester, entry.value);
      final log = ErrorLog();
      final client = await connectedClient();
      final prefs = await SharedPreferences.getInstance();
      await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.text('Recorder').last);
      await tester.pump(const Duration(milliseconds: 400));
      log.found(find.text('RAW +HQ +FFV1'), 'mode card');
      log.found(find.text('Data rate'), 'recording metrics');
      log.found(find.text('REC_20260101_120000.arh'), 'recording file');
      log.check('live view');

      await tester.ensureVisible(find.text('RAW +HQ +FFV1'));
      await tester.tap(find.text('RAW +HQ +FFV1'));
      await tester.pump();
      await tester.pump(const Duration(seconds: 1));
      log.found(find.text('Recording format'), 'settings sheet');
      // (no scrollUntilVisible: it waits for animations to settle, and the record button pulses forever)
      final sheetScroll = find.descendant(of: find.byType(DraggableScrollableSheet), matching: find.byType(Scrollable)).first;
      for (var i = 0; i < 12 && find.text('Test pattern size (width×height@fps)').evaluate().isEmpty; i++) {
        await tester.drag(sheetScroll, const Offset(0, -300));
        await tester.pump(const Duration(milliseconds: 200));
      }
      log.found(find.text('Test pattern size (width×height@fps)'), 'end of settings sheet');
      log.check('settings sheet');
      Navigator.of(tester.element(find.byType(DraggableScrollableSheet))).pop();
      await tester.pump(const Duration(seconds: 1));

      await tester.tap(find.text('Gallery').first);
      await tester.pump(const Duration(milliseconds: 500));
      log.found(find.text('CONVERSIONS'), 'jobs card');
      final grid = find.descendant(of: find.byType(CustomScrollView), matching: find.byType(Scrollable)).first;
      for (var i = 0; i < 10 && find.text('23:59:59').hitTestable().evaluate().isEmpty; i++) {
        await tester.drag(grid, const Offset(0, -250));
        await tester.pump(const Duration(milliseconds: 200));
      }
      log.found(find.text('23:59:59'), 'take card');
      log.check('gallery');
      await tester.tap(find.text('23:59:59'));
      await tester.pump();
      await tester.pump(const Duration(seconds: 1));
      final takeScroll = find.descendant(of: find.byType(DraggableScrollableSheet), matching: find.byType(Scrollable)).first;
      for (var i = 0; i < 8 && find.text('REC_20251231_235959_H264_720p.mp4').evaluate().isEmpty; i++) {
        await tester.drag(takeScroll, const Offset(0, -200));
        await tester.pump(const Duration(milliseconds: 200));
      }
      log.found(find.text('Files'), 'take sheet');
      log.found(find.text('REC_20251231_235959_H264_720p.mp4'), 'variant');
      log.check('take sheet');
      await tester.pump(const Duration(seconds: 5));
      log.check('after timers');
      log.done();
    });
  }

  // covers: UX-03, ADM-01, ADM-02, ADM-03, CON-05
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
    await tester.scrollUntilVisible(find.text('Require a password'), 300);
    log.found(find.text('Open 10 min').evaluate().isEmpty ? find.text('Close') : find.text('Open 10 min'), 'pairing');
    log.check('settings');
    log.done();
    await tester.pump(const Duration(seconds: 70));
  });
}
