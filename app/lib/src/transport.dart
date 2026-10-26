import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'bt/bluetooth.dart';
import 'proto/frames.dart';

/// Byte pipe to the Pi. Bluetooth RFCOMM in the product; a TCP pipe exists for
/// debug builds so the UI can be exercised through `adb reverse` + an SSH tunnel
/// to the daemon's control socket when no Bluetooth link is available.
abstract class Transport {
  Stream<Uint8List> get data;

  /// Emits a reason whenever an established link goes down.
  Stream<String> get closed;

  Future<void> connect();
  Future<void> write(Uint8List bytes);
  Future<void> disconnect();
}

class BluetoothTransport implements Transport {
  BluetoothTransport(this.address);
  final String address;
  final Bluetooth bt = Bluetooth.instance;

  @override
  Stream<Uint8List> get data => bt.data;

  @override
  Stream<String> get closed => bt.events
      .where((e) => e['event'] == 'connection' && e['state'] == 'disconnected')
      .map((e) => e['reason'] as String? ?? 'link lost');

  @override
  Future<void> connect() => bt.connect(address, kServiceUuid);

  @override
  Future<void> write(Uint8List bytes) => bt.write(bytes);

  @override
  Future<void> disconnect() => bt.disconnect();
}

/// Debug only: "tcp:host:port" addresses.
class TcpTransport implements Transport {
  TcpTransport(this.host, this.port);
  final String host;
  final int port;
  Socket? _socket;
  final _data = StreamController<Uint8List>.broadcast();
  final _closed = StreamController<String>.broadcast();

  @override
  Stream<Uint8List> get data => _data.stream;

  @override
  Stream<String> get closed => _closed.stream;

  @override
  Future<void> connect() async {
    await disconnect();
    final s = await Socket.connect(host, port, timeout: const Duration(seconds: 5));
    s.setOption(SocketOption.tcpNoDelay, true);
    _socket = s;
    s.listen((d) => _data.add(d), onDone: () => _lost(s, 'connection closed by the Pi'),
        onError: (Object e) => _lost(s, '$e'), cancelOnError: true);
  }

  void _lost(Socket s, String reason) {
    if (_socket != s) return;
    _socket = null;
    _closed.add(reason);
  }

  @override
  Future<void> write(Uint8List bytes) async {
    final s = _socket;
    if (s == null) throw const SocketException('not connected');
    s.add(bytes);
  }

  @override
  Future<void> disconnect() async {
    final s = _socket;
    _socket = null;
    await s?.close();
    s?.destroy();
  }
}

/// Another A/B slot of the same Pi (TERM-06): its `/ws` carries exactly the frames the
/// Bluetooth link does (binary messages form one byte stream), so [RemoteClient] works over it
/// unchanged. The slot's password goes in the Authorization header.
class WebSocketTransport implements Transport {
  WebSocketTransport(this.url, this.token);
  final Uri url;
  final String? token;
  WebSocket? _ws;
  final _data = StreamController<Uint8List>.broadcast();
  final _closed = StreamController<String>.broadcast();

  @override
  Stream<Uint8List> get data => _data.stream;

  @override
  Stream<String> get closed => _closed.stream;

  @override
  Future<void> connect() async {
    await disconnect();
    final ws = await WebSocket.connect(url.toString(),
            headers: {if (token != null) 'Authorization': 'Bearer $token'})
        .timeout(const Duration(seconds: 6));
    ws.pingInterval = const Duration(seconds: 10);
    _ws = ws;
    ws.listen((m) {
      if (m is Uint8List) {
        _data.add(m);
      } else if (m is List<int>) {
        _data.add(Uint8List.fromList(m));
      }
    }, onDone: () => _lost(ws, 'connection closed by the Pi'), onError: (Object e) => _lost(ws, '$e'),
        cancelOnError: true);
  }

  void _lost(WebSocket ws, String reason) {
    if (_ws != ws) return;
    _ws = null;
    _closed.add(reason);
  }

  @override
  Future<void> write(Uint8List bytes) async {
    final ws = _ws;
    if (ws == null) throw const SocketException('not connected');
    ws.add(bytes);
  }

  @override
  Future<void> disconnect() async {
    final ws = _ws;
    _ws = null;
    await ws?.close();
  }
}

Transport transportFor(String address) {
  if (address.startsWith('tcp:')) {
    final parts = address.split(':');
    return TcpTransport(parts[1], int.parse(parts[2]));
  }
  return BluetoothTransport(address);
}
