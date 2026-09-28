import 'dart:convert';
import 'dart:typed_data';

import 'package:arstro_remote/src/proto/frames.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  // covers: ARC-02
  test('encodes JSON frames with a big-endian length header', () {
    final f = encodeJson({'op': 'ping', 'id': 1});
    expect(f[0], kFrameJson);
    final len = ByteData.sublistView(f).getUint32(1, Endian.big);
    expect(len, f.length - 5);
    expect(jsonDecode(utf8.decode(f.sublist(5))), {'op': 'ping', 'id': 1});
  });

  // covers: ARC-02
  test('encodes TERM frames with the terminal id first', () {
    final f = encodeTerm(3, utf8.encode('ls\n'));
    expect(f[0], kFrameTerm);
    expect(f.sublist(5), [3, ...utf8.encode('ls\n')]);
  });

  // covers: ARC-02
  test('decoder reassembles frames split at every byte', () {
    final data = Uint8List.fromList([
      ...encodeJson({'a': 'ệ'}),
      ...encodeTerm(0, [1, 2, 3]),
      ...encodeJson({'b': 2}),
    ]);
    final d = FrameDecoder();
    final frames = <Frame>[];
    for (var i = 0; i < data.length; i++) {
      frames.addAll(d.feed(Uint8List.fromList([data[i]])));
    }
    expect(frames.length, 3);
    expect(jsonDecode(utf8.decode(frames[0].payload)), {'a': 'ệ'});
    expect(frames[1].type, kFrameTerm);
    expect(frames[1].payload, [0, 1, 2, 3]);
    expect(jsonDecode(utf8.decode(frames[2].payload)), {'b': 2});
  });

  // covers: ARC-02
  test('decoder handles many frames in one chunk', () {
    final chunk = BytesBuilder();
    for (var i = 0; i < 500; i++) {
      chunk.add(encodeJson({'i': i}));
    }
    final frames = FrameDecoder().feed(chunk.takeBytes());
    expect(frames.length, 500);
    expect(jsonDecode(utf8.decode(frames.last.payload)), {'i': 499});
  });

  // covers: ARC-02
  test('decoder skips unknown types and rejects oversized frames', () {
    // A newer server may send frame types this app does not know (e.g. 0x04 VIDEO): skip them.
    final frames = FrameDecoder().feed(Uint8List.fromList([4, 0, 0, 0, 1, 0, ...encodeJson({'a': 1})]));
    expect(frames.length, 1);
    expect(jsonDecode(utf8.decode(frames.single.payload)), {'a': 1});
    expect(() => FrameDecoder().feed(Uint8List.fromList([1, 0x7f, 0, 0, 0])), throwsA(isA<ProtocolException>()));
  });

  // covers: ARC-02
  test('matches the Python encoder byte for byte', () {
    // python3 -c 'from arstro_remote.protocol import *; print(list(encode_json({"op":"ping","id":1})))'
    expect(encodeJson({'op': 'ping', 'id': 1}), [
      1, 0, 0, 0, 20, 123, 34, 111, 112, 34, 58, 34, 112, 105, 110, 103, 34, 44, 34, 105, 100, 34, 58, 49, 125
    ]);
  });
}
