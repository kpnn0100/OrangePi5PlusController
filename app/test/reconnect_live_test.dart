// Live test against a running Pi daemon (skipped unless ARSTRO_TCP is set).
//
//   ssh -N -L 127.0.0.1:7788:/run/user/<uid>/arstro-remote.sock <user>@<pi> &
//   ARSTRO_TCP=127.0.0.1:7788 flutter test test/reconnect_live_test.dart
//
// It drives the real RemoteClient / TerminalHub / FrameDecoder through a proxy
// that cuts the link at random moments (bytes in flight are lost, like a real
// Bluetooth drop) and checks that the shell output ends up complete, in order and
// without duplicates after the automatic reconnects.
import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:arstro_remote/src/bt/bluetooth.dart';
import 'package:arstro_remote/src/remote_client.dart';
import 'package:arstro_remote/src/terminal_hub.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class ChaosProxy {
  ChaosProxy(this.host, this.port);
  final String host;
  final int port;
  late ServerSocket _server;
  final List<Socket> _open = [];
  int cuts = 0;

  Future<int> start() async {
    _server = await ServerSocket.bind('127.0.0.1', 0);
    _server.listen(_accept);
    return _server.port;
  }

  Future<void> _accept(Socket client) async {
    final Socket upstream;
    try {
      upstream = await Socket.connect(host, port);
    } catch (_) {
      client.destroy();
      return;
    }
    _open.addAll([client, upstream]);
    client.listen(upstream.add, onDone: upstream.destroy, onError: (_) => upstream.destroy());
    upstream.listen(client.add, onDone: client.destroy, onError: (_) => client.destroy());
  }

  void cut() {
    cuts++;
    for (final s in _open) {
      s.destroy();
    }
    _open.clear();
  }

  Future<void> close() async {
    cut();
    await _server.close();
  }
}

Future<void> waitFor(bool Function() cond, {Duration timeout = const Duration(seconds: 20), String? what}) async {
  final end = DateTime.now().add(timeout);
  while (!cond()) {
    if (DateTime.now().isAfter(end)) throw TimeoutException('timed out waiting for ${what ?? 'condition'}');
    await Future.delayed(const Duration(milliseconds: 50));
  }
}

String screen(TermSession s) => s.terminal.buffer.getText();

void main() {
  final target = Platform.environment['ARSTRO_TCP'];
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    final m = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    m.setMockStreamHandler(const EventChannel('arstro/bt/events'), MockStreamHandler.inline(onListen: (_, _) {}));
    m.setMockStreamHandler(const EventChannel('arstro/bt/data'), MockStreamHandler.inline(onListen: (_, _) {}));
    m.setMockMethodCallHandler(const MethodChannel('arstro/bt'), (call) async => null);
  });

  test('shell output survives repeated link cuts: nothing lost, nothing repeated', () async {
    final parts = target!.split(':');
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final proxy = ChaosProxy(parts[0], int.parse(parts[1]));
    final port = await proxy.start();
    final client = RemoteClient(prefs);
    await client.connect(BtDevice(address: 'tcp:127.0.0.1:$port'));
    expect(client.state, LinkState.connected, reason: client.error);

    final s = await client.terminals.open();
    await waitFor(() => screen(s).contains(r'$'), what: 'prompt');
    const n = 600;
    s.sendText('for i in \$(seq 1 $n); do echo L\$i; sleep 0.008; done; echo DONE_MARKER\n');

    final rnd = Random(42);
    for (var k = 0; k < 5; k++) {
      await Future.delayed(Duration(milliseconds: 300 + rnd.nextInt(700)));
      proxy.cut();
      await waitFor(() => client.state != LinkState.connected, timeout: const Duration(seconds: 5), what: 'drop');
      await waitFor(() => client.isConnected && s.status == TermStatus.live,
          timeout: const Duration(seconds: 30), what: 'reconnect #$k');
    }
    await waitFor(() => RegExp(r'^DONE_MARKER\s*$', multiLine: true).hasMatch(screen(s)),
        timeout: const Duration(seconds: 60), what: 'loop end');

    final text = screen(s);
    final got = RegExp(r'^L(\d+)\s*$', multiLine: true).allMatches(text).map((m) => int.parse(m.group(1)!)).toList();
    expect(got, List.generate(n, (i) => i + 1), reason: 'output lines lost/duplicated/reordered');
    final flat = text.replaceAll('\n', '');
    expect('echo DONE_MARKER'.allMatches(flat).length, 1, reason: 'command echo repeated');
    expect(proxy.cuts, 5);

    await client.terminals.close(s);
    await client.disconnect();
    await proxy.close();
    client.dispose();
  }, skip: target == null ? 'set ARSTRO_TCP=host:port to run against a live daemon' : false,
      timeout: const Timeout(Duration(minutes: 3)));
}
