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

  /// The last `web.info` (its `slots` lead [SlotLinks] to the other A/B slots, TERM-06).
  Map<String, dynamic>? info;

  /// The web password of this link's slot (null in open mode).
  String? get token => _token;
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
      final info = this.info = Map<String, dynamic>.from(await client.request('web.info') as Map);
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

  /// Saves text the Pi sent (the editor's CSV sheet, GAL-13) as [name] into Downloads/Arstro,
  /// next to the downloaded recordings; returns where it went. Works without Wi-Fi.
  static Future<String?> saveText(String name, String text, {String mime = 'text/csv'}) {
    return const MethodChannel('arstro/download')
        .invokeMethod<String>('save', {'name': name, 'text': text, 'mime': mime});
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

/// What the preview needs of its WebSocket (widget tests hand in a fake one).
abstract class PreviewSocket {
  Stream<dynamic> get stream;
  void send(String text);
  Future<void> close();
}

class _IoPreviewSocket implements PreviewSocket {
  _IoPreviewSocket(this.ws);
  final WebSocket ws;
  @override
  Stream<dynamic> get stream => ws;
  @override
  void send(String text) => ws.add(text);
  @override
  Future<void> close() => ws.close();
}

/// Audio levels of the recorded input (REC-10): dBFS per channel, ten times a second.
class AudioLevels {
  const AudioLevels(this.peak, this.rms);
  final List<double> peak;
  final List<double> rms;

  static List<double> _db(dynamic v) =>
      [for (final x in (v is List ? v : const [])) x is num ? x.toDouble().clamp(-200.0, 20.0) : -200.0];

  factory AudioLevels.from(Map<String, dynamic> m) => AudioLevels(_db(m['peak']), _db(m['rms']));
}

/// Exposure data of the full captured frame (REC-13): binary message version 2, kind 1 -
/// `[2][1][json length u16 BE][JSON {gw, gh, ww, wl, ire_max, range}][hist 128][wave wl x ww][map gw x gh]`.
class Analysis {
  Analysis(this.meta, this.hist, this.wave, this.map);
  final Map<String, dynamic> meta;
  final Uint8List hist;      // share per 1 % IRE (0..127 %), sqrt-scaled 0..255
  final Uint8List wave;      // row i = IRE [i, i+1) x ire_max / wl, row 0 = 0 %; column = 1/ww of the width
  final Uint8List map;       // gw x gh cells from the top: IRE x 2 (200 = 100 %)

  int get gw => (meta['gw'] as num?)?.toInt() ?? 0;
  int get gh => (meta['gh'] as num?)?.toInt() ?? 0;
  int get ww => (meta['ww'] as num?)?.toInt() ?? 0;
  int get wl => (meta['wl'] as num?)?.toInt() ?? 0;
  double get ireMax => (meta['ire_max'] as num?)?.toDouble() ?? 110;

  static Analysis? parse(Uint8List b) {
    if (b.length < 4 || b[0] != 2 || b[1] != 1) return null;
    final n = (b[2] << 8) | b[3];
    if (b.length < 4 + n) return null;
    try {
      final meta = Map<String, dynamic>.from(jsonDecode(utf8.decode(b.sublist(4, 4 + n))) as Map);
      final gw = (meta['gw'] as num).toInt(), gh = (meta['gh'] as num).toInt();
      final ww = (meta['ww'] as num).toInt(), wl = (meta['wl'] as num).toInt();
      var o = 4 + n;
      if (b.length < o + 128 + ww * wl + gw * gh) return null;
      final hist = Uint8List.sublistView(b, o, o + 128);
      o += 128;
      final wave = Uint8List.sublistView(b, o, o + ww * wl);
      o += ww * wl;
      return Analysis(meta, hist, wave, Uint8List.sublistView(b, o, o + gw * gh));
    } catch (_) {
      return null;
    }
  }
}

/// Live H.264 preview (REC-02): the WebSocket `/ws/preview` feeds access units to the
/// native MediaCodec decoder, which draws into a Flutter texture.
///
/// Film work (REC-10, REC-13, REC-14): with [want] the viewer says hello and gets the audio
/// [levels] and the exposure [analysis]; [zoomAt] / [zoomOff] ask for the focus zoom, which
/// arrives as a new `config` (the decoder follows the size) and the crop's frames.
class PreviewController extends ChangeNotifier {
  /// [path]: `ws/preview` (HDMI input) or `ws/screen` (the Pi's desktop, SCR-01).
  PreviewController(this.link, {this.path = 'ws/preview', this.want = const []});

  final MediaLink link;
  final String path;

  /// What this viewer asks for in its hello: `levels`, `analysis` (empty: nothing is sent).
  final List<String> want;
  static const _method = MethodChannel('arstro/preview');
  static const _frames = BasicMessageChannel<ByteData>('arstro/preview/frames', BinaryCodec());

  /// The native side has one decoder (and one event handler) for the live view and the remote
  /// screen: the controller that started last owns it; another one re-creates it on start.
  static PreviewController? _owner;

  /// Widget tests connect to a fake preview socket here.
  @visibleForTesting
  static Future<PreviewSocket> Function(String url, Map<String, String> headers)? connectOverride;

  int? textureId;
  String state = 'stopped';
  String detail = '';
  bool playing = false;
  double aspect = 16 / 9;
  Map<String, dynamic>? config;

  /// Audio levels (REC-10), ten times a second; null until the first ones.
  final ValueNotifier<AudioLevels?> levels = ValueNotifier(null);

  /// Exposure data (REC-13), four times a second while zebras, false colour or a scope are on.
  final ValueNotifier<Analysis?> analysis = ValueNotifier(null);

  /// The focus zoom this viewer is shown (the server's `zoom` reply), or null (REC-14).
  Map<String, dynamic>? zoom;
  bool get zoomed => zoom != null;

  /// A refused zoom (e.g. 3 viewers zoomed already) - the live view shows it.
  void Function(String error)? onZoomError;

  PreviewSocket? _ws;
  bool _wanted = false;
  bool _disposed = false;
  Timer? _retry;
  int _epoch = 0;
  // frames wait while the decoder takes a new size (a `config`), so the first keyframe of the
  // new stream reaches a decoder that already knows it
  Future<void>? _configuring;
  final List<Uint8List> _held = [];

  bool get running => _wanted;

  Future<void> start() async {
    if (_wanted || _disposed) return;
    _wanted = true;
    try {
      if (textureId == null || _owner != this) {
        _owner = this;
        _method.setMethodCallHandler(_onNative);
        textureId = await _method.invokeMethod<int>('create');
      }
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
      final connect = connectOverride;
      final ws = connect != null
          ? await connect(url, link.headers)
          : _IoPreviewSocket(await WebSocket.connect(url, headers: link.headers).timeout(const Duration(seconds: 6)));
      if (!_wanted || epoch != _epoch) {
        ws.close();
        return;
      }
      _ws = ws;
      _resetFilm();
      ws.stream.listen((m) {
        if (epoch != _epoch) return;
        if (m is String) {
          final msg = _decode(m);
          if (msg != null) _onText(msg);
        } else if (m is Uint8List) {
          _onBinary(m);
        } else if (m is List<int>) {
          _onBinary(Uint8List.fromList(m));
        }
      }, onDone: () => _closed(epoch), onError: (_) => _closed(epoch), cancelOnError: true);
      if (want.isNotEmpty) ws.send(jsonEncode({'type': 'hello', 'want': want}));
    } catch (e) {
      _set('reconnecting', 'Preview connection failed ($e)');
      _scheduleRetry();
    }
  }

  /// Python's json writes silence as -Infinity, which is no JSON: read it as very quiet.
  static Map<String, dynamic>? _decode(String text) {
    try {
      return jsonDecode(text) as Map<String, dynamic>;
    } on FormatException {
      try {
        return jsonDecode(text.replaceAll('-Infinity', '-200').replaceAll('Infinity', '200').replaceAll('NaN', 'null'))
            as Map<String, dynamic>;
      } catch (_) {
        return null;
      }
    } catch (_) {
      return null;
    }
  }

  /// Binary messages: the first byte is the version - 1 = video (to the decoder), 2 = analysis.
  void _onBinary(Uint8List m) {
    if (m.isEmpty) return;
    if (m[0] == 1) {
      if (_configuring != null) {
        _held.add(m);
        if (_held.length > 60) _held.removeAt(0);
        return;
      }
      _frames.send(ByteData.sublistView(m));
    } else if (m[0] == 2) {
      final a = Analysis.parse(m);
      if (a != null) analysis.value = a;
    }
  }

  void _onText(Map<String, dynamic> m) {
    switch (m['type']) {
      case 'config':
        config = m;
        final w = (m['width'] as num?)?.toInt() ?? 0, h = (m['height'] as num?)?.toInt() ?? 0;
        if (w > 0 && h > 0) aspect = w / h;
        late final Future<void> f;
        f = _method.invokeMethod('configure', {'width': w, 'height': h}).then((_) {}, onError: (_) {}).whenComplete(() {
          if (_configuring != f) return;
          _configuring = null;
          final held = List.of(_held);
          _held.clear();
          for (final b in held) {
            _frames.send(ByteData.sublistView(b));
          }
        });
        _configuring = f;
        notifyListeners();
      case 'state':
        final s = m['state'] as String? ?? 'stopped';
        if (s != 'live') playing = false;
        _set(s, m['detail'] as String? ?? '');
      case 'levels':
        levels.value = AudioLevels.from(m);
      case 'zoom':
        if (m['on'] == true) {
          zoom = m;
        } else {
          zoom = null;
          final err = m['error'];
          if (err != null && '$err'.isNotEmpty) onZoomError?.call('$err');
        }
        notifyListeners();
    }
  }

  void _send(Map<String, dynamic> m) => _ws?.send(jsonEncode(m));

  /// Zoom in around (x, y), 0..1 of the whole picture (REC-14).
  void zoomTo(double x, double y) {
    double r(double v) => (v.clamp(0.0, 1.0) * 10000).roundToDouble() / 10000;
    _send({'type': 'zoom', 'on': true, 'x': r(x), 'y': r(y)});
  }

  /// A tap at (fx, fy), 0..1 of the picture as shown: the whole picture, or while zoomed the
  /// crop - then the point is moved within the whole picture.
  void zoomAt(double fx, double fy) {
    final z = zoom;
    if (z == null) return zoomTo(fx, fy);
    final (x, y) = zoomTarget(z, fx, fy);
    zoomTo(x, y);
  }

  /// Back to the whole picture.
  void zoomOff() {
    _send({'type': 'zoom', 'on': false});
  }

  /// A point (fx, fy) of the zoomed picture -> the same point in the whole picture (0..1),
  /// from the reply's `crop` [left, top, w, h] and `src_width` / `src_height`.
  static (double, double) zoomTarget(Map<String, dynamic> z, double fx, double fy) {
    final crop = [for (final v in (z['crop'] as List? ?? const [])) (v as num).toDouble()];
    final sw = (z['src_width'] as num?)?.toDouble() ?? 0, sh = (z['src_height'] as num?)?.toDouble() ?? 0;
    if (crop.length < 4 || sw <= 0 || sh <= 0) return (fx, fy);
    return (((crop[0] + fx * crop[2]) / sw).clamp(0.0, 1.0), ((crop[1] + fy * crop[3]) / sh).clamp(0.0, 1.0));
  }

  void _resetFilm() {
    zoom = null;
    levels.value = null;
    analysis.value = null;
    _configuring = null;
    _held.clear();
  }

  void _closed(int epoch) {
    if (epoch != _epoch) return;
    _ws = null;
    playing = false;
    _resetFilm();
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
    _resetFilm();
    await ws?.close();
    try {
      if (_owner == this) await _method.invokeMethod('reset');
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
    if (_owner == this) {
      _owner = null;
      _method.invokeMethod('release').catchError((_) => null);
      _method.setMethodCallHandler(null);
    }
    levels.dispose();
    analysis.dispose();
    super.dispose();
  }
}
