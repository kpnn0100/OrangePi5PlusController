// Film work in the app (REC-09..REC-16, GAL-13): the live view's monitoring, focus loupe, timecode
// and storage over a fake preview socket, the settings that drive them, and the gallery's take
// notes and CSV sheet - all answered from test/fixtures/recorder.json.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:arstro_remote/main.dart';
import 'package:arstro_remote/src/app_scope.dart';
import 'package:arstro_remote/src/media.dart';
import 'package:arstro_remote/src/remote_client.dart';
import 'package:arstro_remote/src/ui/recorder/common.dart';
import 'package:arstro_remote/src/ui/recorder/gallery_view.dart' show avDrift;
import 'package:arstro_remote/src/ui/recorder/loupe.dart';
import 'package:arstro_remote/src/ui/recorder/monitor.dart';
import 'package:arstro_remote/src/ui/recorder/record_button.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'layout_test.dart' show ErrorLog, connectedClient, mockChannels, setSize, sizes;

/// The Pi's end of `/ws/preview`.
class FakePreviewSocket implements PreviewSocket {
  final _in = StreamController<dynamic>();
  final sent = <Map<String, dynamic>>[];

  @override
  Stream<dynamic> get stream => _in.stream;
  @override
  void send(String text) => sent.add(jsonDecode(text) as Map<String, dynamic>);
  @override
  Future<void> close() async => _in.close();

  void text(Map<String, dynamic> m) => _in.add(jsonEncode(m));
  void raw(String s) => _in.add(s);
  void bin(List<int> b) => _in.add(Uint8List.fromList(b));
}

TestDefaultBinaryMessenger get messenger => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

/// What the app asked of the native decoders: `create`, `configure {...}`, `release {decoder:
/// loupe}`, `frame N` (N = the version byte) (the native side routes frames to its decoders by that byte).
final native = <String>[];
Completer<void>? configureGate;

/// The textures the native side hands out: the whole picture's and the loupe's.
const mainTexture = 7, loupeTexture = 8;

void mockDecoder() {
  native.clear();
  configureGate = null;
  messenger.setMockMethodCallHandler(const MethodChannel('arstro/preview'), (call) async {
    native.add('${call.method}${call.arguments != null ? ' ${call.arguments}' : ''}');
    if (call.method == 'configure' && configureGate != null) await configureGate!.future;
    if (call.method != 'create') return null;
    return (call.arguments as Map?)?['decoder'] == 'loupe' ? loupeTexture : mainTexture;
  });
  messenger.setMockMessageHandler('arstro/preview/frames', (data) async {
    native.add('frame ${data!.getUint8(0)}');
    return null;
  });
}

/// The decoder (native side) tells the app it shows pictures.
Future<void> decoderPlays(WidgetTester tester) async {
  await messenger.handlePlatformMessage(
      'arstro/preview', const StandardMethodCodec().encodeMethodCall(const MethodCall('playing')), null);
  await tester.pump(const Duration(milliseconds: 500));
}

/// The loupe decoder shows its first picture.
Future<void> loupePlays(WidgetTester tester) async {
  await messenger.handlePlatformMessage('arstro/preview',
      const StandardMethodCodec().encodeMethodCall(const MethodCall('playing', {'decoder': 'loupe'})), null);
  await tester.pump(const Duration(milliseconds: 100));
}

/// The Pi's answer to a loupe request: its stream is [side] x [side].
Map<String, dynamic> zoomReply(double x, double y, {int side = 480, double size = 0.6}) => {
      'type': 'zoom', 'on': true, 'ratio': 3, 'x': x, 'y': y, 'size': size, 'width': side, 'height': side,
      'fps': 30, 'src_width': 3840, 'src_height': 2160, 'crop': [1600, 760, side, side],
    };

/// Binary preview messages: [version][flags][pts u64][Annex-B]; 1 = the picture, 3 = the loupe.
List<int> frame(int version, {bool key = false}) =>
    [version, key ? 1 : 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, key ? 0x67 : 0x41];

/// The zoom messages the app sent.
List<Map<String, dynamic>> zooms(FakePreviewSocket sock) => [for (final m in sock.sent) if (m['type'] == 'zoom') m];

double r4(double v) => (v * 10000).roundToDouble() / 10000;

/// The loupe's `size` for the picture as shown now: its circle's diameter / the picture's height.
double loupeShare(WidgetTester tester) {
  final r = tester.getRect(find.byKey(const Key('live-picture')));
  return r4(loupeDiameter(r.size) / r.height);
}

Finder textureOf(int id) => find.byWidgetPredicate((w) => w is Texture && w.textureId == id);

/// The app on the Recorder › Live tab, its preview connected to [sock] and showing pictures.
Future<RemoteClient> openLive(WidgetTester tester, FakePreviewSocket sock,
    {int width = 1280, int height = 720, Future<dynamic> Function(String, Map<String, dynamic>)? ops}) async {
  final client = await connectedClient();
  client.media.base = 'http://192.0.2.10:8080/';
  if (ops != null) {
    final fixture = client.requestOverride!;
    client.requestOverride = (op, p) async => await ops(op, p) ?? await fixture(op, p);
  }
  PreviewController.connectOverride = (url, headers) async => sock;
  addTearDown(() => PreviewController.connectOverride = null);
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
  await tester.pump(const Duration(milliseconds: 300));
  await tester.tap(find.text('Recorder').last);
  await tester.pump(const Duration(milliseconds: 400));
  sock.text({'type': 'config', 'codec': 'h264', 'width': width, 'height': height, 'fps': 30});
  sock.text({'type': 'state', 'state': 'live', 'detail': ''});
  await tester.pump(const Duration(milliseconds: 100));
  await decoderPlays(tester);
  return client;
}

/// A point of the live picture, as fractions of its own rectangle.
Offset at(WidgetTester tester, double fx, double fy) {
  final r = tester.getRect(find.byKey(const Key('live-picture')));
  return r.topLeft + Offset(r.width * fx, r.height * fy);
}

/// An analysis message (binary version 2, kind 1) as the Pi sends it.
Uint8List analysisMessage({int gw = 16, int gh = 9, int ww = 8, int wl = 4, int Function(int i)? cell}) {
  final meta = utf8.encode(jsonEncode({'gw': gw, 'gh': gh, 'ww': ww, 'wl': wl, 'ire_max': 110, 'range': 'limited'}));
  final hist = List<int>.generate(128, (i) => i == 50 ? 255 : i == 100 ? 120 : 0);
  final wave = List<int>.generate(ww * wl, (i) => i < ww ? 200 : 0);       // row 0 = 0 %, the bottom row
  final map = List<int>.generate(gw * gh, cell ?? (i) => i < gw ? 200 : 85); // top row 100 %, the rest 42.5 %
  return Uint8List.fromList([2, 1, meta.length >> 8, meta.length & 255, ...meta, ...hist, ...wave, ...map]);
}

/// For `paints..something(...)`: a rectangle drawn in [colour] (other calls are skipped).
bool Function(Symbol, List<dynamic>) rectIn(Color colour) =>
    (m, args) => m == #drawRect && (args[1] as Paint).color.toARGB32() == colour.toARGB32();

bool Function(Symbol, List<dynamic>) call(Symbol method) => (m, _) => m == method;

/// A request the app made: its op and its parameters.
void exOp((String, Map<String, dynamic>) asked, String op, Map<String, dynamic> params) => ex([asked.$1, asked.$2], [op, params]);

ErrorLog? _log;

/// An [ErrorLog] that [ex] restores before a failure (the binding insists on it).
ErrorLog newLog() => _log = ErrorLog();

void ex(dynamic actual, dynamic matcher, {String? reason}) {
  try {
    expect(actual, matcher, reason: reason);
  } on TestFailure {
    _log?.done();
    _log = null;
    rethrow;
  }
}

/// [testWidgets] that gives the framework its error handler back before any failure.
void filmTest(String name, Future<void> Function(WidgetTester tester) body) {
  testWidgets(name, (tester) async {
    try {
      await body(tester);
    } catch (_) {
      _log?.done();
      _log = null;
      rethrow;
    }
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    mockChannels();
    mockDecoder();
  });

  // covers: REC-14
  test('the loupe sits above the point, beside it near the top, always inside the picture', () {
    const pic = Size(800, 450);
    final d = loupeDiameter(pic);
    ex(d, 180, reason: '40 % of the height');
    ex(loupeDiameter(const Size(300, 200)), 120, reason: 'at least 120');
    ex(loupeDiameter(const Size(2000, 1000)), 280, reason: 'at most 280');
    ex(loupeDiameter(const Size(100, 100)), 92, reason: 'never more than fits');
    bool inside(Offset c, double d) =>
        c.dx - d / 2 >= 0 && c.dy - d / 2 >= 0 && c.dx + d / 2 <= pic.width && c.dy + d / 2 <= pic.height;
    // room above: straight above, its rim clear of the finger
    var c = loupeCentre(pic, const Offset(400, 400), d);
    ex(c.dx, 400);
    ex(c.dy, lessThan(400 - d / 2 - 20));
    ex(inside(c, d), true);
    // near the top: beside the point, towards the middle
    c = loupeCentre(pic, const Offset(100, 60), d);
    ex(c.dx, greaterThan(100 + d / 2));
    ex(inside(c, d), true);
    c = loupeCentre(pic, const Offset(700, 60), d);
    ex(c.dx, lessThan(700 - d / 2));
    ex(inside(c, d), true);
    // at the edges it stays inside
    for (final p in const [Offset(0, 0), Offset(800, 450), Offset(0, 450), Offset(800, 0), Offset(400, 225)]) {
      ex(inside(loupeCentre(pic, p, d), d), true, reason: '$p');
    }
  });

  // covers: REC-13, REC-09
  test('analysis layout, false-colour bands and the A/V sync of a take', () {
    final a = Analysis.parse(analysisMessage())!;
    expect([a.gw, a.gh, a.ww, a.wl], [16, 9, 8, 4]);
    expect(a.hist[50], 255);
    expect(a.wave.length, 32);
    expect(a.map.first, 200);
    expect(a.map.last, 85);
    expect(Analysis.parse(Uint8List.fromList([1, 1, 0, 0])), isNull, reason: 'version 1 is video');
    final bands = asList(asMap(asMap(json['recorder'])['monitor'])['bands']);
    final table = MonitorPainter.bandTable(bands);
    expect(table[200], const Color(0xFFFF2A2A).withValues(alpha: 0.8), reason: '100 % = white clipped');
    expect(table[85], const Color(0xFF2ECC40).withValues(alpha: 0.8), reason: '42.5 % = 18 % grey');
    expect(table[120], isNull, reason: '60 %: the picture shows through');
    expect(avDrift({'drift_ms': -45.0, 'rate': '30000/1001'}, const []), (-45.0, true));
    expect(avDrift({'drift_ms': 20.0, 'rate': '30000/1001'}, const []), (20.0, false));
    expect(avDrift({'drift_ms': 20.0}, [{'fps': 60.0}]), (20.0, true));
  });

  // covers: REC-14
  filmTest('live view: a tap opens the loupe there, dragging moves it, a tap moves it, × and Back close it',
      (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final log = newLog();
    final sock = FakePreviewSocket();
    // a 4:3 picture in the 16:9 frame: black bars left and right
    await openLive(tester, sock, width: 960, height: 720);
    ex(sock.sent.first, {'type': 'hello', 'want': ['levels', 'analysis']});
    ex(native, contains('configure {width: 960, height: 720}'));
    final pic = tester.getRect(find.byKey(const Key('live-picture')));
    final share = loupeShare(tester);

    await tester.tapAt(Offset(pic.left - 10, pic.center.dy));          // the black bar is not the picture
    await tester.pump();
    ex(zooms(sock), isEmpty);
    ex(find.byKey(const Key('loupe')), findsNothing);

    // a tap opens the loupe at that point: x, y of the whole picture, size = the circle's share
    await tester.tapAt(at(tester, 0.25, 0.5));
    await tester.pump();
    ex(sock.sent.last, {'type': 'zoom', 'on': true, 'x': 0.25, 'y': 0.5, 'size': share});
    log.found(find.byKey(const Key('loupe')), 'the loupe circle');
    log.found(find.byKey(const Key('loupe-close')), 'its close button');
    log.found(find.text('3×'), 'its magnification');
    log.found(find.byKey(const Key('loupe-dot')), 'the exact point');
    log.found(find.byKey(const Key('loupe-waiting')), '… before its first picture');
    final circle = tester.getRect(find.byKey(const Key('loupe')));
    ex(circle.width, closeTo(share * pic.height, 0.5));
    ex(pic.inflate(0.5).contains(circle.topLeft) && pic.inflate(0.5).contains(circle.bottomRight), true,
        reason: 'inside the picture');
    ex((circle.center - at(tester, 0.25, 0.5)).distance, greaterThan(circle.width / 2 + 20),
        reason: 'the finger never hides it');
    // the whole picture and its monitoring stay as they are around it
    ex(textureOf(mainTexture), findsOneWidget);
    log.found(find.byKey(const Key('monitor-overlay')), 'overlays on the whole picture');
    log.found(find.byKey(const Key('tally')), 'tally');
    log.found(find.text('3840×2160 · 30 fps'), 'the signal badge');

    // the Pi answers: the loupe gets its own decoder and texture
    native.clear();
    sock.text(zoomReply(0.25, 0.5));
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['create {decoder: loupe}', 'configure {decoder: loupe, width: 480, height: 480}']);
    ex(textureOf(loupeTexture), findsOneWidget);
    ex(textureOf(mainTexture), findsOneWidget);
    await loupePlays(tester);
    ex(find.byKey(const Key('loupe-waiting')), findsNothing);

    // dragging from the point: it follows the finger - the Pi hears ~15 times a second, and once more at the end
    var before = zooms(sock).length;
    var g = await tester.startGesture(at(tester, 0.25, 0.5));
    for (var i = 0; i < 12; i++) {
      await g.moveBy(const Offset(4, 0));
      await tester.pump(const Duration(milliseconds: 10));
    }
    ex(zooms(sock).length - before, inInclusiveRange(1, 3), reason: '12 moves in 120 ms, throttled');
    await g.up();
    await tester.pump();
    var x = 0.25 + 48 / pic.width;
    ex(sock.sent.last['on'], true);
    ex(sock.sent.last['x'], closeTo(x, 1e-3));
    ex(sock.sent.last['y'], 0.5);
    ex(sock.sent.last['size'], share);
    ex((tester.getCenter(find.byKey(const Key('loupe-dot'))) - at(tester, x, 0.5)).distance, lessThan(1));

    // dragging the circle itself moves it by the finger's way
    await tester.pump(const Duration(milliseconds: 200));
    before = zooms(sock).length;
    g = await tester.startGesture(tester.getCenter(find.byKey(const Key('loupe'))));
    for (var i = 0; i < 3; i++) {
      await g.moveBy(const Offset(-10, 5));
      await tester.pump(const Duration(milliseconds: 30));
    }
    await g.up();
    await tester.pump();
    ex(zooms(sock).length - before, inInclusiveRange(2, 4));
    ex(sock.sent.last['x'], closeTo(x - 30 / pic.width, 1e-3));
    ex(sock.sent.last['y'], closeTo(0.5 + 15 / pic.height, 1e-3));

    // a tap elsewhere moves it there at once; only a new zoom comes back, the stream goes on
    await tester.pump(const Duration(milliseconds: 200));
    await tester.tapAt(at(tester, 0.12, 0.6));
    await tester.pump();
    ex(sock.sent.last, {'type': 'zoom', 'on': true, 'x': 0.12, 'y': 0.6, 'size': share});
    native.clear();
    sock.text(zoomReply(0.12, 0.6));
    sock.bin(frame(3));
    sock.bin(frame(1));
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['frame 3', 'frame 1'], reason: 'version 3 to the loupe decoder, 1 to the main one; no new configure');

    // × closes it: zoom off, the circle and the loupe decoder go, the picture stays
    native.clear();
    await tester.tap(find.byKey(const Key('loupe-close')));
    await tester.pump();
    ex(sock.sent.last, {'type': 'zoom', 'on': false});
    ex(find.byKey(const Key('loupe')), findsNothing);
    ex(native, ['release {decoder: loupe}'], reason: 'only the loupe decoder is released');
    ex(textureOf(mainTexture), findsOneWidget);
    sock.text({'type': 'zoom', 'on': false, 'error': null});              // the Pi's answer changes nothing
    sock.bin(frame(3));                                                    // nor does a late loupe frame
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['release {decoder: loupe}']);
    log.found(find.byKey(const Key('monitor-overlay')), 'overlays still there');

    // Android back closes it (and does not leave the app)
    await tester.tapAt(at(tester, 0.5, 0.5));
    await tester.pump();
    sock.text(zoomReply(0.5, 0.5));
    await tester.pump(const Duration(milliseconds: 50));
    log.found(find.byKey(const Key('loupe')), 'the loupe again');
    final n = sock.sent.length;
    await tester.binding.handlePopRoute();
    await tester.pump();
    ex(sock.sent.sublist(n), [{'type': 'zoom', 'on': false}]);
    ex(find.byKey(const Key('loupe')), findsNothing);
    log.found(find.byKey(const Key('live-picture')), 'still in the live view');
    sock.text({'type': 'zoom', 'on': false, 'error': null});
    await tester.pump(const Duration(milliseconds: 50));

    // a refusal is said
    await tester.tapAt(at(tester, 0.5, 0.5));
    await tester.pump();
    log.found(find.byKey(const Key('loupe')), 'shown at once');
    sock.text({'type': 'zoom', 'on': false, 'error': '3 loupes are open already'});
    await tester.pump(const Duration(milliseconds: 100));
    log.found(find.text('Focus loupe: 3 loupes are open already'), 'refusal snack bar');
    ex(find.byKey(const Key('loupe')), findsNothing);
    log.check('focus loupe');
    await tester.pump(const Duration(seconds: 5));
    log.done();
  });

  // covers: REC-14
  filmTest('decoders: version 3 waits for the loupe decoder\'s configure, version 1 goes to the main one', (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final sock = FakePreviewSocket();
    await openLive(tester, sock);
    // the main decoder still takes a new size first (the preview quality changed)
    native.clear();
    configureGate = Completer<void>();
    sock.text({'type': 'config', 'codec': 'h264', 'width': 1920, 'height': 1080, 'fps': 30});
    sock.bin(frame(1, key: true));
    sock.bin(analysisMessage());                                           // version 2: never to a decoder
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['configure {width: 1920, height: 1080}'], reason: 'the keyframe waits for the decoder');
    configureGate!.complete();
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['configure {width: 1920, height: 1080}', 'frame 1']);

    // the loupe's first keyframe waits for the loupe decoder; the picture goes on meanwhile
    await tester.tapAt(at(tester, 0.5, 0.5));
    await tester.pump();
    native.clear();
    configureGate = Completer<void>();
    sock.text(zoomReply(0.5, 0.5));
    sock.bin(frame(3, key: true));
    sock.bin(frame(1));
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['create {decoder: loupe}', 'configure {decoder: loupe, width: 480, height: 480}', 'frame 1']);
    configureGate!.complete();
    await tester.pump(const Duration(milliseconds: 50));
    ex(native.last, 'frame 3');
    sock.bin(frame(3));
    await tester.pump(const Duration(milliseconds: 50));
    ex(native.last, 'frame 3');

    // moving: the same stream - no new configure
    native.clear();
    sock.text(zoomReply(0.4, 0.5));
    sock.bin(frame(3));
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['frame 3']);

    // a new size (another layout, another ratio): only the loupe decoder is configured again
    native.clear();
    configureGate = Completer<void>();
    sock.text(zoomReply(0.4, 0.5, side: 640));
    sock.bin(frame(3, key: true));
    sock.bin(frame(1));
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['configure {decoder: loupe, width: 640, height: 640}', 'frame 1']);
    configureGate!.complete();
    await tester.pump(const Duration(milliseconds: 50));
    ex(native.last, 'frame 3');

    // the connection drops: the loupe decoder goes, the main one stays for the reconnect
    native.clear();
    await sock.close();
    await tester.pump(const Duration(milliseconds: 100));
    ex(native, contains('release {decoder: loupe}'));
    ex(native.where((c) => c == 'release' || c == 'reset'), isEmpty);
    ex(find.byKey(const Key('loupe')), findsNothing);
    await tester.pump(const Duration(seconds: 5));
  });

  // covers: REC-14
  filmTest('full screen: the loupe follows the new layout; Back closes it before leaving full screen', (tester) async {
    await setSize(tester, sizes['phone landscape']!);
    final log = newLog();
    final sock = FakePreviewSocket();
    await openLive(tester, sock);
    await tester.tapAt(at(tester, 0.5, 0.25));
    await tester.pump();
    ex(sock.sent.last, {'type': 'zoom', 'on': true, 'x': 0.5, 'y': 0.25, 'size': loupeShare(tester)});
    sock.text(zoomReply(0.5, 0.25));
    await tester.pump(const Duration(milliseconds: 50));

    await tester.ensureVisible(find.byTooltip('Full screen'));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.tap(find.byTooltip('Full screen'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    log.found(find.byKey(const Key('loupe')), 'the loupe in full screen');
    final full = tester.getRect(find.byKey(const Key('live-picture')));
    ex(full.height, 390);
    ex(sock.sent.last, {'type': 'zoom', 'on': true, 'x': 0.5, 'y': 0.25, 'size': loupeShare(tester)},
        reason: 'the circle is another share of this picture');
    final circle = tester.getRect(find.byKey(const Key('loupe')));
    ex(circle.width, closeTo(156, 0.5), reason: '40 % of 390');

    // dragging works here too
    final g = await tester.startGesture(at(tester, 0.5, 0.25));
    await g.moveBy(const Offset(0, 20));
    await g.moveBy(const Offset(0, 20));
    await g.up();
    await tester.pump();
    ex(sock.sent.last['y'], closeTo(0.25 + 40 / full.height, 1e-3));

    // turned to portrait: the size is sent again
    await setSize(tester, sizes['phone portrait']!);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    ex(sock.sent.last['size'], loupeShare(tester));
    ex(sock.sent.last['size'], isNot(0.4));

    // Back closes the loupe first, then leaves full screen
    var n = sock.sent.length;
    await tester.binding.handlePopRoute();
    await tester.pump();
    ex(sock.sent.sublist(n), [{'type': 'zoom', 'on': false}]);
    ex(find.byKey(const Key('loupe')), findsNothing);
    ex(find.byTooltip('Leave full screen'), findsOneWidget);
    n = sock.sent.length;
    await tester.binding.handlePopRoute();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    ex(find.byTooltip('Leave full screen'), findsNothing);
    ex(sock.sent.sublist(n), isEmpty);
    log.check('full-screen loupe');
    await tester.pump(const Duration(seconds: 5));
    log.done();
  });

  for (final size in ['phone portrait', 'small phone', 'tablet']) {
    // covers: REC-10, REC-13
    filmTest('live view: tally, guides, zebras, false colour, waveform and meters on $size', (tester) async {
      await setSize(tester, sizes[size]!);
      final log = newLog();
      final sock = FakePreviewSocket();
      final client = await openLive(tester, sock);
      final st = asMap(client.topic('recorder.settings').value);
      client.topic('recorder.settings').value = {...st, 'monitor': {...asMap(st['monitor']), 'false_color': true}};
      // silence comes as -Infinity from the Pi's JSON
      sock.raw('{"type": "levels", "peak": [-0.4, -Infinity], "rms": [-6.0, -Infinity]}');
      sock.bin(analysisMessage());
      await tester.pump(const Duration(milliseconds: 100));

      log.found(find.byKey(const Key('tally')), 'recording tally');
      final meters = tester.widget<AudioMeters>(find.byType(AudioMeters));
      ex(meters.levels.peak, [-0.4, -200.0]);
      ex(meters.levels.rms, [-6.0, -200.0]);
      final meterBox = find.descendant(of: find.byKey(const Key('audio-meters')), matching: find.byType(CustomPaint));
      ex(tester.renderObject(meterBox.first), paints..something(rectIn(kRec)), reason: 'a clipped peak is red');

      final overlay = find.byKey(const Key('monitor-overlay'));
      final painter = tester.widget<CustomPaint>(overlay).painter! as MonitorPainter;
      ex(painter.analysis, isNotNull);
      ex(painter.monitor['guides'], '2.39');
      ex(
          tester.renderObject(overlay),
          paints
            ..something(rectIn(const Color(0xFFFF2A2A).withValues(alpha: 0.8)))   // false colour: white clipped
            ..something(rectIn(const Color(0xFF2ECC40).withValues(alpha: 0.8)))   // false colour: 18 % grey
            ..something(call(#clipPath))                                           // zebras over the top row
            ..something(call(#drawLine))
            ..something(rectIn(Colors.black.withValues(alpha: 0.45))));           // 2.39 guide shading
      final scope = tester.widget<CustomPaint>(find.byKey(const Key('scope'))).painter! as ScopePainter;
      ex(scope.kind, 'waveform');
      log.check('monitoring');

      // meters and the scope follow the shared settings
      client.topic('recorder.settings').value = {
        ...st,
        'monitor': {...asMap(st['monitor']), 'meters': false, 'scope': 'off'},
      };
      await tester.pump(const Duration(milliseconds: 100));
      ex(find.byType(AudioMeters), findsNothing);
      ex(find.byKey(const Key('scope')), findsNothing);
      log.check('monitoring off');
      await tester.pump(const Duration(seconds: 5));
      log.done();
    });
  }

  // covers: REC-11, REC-12, REC-15
  filmTest('live view: timecode, markers, next take, storage, disk test and record anyway', (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final log = newLog();
    final sock = FakePreviewSocket();
    final asked = <(String, Map<String, dynamic>)>[];
    var starts = 0;
    final client = await openLive(tester, sock, ops: (op, p) async {
      asked.add((op, p));
      if (op == 'recorder.start' && starts++ == 0) {
        throw RemoteError('disk too slow: this disk writes 120 MB/s; this mode needs 497 MB/s (+15 % margin) - frames '
            'would be lost: use H.265, a faster disk (NVMe) or a lower resolution (record anyway: force)');
      }
      return null;
    });
    log.found(find.text('TC 12:00:00;04'), 'timecode of the take');
    log.found(find.text('2 markers'), 'marker count');
    await tester.ensureVisible(find.text('Set marker'));
    await tester.tap(find.text('Set marker'));
    await tester.pump(const Duration(milliseconds: 100));
    ex(asked.last.$1, 'recorder.mark');
    log.found(find.text('Marker at 12:00:31;10'), 'marker set');
    log.found(find.text('Time left'), 'storage figures');
    log.found(find.text('15 min'), 'time left at the recording rate');
    final test = find.ancestor(of: find.text('Test disk'), matching: find.byWidgetPredicate((w) => w is TextButton));
    ex(tester.widget<TextButton>(test).onPressed, isNull, reason: 'no disk test while recording');
    log.check('recording');

    // stopped: the next take's name, the clock warning, the disk test, a disk too slow
    final rec = asMap(client.topic('recorder').value);
    client.topic('recorder').value = {
      ...rec,
      'recording': {'active': false},
      'clock_synced': false,
      'storage': {...asMap(rec['storage']), 'ok': false, 'speed': 120000000,
                  'warning': 'this disk writes 120 MB/s; this mode needs 497 MB/s (+15 % margin) - frames would be lost'},
    };
    for (var i = 0; i < 25; i++) {
      await tester.pump(const Duration(milliseconds: 200));             // the marker's snack bar goes
    }
    log.found(find.text('Next take S12A_SH3_T05_20260101_120124'), 'next take');
    log.found(find.textContaining('Board clock not synced (NTP)'), 'clock warning');
    log.found(find.textContaining('This disk writes 120 MB/s'), 'disk warning');
    ex(find.byKey(const Key('tally')), findsNothing);
    await tester.ensureVisible(test);
    await tester.tap(test);
    await tester.pump(const Duration(milliseconds: 100));
    ex(asked.last.$1, 'recorder.disk_test');
    await tester.pump(const Duration(milliseconds: 400));
    log.found(find.textContaining('This disk writes 1.'), 'disk test result');

    await tester.ensureVisible(find.byType(RecordButton));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.byType(RecordButton));
    await tester.pump(const Duration(milliseconds: 300));
    log.found(find.text('Record anyway?'), 'record anyway question');
    await tester.tap(find.widgetWithText(FilledButton, 'Record anyway'));
    await tester.pump(const Duration(milliseconds: 300));
    final startCalls = asked.where((a) => a.$1 == 'recorder.start').map((a) => a.$2).toList();
    ex(startCalls, [{}, {'force': true}]);
    log.check('record anyway');
    await tester.pump(const Duration(seconds: 5));
    log.done();
  });

  // covers: REC-09, REC-12, REC-13, REC-14, REC-15, REC-16
  filmTest('settings: monitoring, audio, slate, record button & tally and storage guard send their patches',
      (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final log = newLog();
    final client = await connectedClient();
    final patches = <Map<String, dynamic>>[];
    final other = <(String, Map<String, dynamic>)>[];
    final fixture = client.requestOverride!;
    client.requestOverride = (op, p) async {
      if (op == 'recorder.settings.set') {
        patches.add(asMap(p['settings']));
        return {};
      }
      if (op != 'gallery.get' && op != 'io.gpio.header') other.add((op, p));
      return fixture(op, p);
    };
    final st = asMap(client.topic('recorder.settings').value);
    client.topic('recorder.settings').value = {...st, 'mode': 'h265'};     // shows the container
    final prefs = await SharedPreferences.getInstance();
    await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.text('Recorder').last);
    await tester.pump(const Duration(milliseconds: 400));
    await tester.ensureVisible(find.text('RAW +HQ +FFV1'));
    await tester.tap(find.text('RAW +HQ +FFV1'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));

    Future<void> show(Finder f) async {
      await tester.ensureVisible(f);
      await tester.pump(const Duration(milliseconds: 200));
    }

    Future<Map<String, dynamic>> tapFor(Finder f) async {
      await show(f);
      final n = patches.length;
      await tester.tap(f);
      await tester.pump(const Duration(milliseconds: 300));
      ex(patches.length, n + 1, reason: 'one patch for $f');
      return patches.last;
    }

    Finder switchOf(String key) => find.descendant(of: find.byKey(Key(key)), matching: find.byType(Switch));

    Future<Map<String, dynamic>> pick(String key, String item) async {
      await show(find.byKey(Key(key)));
      await tester.tap(find.byKey(Key(key)));
      await tester.pump(const Duration(milliseconds: 400));
      return tapFor(find.text(item).last);
    }

    Future<Map<String, dynamic>> type(String key, String text) async {
      final field = find.descendant(of: find.byKey(Key(key)), matching: find.byType(TextField));
      await show(field);
      final n = patches.length;
      await tester.enterText(field, text);
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump(const Duration(milliseconds: 300));
      ex(patches.length, n + 1, reason: 'one patch for $key');
      return patches.last;
    }

    // REC-09: container and audio input
    log.found(find.text('MOV: PCM audio, opens in every editor'), 'container explained');
    ex(await tapFor(find.text('MKV')), {'h265': {'container': 'mkv'}});
    // REC-13: monitoring
    ex(await pick('mon-guides', '4:3'), {'monitor': {'guides': '4:3'}});
    ex(await tapFor(switchOf('mon-safe')), {'monitor': {'safe': false}});
    ex(await tapFor(switchOf('mon-zebra')), {'monitor': {'zebra': false}});
    ex(await tapFor(switchOf('mon-false-colour')), {'monitor': {'false_color': true}});
    ex(await tapFor(find.text('Histogram')), {'monitor': {'scope': 'histogram'}});
    ex(await tapFor(switchOf('mon-meters')), {'monitor': {'meters': false}});
    // REC-14: the loupe's magnification
    ex(await tapFor(find.text('4×')), {'monitor': {'zoom': 4}});
    ex(await pick('mon-lut', 'HLG → Rec.709'), {'monitor': {'lut': 'hlg'}});
    await show(find.byTooltip('Delete this LUT'));
    await tester.tap(find.byTooltip('Delete this LUT'));
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
    await tester.pump(const Duration(milliseconds: 400));
    exOp(other.last, 'recorder.lut.delete', {'lut': 'cube:FX3 Rec709'});
    ex(await pick('audio-source', 'HDMI input (from the camera)'), {'audio': {'source': 'hdmi'}});
    // REC-15: the slate
    ex(await type('slate-scene', '14'), {'slate': {'scene': '14'}});
    ex(await type('slate-take', '7'), {'slate': {'take': 7}});
    ex(await tapFor(switchOf('slate-auto')), {'slate': {'auto': false}});
    log.found(find.textContaining('Next take: S12A_SH3_T05_20260101_120124'), 'next take name');
    // REC-16: record button and tally
    log.found(find.text('Ready · 3 presses'), 'button state');
    log.found(find.text('line 16 of /dev/gpiochip1 is busy (used by sysfs)'), 'tally problem');
    ex(await type('gpio-tally', ''), {'gpio': {'tally': ''}});
    await show(find.byTooltip('Choose a header pin').first);
    await tester.tap(find.byTooltip('Choose a header pin').first);
    await tester.pump(const Duration(milliseconds: 400));
    log.found(find.textContaining('Pin 7 · GPIO1_B7 · gpiochip1:15 · in use'), 'header pins');
    ex(await tapFor(find.text('Pin 11 · GPIO1_C0 · gpiochip1:16')), {'gpio': {'button': 'gpiochip1:16'}});
    // REC-12
    ex(await tapFor(find.descendant(of: find.widgetWithText(Row, 'Storage guard'), matching: find.byType(Switch))),
        {'storage_guard': false});
    log.check('settings');
    await tester.pump(const Duration(seconds: 5));
    log.done();
  });

  // covers: GAL-13
  filmTest('gallery: slate, timecode, good take and markers; annotate and the CSV sheet', (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final log = newLog();
    final client = await connectedClient();
    final asked = <(String, Map<String, dynamic>)>[];
    final saved = <Map>[];
    final fixture = client.requestOverride!;
    client.requestOverride = (op, p) async {
      if (op.startsWith('gallery.') && op != 'gallery.get' && op != 'gallery.list') asked.add((op, p));
      return fixture(op, p);
    };
    messenger.setMockMethodCallHandler(const MethodChannel('arstro/download'), (call) async {
      saved.add({'method': call.method, ...call.arguments as Map});
      return 'Downloads/Arstro/${(call.arguments as Map)['name']}';
    });
    final prefs = await SharedPreferences.getInstance();
    await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.text('Recorder').last);
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('Gallery').first);
    await tester.pump(const Duration(milliseconds: 500));
    final grid = find.descendant(of: find.byType(CustomScrollView), matching: find.byType(Scrollable)).first;
    final card = find.text('Scene 12A · Shot 3 · Take 4');
    for (var i = 0; i < 10 && card.hitTestable().evaluate().isEmpty; i++) {
      await tester.drag(grid, const Offset(0, -150));
      await tester.pump(const Duration(milliseconds: 200));
    }
    log.found(card, 'slate title on the card');
    log.found(find.text('11:30:00;12'), 'start timecode on the thumbnail');
    log.found(find.byWidgetPredicate((w) => w is Icon && w.icon == Icons.star_rounded && w.semanticLabel == 'Good take'),
        'good-take star');
    log.found(find.text('A/V -45 ms'), 'A/V sync warning');
    log.found(find.text('2 markers'), 'markers');
    log.check('gallery cards');

    await tester.tap(card);
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    final sheet = find.byType(DraggableScrollableSheet);
    Finder inSheet(Finder f) => find.descendant(of: sheet, matching: f);
    log.found(inSheet(find.text('Scene 12A · Shot 3 · Take 4')), 'slate in the sheet');
    log.found(inSheet(find.text('Start TC 11:30:00;12 · 29.97 fps')), 'start timecode');
    log.found(inSheet(find.text('A/V sync: audio and video differ by 45 ms - more than a frame')), 'A/V sync');
    log.found(inSheet(find.text('clap')), 'a marker');
    log.found(inSheet(find.textContaining('not synced (NTP)')), 'clock note');
    const id = 'S12A_SH3_T04_20260101_113000';

    Future<void> tapIn(Finder f) async {
      await tester.ensureVisible(f);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.tap(f);
      await tester.pump(const Duration(milliseconds: 300));
    }

    await tapIn(find.byKey(const Key('take-good')));
    exOp(asked.last, 'gallery.annotate', {'take': id, 'good': false});
    await tester.enterText(find.byKey(const Key('take-notes')), 'Use this one');
    await tapIn(find.byKey(const Key('take-notes-save')));
    exOp(asked.last, 'gallery.annotate', {'take': id, 'notes': 'Use this one'});
    await tester.enterText(find.byKey(const Key('take-scene')), '12B');
    await tester.enterText(find.byKey(const Key('take-no')), '6');
    await tapIn(find.byKey(const Key('take-slate-save')));
    exOp(asked.last, 'gallery.annotate', {'take': id, 'scene': '12B', 'shot': '3', 'take_no': 6});
    log.check('take sheet');
    Navigator.of(tester.element(sheet)).pop();
    tester.state<ScaffoldMessengerState>(find.byType(ScaffoldMessenger).first).clearSnackBars();
    await tester.pump(const Duration(seconds: 1));

    // GAL-13: the day's takes as the editor's sheet, saved like the downloads
    await tester.ensureVisible(find.text('Select'));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.text('Select'));
    await tester.pump(const Duration(milliseconds: 400));
    for (var i = 0; i < 10 && find.text('Thu 1 Jan').hitTestable().evaluate().isEmpty; i++) {
      await tester.drag(grid, const Offset(0, -100));
      await tester.pump(const Duration(milliseconds: 200));
    }
    await tester.tap(find.bySemanticsLabel('Select Thu 1 Jan'));
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.widgetWithText(TextButton, 'CSV'));
    await tester.pump(const Duration(milliseconds: 400));
    exOp(asked.last, 'gallery.csv', {'day': '2026-01-01'});
    ex(saved.single['method'], 'save');
    ex(saved.single['name'], 'arstro-takes-2026-01-01.csv');
    ex(saved.single['mime'], 'text/csv');
    ex((saved.single['text'] as String).split('\r\n').first, startsWith('File Name,Clip Directory,Scene,Shot,Take'));
    log.found(find.textContaining('Saved arstro-takes-2026-01-01.csv · 2 rows'), 'saved snack bar');

    // one take: by its id
    tester.state<ScaffoldMessengerState>(find.byType(ScaffoldMessenger).first).clearSnackBars();
    await tester.ensureVisible(find.text('Select'));
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('Select'));
    await tester.pump(const Duration(milliseconds: 400));
    for (var i = 0; i < 10 && card.hitTestable().evaluate().isEmpty; i++) {
      await tester.drag(grid, const Offset(0, -100));
      await tester.pump(const Duration(milliseconds: 200));
    }
    await tester.tap(card);
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.widgetWithText(TextButton, 'CSV'));
    await tester.pump(const Duration(milliseconds: 400));
    exOp(asked.last, 'gallery.csv', {'takes': [id]});
    log.check('csv');
    await tester.pump(const Duration(seconds: 5));
    log.done();
  });
}

/// The fixture, for the plain (non-widget) tests.
final Map<String, dynamic> json =
    jsonDecode(File('test/fixtures/recorder.json').readAsStringSync()) as Map<String, dynamic>;
