// Live test of the link to another A/B slot (TERM-06), skipped unless ARSTRO_WS is set:
//
//   ARSTRO_WS=ws://<pi>:8081/ws ARSTRO_WS_TOKEN=<that slot's password> \
//     flutter test test/slot_link_live_test.dart
//
// It drives the app's own RemoteClient / TerminalHub over WebSocketTransport - exactly what
// the Terminal tab uses for the slot that is not on the phone's Bluetooth link.
// covers: TERM-06
import 'dart:io';

import 'package:arstro_remote/src/bt/bluetooth.dart';
import 'package:arstro_remote/src/remote_client.dart';
import 'package:arstro_remote/src/terminal_hub.dart';
import 'package:arstro_remote/src/transport.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final url = Platform.environment['ARSTRO_WS'];
  final token = Platform.environment['ARSTRO_WS_TOKEN'];

  setUp(() {
    final m = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    m.setMockStreamHandler(const EventChannel('arstro/bt/events'), MockStreamHandler.inline(onListen: (_, _) {}));
    m.setMockStreamHandler(const EventChannel('arstro/bt/data'), MockStreamHandler.inline(onListen: (_, _) {}));
    m.setMockMethodCallHandler(const MethodChannel('arstro/bt'), (call) async => null);
  });

  test('a shell on the other slot over Wi-Fi: open, type, see the output, close', () async {
    HttpOverrides.global = null; // real sockets (the test binding fakes HTTP, and so WebSockets)
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final uri = Uri.parse(url!);

    final c = RemoteClient(prefs, primary: false);
    await c.connect(BtDevice(address: url, name: 'peer'), transport: WebSocketTransport(uri, token));
    expect(c.isConnected, isTrue, reason: c.error);
    expect(prefs.getString('last_address'), isNull, reason: 'a link to another slot is never remembered');

    final s = await c.terminals.open();
    expect(s.status, TermStatus.live);
    s.sendText('echo SLOT_LINK_\$((6*7))\n');
    final deadline = DateTime.now().add(const Duration(seconds: 10));
    while (DateTime.now().isBefore(deadline) && !s.terminal.buffer.getText().contains('SLOT_LINK_42')) {
      await Future<void>.delayed(const Duration(milliseconds: 100));
    }
    expect(s.terminal.buffer.getText(), contains('SLOT_LINK_42'));
    await c.terminals.close(s);

    final bad = RemoteClient(prefs, primary: false);
    await bad.connect(BtDevice(address: url, name: 'bad'), transport: WebSocketTransport(uri, 'not-the-password'));
    expect(bad.isConnected, isFalse, reason: 'a wrong password must be refused');

    await c.disconnect();
    await bad.disconnect();
    c.dispose();
    bad.dispose();
  },
      skip: url == null ? 'set ARSTRO_WS=ws://<pi>:<port>/ws and ARSTRO_WS_TOKEN to run against a live slot' : false,
      timeout: const Timeout(Duration(seconds: 60)));
}
