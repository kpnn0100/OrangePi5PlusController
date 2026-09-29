import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'remote_client.dart';

/// The Wi-Fi side of the app (CON-02): control stays on Bluetooth, but the preview,
/// thumbnails, playback and downloads come from the Pi's web server. The address and
/// the access password arrive over Bluetooth (`web.info`); the first address the phone
/// can actually reach wins.
class MediaLink extends ChangeNotifier {
  MediaLink(this.client) {
    client.addListener(_onClient);
    client.topic('web').addListener(_soon);
    client.topic('wifi').addListener(_soon);
  }

  final RemoteClient client;

  /// e.g. `http://<pi-address>:8080/`; null while the phone can't reach the Pi.
  String? base;
  String? _token;
  String? error;
  bool checking = false;
  List<String> tried = const [];
  bool _wasConnected = false;
  Timer? _debounce;

  bool get ready => base != null;
  Map<String, String> get headers => _token == null ? const {} : {'Authorization': 'Bearer $_token'};
  Uri url(String path) => Uri.parse(base! + (path.startsWith('/') ? path.substring(1) : path));

  void _onClient() {
    final up = client.isConnected;
    if (up && !_wasConnected) refresh();
    _wasConnected = up;
  }

  void _soon() {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 800), refresh);
  }

  Future<void> refresh() async {
    if (!client.isConnected || checking) return;
    checking = true;
    notifyListeners();
    try {
      final info = Map<String, dynamic>.from(await client.request('web.info') as Map);
      if (info['enabled'] == false) {
        base = null;
        error = "The Pi's web server is switched off.";
        return;
      }
      _token = info['auth'] == 'open' ? null : info['token'] as String?;
      final port = info['port'] ?? 8080;
      final urls = <String>[
        // developer transport through adb reverse: the web port is forwarded too
        if ((client.device?.address ?? '').startsWith('tcp:')) 'http://127.0.0.1:$port/',
        ...List<String>.from(info['urls'] as List? ?? const []),
      ];
      tried = urls;
      String? found, lastError;
      for (final u in urls) {
        final problem = await _probe(u);
        if (problem == null) {
          found = u;
          break;
        }
        lastError = problem;
      }
      base = found;
      error = found == null ? _explain(urls, lastError) : null;
    } catch (e) {
      base = null;
      error = 'Could not ask the Pi for its media address ($e)';
    } finally {
      checking = false;
      notifyListeners();
    }
  }

  Future<String?> _probe(String u) async {
    final http = HttpClient()..connectionTimeout = const Duration(seconds: 2);
    try {
      final req = await http.getUrl(Uri.parse('${u}api/ping')).timeout(const Duration(seconds: 3));
      headers.forEach(req.headers.set);
      final res = await req.close().timeout(const Duration(seconds: 3));
      final body = jsonDecode(await res.transform(utf8.decoder).join()) as Map;
      return body['authorized'] == true ? null : 'the Pi refused the password';
    } catch (e) {
      return e.toString();
    } finally {
      http.close(force: true);
    }
  }

  String _explain(List<String> urls, String? problem) {
    if (urls.isEmpty) return 'The Pi has no network address. Connect it to Wi-Fi in the Wi-Fi tab.';
    if (problem == 'the Pi refused the password') return 'The Pi refused the media password. Try again in a moment.';
    final wifi = client.topic('wifi').value;
    final ssid = wifi is Map ? wifi['ssid'] as String? : null;
    return "The phone can't reach the Pi over Wi-Fi (${urls.join(', ')}). "
        '${ssid != null ? 'Join "$ssid" on this phone.' : 'Put the phone on the same network as the Pi.'}';
  }

  /// Saves a recording to the phone's Downloads/Arstro folder (Android DownloadManager).
  Future<void> download(String path, String name) async {
    await const MethodChannel('arstro/download')
        .invokeMethod('enqueue', {'url': url(path).toString(), 'name': name, 'headers': headers});
  }

  @override
  void dispose() {
    client.removeListener(_onClient);
    client.topic('web').removeListener(_soon);
    client.topic('wifi').removeListener(_soon);
    _debounce?.cancel();
    super.dispose();
  }
}

/// Live H.264 preview (REC-02): the WebSocket `/ws/preview` feeds access units to the
/// native MediaCodec decoder, which draws into a Flutter texture.
class PreviewController extends ChangeNotifier {
  /// [path]: `ws/preview` (HDMI input) or `ws/screen` (the Pi's desktop, SCR-01).
  PreviewController(this.link, {this.path = 'ws/preview'}) {
    _method.setMethodCallHandler(_onNative);
  }

  final MediaLink link;
  final String path;
  static const _method = MethodChannel('arstro/preview');
  static const _frames = BasicMessageChannel<ByteData>('arstro/preview/frames', BinaryCodec());

  int? textureId;
  String state = 'stopped';
  String detail = '';
  bool playing = false;
  double aspect = 16 / 9;
  Map<String, dynamic>? config;
  WebSocket? _ws;
  bool _wanted = false;
  bool _disposed = false;
  Timer? _retry;
  int _epoch = 0;

  bool get running => _wanted;

  Future<void> start() async {
    if (_wanted || _disposed) return;
    _wanted = true;
    try {
      textureId ??= await _method.invokeMethod<int>('create');
    } on MissingPluginException {
      return;                               // widget tests
    } catch (e) {
      _set('error', 'Video decoder: $e');
      return;
    }
    _set('starting', '');
    _connect();
  }

  Future<void> _connect() async {
    final epoch = ++_epoch;
    if (!link.ready) {
      _set('no-link', link.error ?? '');
      return;
    }
    final url = '${link.base!.replaceFirst(RegExp('^http'), 'ws')}$path';
    try {
      final ws = await WebSocket.connect(url, headers: link.headers).timeout(const Duration(seconds: 6));
      if (!_wanted || epoch != _epoch) {
        ws.close();
        return;
      }
      _ws = ws;
      ws.listen((m) {
        if (m is String) {
          _onText(jsonDecode(m) as Map<String, dynamic>);
        } else if (m is Uint8List) {
          _frames.send(ByteData.sublistView(m));
        } else if (m is List<int>) {
          _frames.send(ByteData.sublistView(Uint8List.fromList(m)));
        }
      }, onDone: () => _closed(epoch), onError: (_) => _closed(epoch), cancelOnError: true);
    } catch (e) {
      _set('reconnecting', 'Preview connection failed ($e)');
      _scheduleRetry();
    }
  }

  void _onText(Map<String, dynamic> m) {
    if (m['type'] == 'config') {
      config = m;
      final w = (m['width'] as num?)?.toInt() ?? 0, h = (m['height'] as num?)?.toInt() ?? 0;
      if (w > 0 && h > 0) aspect = w / h;
      _method.invokeMethod('configure', {'width': w, 'height': h}).catchError((_) => null);
      notifyListeners();
    } else if (m['type'] == 'state') {
      final s = m['state'] as String? ?? 'stopped';
      if (s != 'live') playing = false;
      _set(s, m['detail'] as String? ?? '');
    }
  }

  void _closed(int epoch) {
    if (epoch != _epoch) return;
    _ws = null;
    playing = false;
    if (_wanted) {
      _set('reconnecting', '');
      _scheduleRetry();
    } else {
      notifyListeners();
    }
  }

  void _scheduleRetry() {
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 2), () {
      if (_wanted) _connect();
    });
  }

  Future<dynamic> _onNative(MethodCall call) async {
    switch (call.method) {
      case 'playing':
        playing = true;
        notifyListeners();
      case 'size':
        final a = Map<String, dynamic>.from(call.arguments as Map);
        final w = (a['width'] as num).toDouble(), h = (a['height'] as num).toDouble();
        if (w > 0 && h > 0) aspect = w / h;
        notifyListeners();
      case 'error':
        debugPrint('preview decoder: ${call.arguments}');
    }
    return null;
  }

  Future<void> stop() async {
    _wanted = false;
    _epoch++;
    _retry?.cancel();
    final ws = _ws;
    _ws = null;
    playing = false;
    await ws?.close();
    try {
      await _method.invokeMethod('reset');
    } catch (_) {}
    _set('stopped', '');
  }

  void _set(String s, String d) {
    state = s;
    detail = d;
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _wanted = false;
    _epoch++;
    _retry?.cancel();
    _ws?.close();
    _method.invokeMethod('release').catchError((_) => null);
    _method.setMethodCallHandler(null);
    super.dispose();
  }
}
