import 'dart:convert';
import 'dart:typed_data';

import 'package:arstro_remote/src/proto/frames.dart';
import 'package:arstro_remote/src/remote_client.dart';
import 'package:arstro_remote/src/terminal_hub.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

Uint8List b(String s) => Uint8List.fromList(utf8.encode(s));

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late TermSession s;

  setUp(() async {
    final m = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    m.setMockStreamHandler(const EventChannel('arstro/bt/events'), MockStreamHandler.inline(onListen: (_, _) {}));
    m.setMockStreamHandler(const EventChannel('arstro/bt/data'), MockStreamHandler.inline(onListen: (_, _) {}));
    SharedPreferences.setMockInitialValues({});
    final client = RemoteClient(await SharedPreferences.getInstance());
    s = TermSession(client.terminals, 1);
  });

  String text() => s.terminal.buffer.getText().replaceAll(RegExp(r'\s+$'), '');

  // covers: TERM-03, CON-01
  test('in-order chunks are shown once', () {
    s.onData(0, b('hello '));
    s.onData(6, b('world'));
    expect(text(), 'hello world');
    expect(s.received, 11);
  });

  // covers: TERM-03, CON-01
  test('replayed bytes that are already on screen are dropped', () {
    s.onData(0, b('abcdef'));
    s.onData(0, b('abcdef')); // exact duplicate
    s.onData(3, b('defGHI')); // overlaps by 3
    expect(text(), 'abcdefGHI');
    expect(s.received, 9);
  });

  // covers: TERM-03, CON-01
  test('overlap that splits a UTF-8 character keeps the text intact', () {
    final bytes = b('việt');
    s.onData(0, Uint8List.sublistView(bytes, 0, 3)); // "vi" + first byte of "ệ"
    s.onData(1, Uint8List.sublistView(bytes, 1)); // resend from "i"
    expect(text(), 'việt');
  });

  // covers: TERM-03, CON-01
  test('a gap jumps forward instead of stalling', () {
    s.onData(0, b('ab'));
    s.onData(5, b('XY'));
    expect(s.received, 7);
    expect(text(), 'abXY');
  });

  // covers: TERM-03, CON-01
  test('decodes a TERM_OUT frame produced by the Pi', () {
    // python3 -c 'from arstro_remote.protocol import *; print(list(encode_term_out(2, 300, b"ok")))'
    final frame = Uint8List.fromList([3, 0, 0, 0, 11, 2, 0, 0, 0, 0, 0, 0, 1, 44, 111, 107]);
    final f = FrameDecoder().feed(frame).single;
    expect(f.type, kFrameTermOut);
    expect(f.payload[0], 2);
    expect(ByteData.sublistView(f.payload).getUint64(1, Endian.big), 300);
    expect(utf8.decode(f.payload.sublist(9)), 'ok');
  });
}
