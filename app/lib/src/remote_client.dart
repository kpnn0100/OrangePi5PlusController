import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'bt/bluetooth.dart';
import 'media.dart';
import 'proto/frames.dart';
import 'terminal_hub.dart';
import 'transport.dart';

enum LinkState { idle, connecting, connected, reconnecting, failed }

class RemoteError implements Exception {
  RemoteError(this.message);
  final String message;
  @override
  String toString() => message;
}

const String kAppVersion = '2.0.3';

/// Owns the link to one Pi: handshake, request/response matching, server
/// events, stats, terminal routing, input coalescing and auto-reconnect.
class RemoteClient extends ChangeNotifier {
  RemoteClient(this.prefs) {
    _clientId = prefs.getString('client_id') ?? _newClientId();
    _eventSub = bt.events.listen(_onBtEvent);
    final addr = prefs.getString('last_address');
    if (addr != null) lastDevice = BtDevice(address: addr, name: prefs.getString('last_name'), bonded: true);
  }

  final SharedPreferences prefs;
  final Bluetooth bt = Bluetooth.instance;
  late final TerminalHub terminals = TerminalHub(this);

  /// Wi-Fi media link (preview, thumbnails, playback) to this Pi's web server.
  late final MediaLink media = MediaLink(this);

  LinkState state = LinkState.idle;
  BtDevice? device;
  BtDevice? lastDevice;
  String? error;
  Map<String, dynamic>? server;
  int? latencyMs;
  int reconnectAttempt = 0;
  DateTime? nextRetryAt;

  final ValueNotifier<Map<String, dynamic>?> stats = ValueNotifier(null);

  /// Live server state (ARC-03): recorder, recorder.settings, gallery, jobs, wifi,
  /// terminals, pairing, controllers, web. Filled from hello, then by `state` events.
  final Map<String, ValueNotifier<dynamic>> _topics = {};
  ValueNotifier<dynamic> topic(String name) => _topics.putIfAbsent(name, () => ValueNotifier<dynamic>(null));
  final List<double> cpuHistory = [];
  final List<double> tempHistory = [];
  static const int historyLength = 90;

  late final String _clientId;
  StreamSubscription? _eventSub;
  StreamSubscription? _dataSub;
  StreamSubscription? _closedSub;
  Transport? _transport;
  final FrameDecoder _decoder = FrameDecoder();
  final Map<int, Completer<dynamic>> _pending = {};
  final Map<int, void Function(dynamic)> _syncHooks = {};
  int _nextId = 1;
  bool _userDisconnect = false;
  Timer? _pingTimer;
  Timer? _retryTimer;
  int _missedPings = 0;
  int _connectToken = 0;

  bool get isConnected => state == LinkState.connected;
  String get hostname => (server?['hostname'] as String?) ?? device?.label ?? 'Orange Pi';
  bool get inputAvailable => (server?['input']?['available'] as bool?) ?? false;

  String _newClientId() {
    final r = Random.secure();
    final id = List.generate(16, (_) => r.nextInt(256).toRadixString(16).padLeft(2, '0')).join();
    prefs.setString('client_id', id);
    return id;
  }

  void _setState(LinkState s, {String? err}) {
    state = s;
    error = err;
    notifyListeners();
  }

  // ------------------------------------------------------------ connection
  Future<void> connect(BtDevice d) async {
    _userDisconnect = false;
    _retryTimer?.cancel();
    reconnectAttempt = 0;
    device = d;
    await _dataSub?.cancel();
    await _closedSub?.cancel();
    final t = _transport = transportFor(d.address);
    _dataSub = t.data.listen(_onData);
    _closedSub = t.closed.listen(_onClosed);
    await _attempt(first: true);
  }

  Future<void> _attempt({bool first = false}) async {
    final token = ++_connectToken;
    final d = device!;
    _setState(first ? LinkState.connecting : LinkState.reconnecting, err: first ? null : error);
    try {
      await _transport!.connect();
      if (token != _connectToken) return;
      _decoder.reset();
      await _handshake();
      if (token != _connectToken) return;
      reconnectAttempt = 0;
      nextRetryAt = null;
      lastDevice = d;
      prefs.setString('last_address', d.address);
      prefs.setString('last_name', d.name ?? hostname);
      _setState(LinkState.connected);
      _startPing();
    } catch (e) {
      if (token != _connectToken) return;
      final msg = _describe(e);
      debugPrint('connect failed: $msg');
      try {
        await _transport?.disconnect();
      } catch (_) {}
      if (first) {
        _setState(LinkState.failed, err: msg);
      } else {
        error = msg;
        _scheduleReconnect();
      }
    }
  }

  String _describe(Object e) {
    if (e is PlatformException) {
      final m = e.message ?? e.code;
      if (m.contains('read failed') || m.contains('timeout') || m.contains('Service discovery failed')) {
        return 'Could not reach the Arstro service. Is the Pi on and in range, and is '
            'arstro-remote running? ($m)';
      }
      return m;
    }
    return e.toString();
  }

  Future<void> _handshake() async {
    final hello = await request('hello',
        {'app': 'arstro-android', 'version': kAppVersion, 'client_id': _clientId}, const Duration(seconds: 10));
    server = Map<String, dynamic>.from(hello as Map);
    if ((server!['proto'] as int? ?? 0) != kProtoVersion) {
      throw RemoteError('Protocol mismatch: Pi speaks v${server!['proto']}, app speaks v$kProtoVersion');
    }
    final st = server!['state'];
    if (st is Map) {
      for (final e in st.entries) {
        topic(e.key as String).value = e.value;
      }
    }
    await request('stats.subscribe', {'interval_ms': 2000});
    await terminals.onConnected(List<Map<String, dynamic>>.from(
        (server!['terminals'] as List? ?? []).map((e) => Map<String, dynamic>.from(e as Map))));
  }

  /// User-initiated disconnect: no auto-reconnect.
  Future<void> disconnect() async {
    _userDisconnect = true;
    _connectToken++;
    _retryTimer?.cancel();
    _pingTimer?.cancel();
    nextRetryAt = null;
    try {
      await _transport?.disconnect();
    } catch (_) {}
    _linkDown('disconnected');
    _setState(LinkState.idle);
  }

  void cancelConnect() {
    disconnect();
  }

  void _linkDown(String reason) {
    _pingTimer?.cancel();
    for (final c in _pending.values) {
      if (!c.isCompleted) c.completeError(RemoteError('Connection lost ($reason)'));
    }
    _pending.clear();
    _syncHooks.clear();
    terminals.onDisconnected();
    latencyMs = null;
  }

  void _scheduleReconnect() {
    if (_userDisconnect || device == null) return;
    reconnectAttempt++;
    final secs = min(15, 1 << min(reconnectAttempt - 1, 4));
    nextRetryAt = DateTime.now().add(Duration(seconds: secs));
    _setState(LinkState.reconnecting, err: error);
    _retryTimer?.cancel();
    _retryTimer = Timer(Duration(seconds: secs), () => _attempt());
  }

  void retryNow() {
    if (device == null) return;
    _retryTimer?.cancel();
    _userDisconnect = false;
    _attempt(first: state == LinkState.failed || state == LinkState.idle);
  }

  void _onClosed(String reason) {
    final wasUp = state == LinkState.connected;
    if (!wasUp && state != LinkState.connecting && state != LinkState.reconnecting) return;
    if (!wasUp) {
      // Dropped during the handshake: fail its requests now instead of timing out.
      for (final c in _pending.values) {
        if (!c.isCompleted) c.completeError(RemoteError('Connection closed during handshake'));
      }
      _pending.clear();
      return;
    }
    debugPrint('link lost: $reason');
    _linkDown(reason);
    if (_userDisconnect) {
      _setState(LinkState.idle);
    } else {
      error = 'Link lost: $reason';
      reconnectAttempt = 0;
      _scheduleReconnect();
    }
  }

  void _onBtEvent(Map<dynamic, dynamic> ev) {
    if (ev['event'] == 'adapter' && ev['state'] == 'on' && state == LinkState.reconnecting) {
      retryNow();
    }
  }

  // ---------------------------------------------------------------- ping
  void _startPing() {
    _pingTimer?.cancel();
    _missedPings = 0;
    _pingTimer = Timer.periodic(const Duration(seconds: 4), (_) async {
      if (!isConnected) return;
      final t0 = DateTime.now();
      try {
        await request('ping', {}, const Duration(seconds: 4));
        _missedPings = 0;
        latencyMs = DateTime.now().difference(t0).inMilliseconds;
        notifyListeners();
      } catch (_) {
        if (++_missedPings >= 3 && isConnected) {
          debugPrint('3 pings missed, dropping link');
          _linkDown('no response');
          try {
            await _transport?.disconnect();
          } catch (_) {}
          error = 'The Pi stopped responding';
          reconnectAttempt = 0;
          _scheduleReconnect();
        }
      }
    });
  }

  // ------------------------------------------------------------ transport
  void _send(Uint8List frame) {
    final t = _transport;
    if (t == null) return;
    t.write(frame).catchError((Object e) {
      debugPrint('write failed: $e');
    });
  }

  Future<dynamic> request(String op, [Map<String, dynamic>? params, Duration? timeout]) {
    return _request(op, params ?? const {}, timeout ?? const Duration(seconds: 15));
  }

  /// Like [request], but [onReply] runs synchronously when the reply is decoded,
  /// before any frame that follows it (term.attach relies on this ordering).
  Future<dynamic> requestSync(String op, Map<String, dynamic> params, void Function(dynamic data) onReply) {
    return _request(op, params, const Duration(seconds: 15), onReply);
  }

  /// Widget tests answer requests here instead of a Pi.
  @visibleForTesting
  Future<dynamic> Function(String op, Map<String, dynamic> params)? requestOverride;

  Future<dynamic> _request(String op, Map<String, dynamic> params, Duration timeout,
      [void Function(dynamic)? onReply]) {
    final fake = requestOverride;
    if (fake != null) return fake(op, params);
    final id = _nextId++;
    final c = Completer<dynamic>();
    _pending[id] = c;
    if (onReply != null) _syncHooks[id] = onReply;
    _send(encodeJson({...params, 'op': op, 'id': id}));
    return c.future.timeout(timeout, onTimeout: () {
      _pending.remove(id);
      _syncHooks.remove(id);
      throw RemoteError('$op timed out');
    });
  }

  void notify(String op, [Map<String, dynamic>? params]) {
    if (!isConnected) return;
    _send(encodeJson({...?params, 'op': op}));
  }

  void sendTerm(int termId, List<int> data) {
    if (!isConnected) return;
    _send(encodeTerm(termId, data));
  }

  void _onData(Uint8List chunk) {
    List<Frame> frames;
    try {
      frames = _decoder.feed(chunk);
    } on ProtocolException catch (e) {
      debugPrint('protocol error: $e');
      _decoder.reset();
      return;
    }
    for (final f in frames) {
      if (f.type == kFrameTermOut) {
        if (f.payload.length >= 9) {
          final offset = ByteData.sublistView(f.payload).getUint64(1, Endian.big);
          terminals.onData(f.payload[0], offset, Uint8List.sublistView(f.payload, 9));
        }
        continue;
      }
      if (f.type != kFrameJson) continue;
      final Map<String, dynamic> msg;
      try {
        msg = jsonDecode(utf8.decode(f.payload)) as Map<String, dynamic>;
      } catch (e) {
        debugPrint('bad json frame: $e');
        continue;
      }
      if (msg.containsKey('ok') && msg['id'] != null) {
        final c = _pending.remove(msg['id']);
        final hook = _syncHooks.remove(msg['id']);
        if (c == null || c.isCompleted) continue;
        if (msg['ok'] == true) {
          hook?.call(msg['data']);
          c.complete(msg['data']);
        } else {
          c.completeError(RemoteError(msg['error']?.toString() ?? 'request failed'));
        }
      } else if (msg['ev'] == 'stats') {
        _onStats(Map<String, dynamic>.from(msg['data'] as Map));
      } else if (msg['ev'] == 'state') {
        topic(msg['topic'] as String).value = msg['data'];
      } else if (msg['ev'] == 'term.exit') {
        terminals.onExit(msg['term'] as int, msg['code'] as int?);
      }
    }
  }

  void _onStats(Map<String, dynamic> s) {
    final cpu = (s['cpu']?['percent'] as num?)?.toDouble();
    final temp = (s['cpu_temp'] as num?)?.toDouble();
    if (cpu != null) _push(cpuHistory, cpu);
    if (temp != null) _push(tempHistory, temp);
    stats.value = s;
  }

  void _push(List<double> list, double v) {
    list.add(v);
    if (list.length > historyLength) list.removeAt(0);
  }

  // --------------------------------------------------------------- input
  double _mdx = 0, _mdy = 0, _sdx = 0, _sdy = 0;
  Timer? _moveTimer;

  /// Pointer motion is coalesced (~60 Hz) so a slow link never builds a backlog.
  void mouseMove(double dx, double dy) {
    _mdx += dx;
    _mdy += dy;
    _moveTimer ??= Timer(const Duration(milliseconds: 16), _flushMotion);
  }

  void scroll(double dx, double dy) {
    _sdx += dx;
    _sdy += dy;
    _moveTimer ??= Timer(const Duration(milliseconds: 16), _flushMotion);
  }

  void _flushMotion() {
    _moveTimer = null;
    if (_mdx.abs() >= 0.01 || _mdy.abs() >= 0.01) {
      notify('in.move', {'dx': _round(_mdx), 'dy': _round(_mdy)});
      _mdx = _mdy = 0;
    }
    if (_sdx.abs() >= 0.01 || _sdy.abs() >= 0.01) {
      notify('in.scroll', {'dx': _round(_sdx), 'dy': _round(_sdy)});
      _sdx = _sdy = 0;
    }
  }

  double _round(double v) => (v * 100).roundToDouble() / 100;

  /// button: left/middle/right, action: down/up/click/double
  void mouseButton(String button, String action) {
    _flushMotion();
    notify('in.btn', {'b': button, 'a': action});
  }

  void key(String key, {List<String> mods = const [], String action = 'press'}) {
    notify('in.key', {'k': key, if (mods.isNotEmpty) 'mods': mods, if (action != 'press') 'a': action});
  }

  void typeText(String text) {
    if (text.isEmpty) return;
    notify('in.text', {'s': text});
  }

  @override
  void dispose() {
    _eventSub?.cancel();
    _dataSub?.cancel();
    _closedSub?.cancel();
    _pingTimer?.cancel();
    _retryTimer?.cancel();
    _moveTimer?.cancel();
    terminals.dispose();
    super.dispose();
  }
}
