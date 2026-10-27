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
/// [levels] and the exposure [analysis]; [loupeTo] / [loupeOff] open, move and close the focus
/// loupe - a magnifying glass over the picture. The whole picture keeps coming (version 1, the
/// main decoder); the loupe is its own small square stream (version 3) for a second native
/// decoder and texture ([loupeTexture]), so the main picture is never disturbed by it.
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

  /// The focus loupe (REC-14) as this viewer wants it: the point (0..1 of the whole picture)
  /// and the circle's diameter as a share of the picture's displayed height - or null (closed).
  /// It follows the finger at once; the server's replies only bring the stream to it. Moving
  /// notifies only [loupePoint] (the circle), opening and closing the controller too.
  final ValueNotifier<({double x, double y, double size})?> loupePoint = ValueNotifier(null);
  ({double x, double y, double size})? get loupeAt => loupePoint.value;
  bool get loupeOpen => loupePoint.value != null;

  /// The server's latest `zoom` reply for the open loupe (`ratio`, the stream's `width` x
  /// `height`, `crop`, ...), or null before the first one.
  Map<String, dynamic>? loupe;

  /// The loupe decoder's texture (made on the first reply, released when the loupe closes).
  int? loupeTexture;

  /// The loupe decoder shows pictures (before that the circle shows it is coming).
  bool loupePlaying = false;

  /// A refused loupe (e.g. 3 loupes open already) - the live view shows it.
  void Function(String error)? onLoupeError;

  /// At most this often a moving loupe tells the server where it is (~15 a second).
  static const loupeInterval = Duration(milliseconds: 66);

  PreviewSocket? _ws;
  bool _wanted = false;
  bool _disposed = false;
  Timer? _retry;
  int _epoch = 0;
  // frames wait while the decoder takes a new size (a `config`), so the first keyframe of the
  // new stream reaches a decoder that already knows it
  Future<void>? _configuring;
  final List<Uint8List> _held = [];
  // the same for the loupe decoder (made and sized on a `zoom` reply)
  Future<void>? _loupeConfiguring;
  final List<Uint8List> _loupeHeld = [];
  (int, int)? _loupeSize;                 // what the loupe decoder was configured for
  Future<int?>? _loupeCreate;             // the loupe decoder being made
  int _loupeGen = 0;                      // a closed loupe's late replies are ignored
  int _offsSent = 0;                      // `zoom off` sent, not answered yet
  Timer? _loupeTimer;                     // the throttle window of a moving loupe
  bool _loupeDirty = false;               // a newer point waits for the window's end

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

  /// Binary messages: the first byte is the version - 1 = the picture (main decoder),
  /// 2 = analysis, 3 = the loupe (loupe decoder; the native side routes by the same byte).
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
    } else if (m[0] == 3) {
      if (!loupeOpen) return;                // closed: the server stops it on our `off`
      if (_loupeConfiguring != null || loupeTexture == null) {
        _loupeHeld.add(m);
        if (_loupeHeld.length > 60) _loupeHeld.removeAt(0);
        return;
      }
      _frames.send(ByteData.sublistView(m));
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
        _onZoom(m);
    }
  }

  /// The server's answer about the loupe: on (its stream's size and where it is) or off.
  void _onZoom(Map<String, dynamic> m) {
    if (m['on'] == true) {
      if (!loupeOpen) return;              // closed meanwhile: our `off` ends it
      final shown = loupe?['ratio'] != m['ratio'];     // the circle's label
      loupe = m;
      final w = (m['width'] as num?)?.toInt() ?? 0, h = (m['height'] as num?)?.toInt() ?? 0;
      if (w > 0 && h > 0 && _loupeSize != (w, h)) _configureLoupe(w, h);
      if (shown) notifyListeners();
      return;
    }
    final err = m['error'];
    if (err != null && '$err'.isNotEmpty) {
      if (!loupeOpen) return;              // refused, but closed meanwhile anyway
      _closeLoupe();
      onLoupeError?.call('$err');
    } else if (_offsSent > 0) {
      _offsSent--;                         // the answer to our own `off`
      return;
    } else {
      _closeLoupe();                       // the server ended it
    }
    notifyListeners();
  }

  /// Makes (the first time) and sizes the loupe decoder; its frames wait until it is ready.
  void _configureLoupe(int w, int h) {
    final gen = _loupeGen;
    _loupeSize = (w, h);
    late final Future<void> f;
    f = () async {
      // one decoder per loupe: a second reply while it is being made waits for the same one
      final id = await (_loupeCreate ??= _method.invokeMethod<int>('create', {'decoder': 'loupe'}));
      if (gen != _loupeGen) return;         // closed meanwhile: the release went after it
      if (loupeTexture == null) {
        loupeTexture = id;
        loupePlaying = false;
        if (!_disposed) notifyListeners();
      }
      await _method.invokeMethod('configure', {'decoder': 'loupe', 'width': w, 'height': h});
    }()
        .then((_) {}, onError: (Object e) {
      debugPrint('loupe decoder: $e');
      if (gen == _loupeGen && _loupeConfiguring == f) _loupeSize = null;   // the next reply tries again
    }).whenComplete(() {
      if (_loupeConfiguring != f || gen != _loupeGen) return;
      _loupeConfiguring = null;
      final held = List.of(_loupeHeld);
      _loupeHeld.clear();
      if (loupeTexture == null) return;
      for (final b in held) {
        _frames.send(ByteData.sublistView(b));
      }
    });
    _loupeConfiguring = f;
  }

  void _send(Map<String, dynamic> m) => _ws?.send(jsonEncode(m));

  /// Opens the loupe at (x, y) - 0..1 of the whole picture - or moves it there; [size] = the
  /// circle's diameter as a share of the picture's displayed height (REC-14). The circle follows
  /// at once; the server hears of it at most every [loupeInterval] (the latest point at the end
  /// of each window) - or right away with [now] (a tap, the end of a drag, a new layout).
  void loupeTo(double x, double y, double size, {bool now = false}) {
    if (_ws == null) return;
    final opens = !loupeOpen;
    loupePoint.value = (x: x.clamp(0.0, 1.0), y: y.clamp(0.0, 1.0), size: size.clamp(0.02, 1.0));
    if (opens) notifyListeners();
    if (now) {
      _loupeTimer?.cancel();
      _loupeTimer = null;
    }
    _throttledSend();
  }

  void _throttledSend() {
    if (_loupeTimer != null) {
      _loupeDirty = true;                  // sent when this window ends
      return;
    }
    _loupeDirty = false;
    final at = loupeAt;
    if (at == null) return;
    double r(double v) => (v * 10000).roundToDouble() / 10000;
    _send({'type': 'zoom', 'on': true, 'x': r(at.x), 'y': r(at.y), 'size': r(at.size)});
    _loupeTimer = Timer(loupeInterval, () {
      _loupeTimer = null;
      if (_loupeDirty) _throttledSend();
    });
  }

  /// Closes the loupe (the × on it, Back).
  void loupeOff() {
    if (!loupeOpen) return;
    _closeLoupe();
    if (_ws != null) {
      _offsSent++;
      _send({'type': 'zoom', 'on': false});
    }
    notifyListeners();
  }

  /// Forgets the loupe here and releases its decoder (the main one is untouched).
  void _closeLoupe() {
    final had = loupeTexture != null || _loupeCreate != null;
    loupePoint.value = null;
    loupe = null;
    loupeTexture = null;
    loupePlaying = false;
    _loupeGen++;
    _loupeConfiguring = null;
    _loupeHeld.clear();
    _loupeSize = null;
    _loupeCreate = null;
    _loupeTimer?.cancel();
    _loupeTimer = null;
    _loupeDirty = false;
    if (had) _method.invokeMethod('release', {'decoder': 'loupe'}).catchError((_) => null);
  }

  void _resetFilm() {
    _closeLoupe();
    _offsSent = 0;
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
    final args = call.arguments;
    if (args is Map && args['decoder'] == 'loupe') {
      // the loupe decoder: its first picture (it is square - the main aspect is not its own)
      if (call.method == 'playing' && loupeTexture != null && !loupePlaying) {
        loupePlaying = true;
        notifyListeners();
      } else if (call.method == 'error') {
        debugPrint('loupe decoder: $args');
      }
      return null;
    }
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
    _loupeTimer?.cancel();
    _loupeGen++;
    _ws?.close();
    if (_owner == this) {
      _owner = null;
      _method.invokeMethod('release').catchError((_) => null);
      _method.setMethodCallHandler(null);
    }
    levels.dispose();
    analysis.dispose();
    loupePoint.dispose();
    super.dispose();
  }
}
