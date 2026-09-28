import 'dart:convert';
import 'dart:typed_data';

/// Arstro Remote wire protocol (see pi/arstro_remote/protocol.py).
///
/// frame = [type u8][length u32 big-endian][payload]
///   0x01 JSON      UTF-8 JSON object
///   0x02 TERM      keyboard bytes to a shell: [term_id u8][raw bytes]
///   0x03 TERM_OUT  shell output: [term_id u8][offset u64 BE][raw bytes]
///                  (offset = stream position of the first byte, used to drop
///                  bytes the app already has after a reconnect replay)
const int kProtoVersion = 1;
const int kFrameJson = 0x01;
const int kFrameTerm = 0x02;
const int kFrameTermOut = 0x03;
const int kMaxFrame = 1 << 20;

/// UUID of the RFCOMM service registered by the Pi daemon.
const String kServiceUuid = 'a57e0001-7c2b-4d1e-9f3a-5e7a1b2c3d4e';

class Frame {
  const Frame(this.type, this.payload);
  final int type;
  final Uint8List payload;
}

class ProtocolException implements Exception {
  ProtocolException(this.message);
  final String message;
  @override
  String toString() => 'ProtocolException: $message';
}

Uint8List encodeFrame(int type, List<int> payload) {
  if (payload.length > kMaxFrame) throw ProtocolException('frame too large');
  final out = Uint8List(5 + payload.length);
  final bd = ByteData.sublistView(out);
  bd.setUint8(0, type);
  bd.setUint32(1, payload.length, Endian.big);
  out.setRange(5, out.length, payload);
  return out;
}

Uint8List encodeJson(Map<String, dynamic> msg) => encodeFrame(kFrameJson, utf8.encode(jsonEncode(msg)));

Uint8List encodeTerm(int termId, List<int> data) {
  final payload = Uint8List(1 + data.length);
  payload[0] = termId & 0xff;
  payload.setRange(1, payload.length, data);
  return encodeFrame(kFrameTerm, payload);
}

/// Incremental decoder: feed() arbitrary chunks, receive whole frames.
class FrameDecoder {
  final BytesBuilder _pending = BytesBuilder(copy: false);
  Uint8List _buf = Uint8List(0);
  int _pos = 0;

  List<Frame> feed(Uint8List chunk) {
    if (_pos < _buf.length) {
      _pending.add(Uint8List.sublistView(_buf, _pos));
    }
    _pending.add(chunk);
    _buf = _pending.takeBytes();
    _pos = 0;
    final frames = <Frame>[];
    while (_buf.length - _pos >= 5) {
      final bd = ByteData.sublistView(_buf, _pos);
      final type = bd.getUint8(0);
      final len = bd.getUint32(1, Endian.big);
      if (type != kFrameJson && type != kFrameTerm && type != kFrameTermOut) {
        throw ProtocolException('unknown frame type $type');
      }
      if (len > kMaxFrame) throw ProtocolException('frame too large: $len');
      if (_buf.length - _pos < 5 + len) break;
      frames.add(Frame(type, Uint8List.fromList(Uint8List.sublistView(_buf, _pos + 5, _pos + 5 + len))));
      _pos += 5 + len;
    }
    return frames;
  }

  void reset() {
    _pending.clear();
    _buf = Uint8List(0);
    _pos = 0;
  }
}
