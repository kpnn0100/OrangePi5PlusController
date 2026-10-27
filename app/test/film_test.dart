// Film work in the app (REC-09..REC-16, GAL-13): the live view's monitoring, focus zoom, timecode
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

/// What the app asked of the native decoder: `create`, `configure {...}`, `frame <version>`.
final native = <String>[];
Completer<void>? configureGate;

void mockDecoder() {
  native.clear();
  configureGate = null;
  messenger.setMockMethodCallHandler(const MethodChannel('arstro/preview'), (call) async {
    native.add('${call.method}${call.arguments != null ? ' ${call.arguments}' : ''}');
    if (call.method == 'configure' && configureGate != null) await configureGate!.future;
    return call.method == 'create' ? 7 : null;
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
  test('a tap in the zoomed crop is the same point of the whole picture', () {
    final z = {'crop': [1280, 720, 1280, 720], 'src_width': 3840, 'src_height': 2160};
    expect(PreviewController.zoomTarget(z, 0.5, 0.5), (0.5, 0.5));
    final (x, y) = PreviewController.zoomTarget(z, 0, 1);
    expect(x, closeTo(1 / 3, 1e-9));
    expect(y, closeTo(2 / 3, 1e-9));
    expect(zoomBadge({'ratio': 3, 'pixels': 1.0}), 'Zoom 3× · 1:1 pixels');
    expect(zoomBadge({'ratio': 2, 'pixels': 0.5}), 'Zoom 2× · 1:2 pixels');
    expect(zoomBadge({'ratio': 8, 'pixels': 2.0}), 'Zoom 8× · 2:1 pixels');
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
  filmTest('live view: a tap zooms in at that point, moves the zoom, and back / Whole picture leave it',
      (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final log = newLog();
    final sock = FakePreviewSocket();
    // a 4:3 picture in the 16:9 frame: black bars left and right
    await openLive(tester, sock, width: 960, height: 720);
    ex(sock.sent.first, {'type': 'hello', 'want': ['levels', 'analysis']});
    ex(native, contains('configure {width: 960, height: 720}'));

    final card = tester.getRect(find.byKey(const Key('live-picture')));
    await tester.tapAt(Offset(card.left - 10, card.center.dy));          // the black bar is not the picture
    await tester.pump();
    ex(sock.sent.where((m) => m['type'] == 'zoom'), isEmpty);

    await tester.tapAt(at(tester, 0.25, 0.5));
    await tester.pump();
    ex(sock.sent.last, {'type': 'zoom', 'on': true, 'x': 0.25, 'y': 0.5});
    log.found(find.byKey(const Key('monitor-overlay')), 'overlays on the whole picture');

    // the Pi zooms: 3x of 3840x2160 around (0.25, 0.5) = a 1280x720 crop at its own pixels
    sock.text({'type': 'zoom', 'on': true, 'ratio': 3, 'x': 0.25, 'y': 0.5, 'width': 1280, 'height': 720,
               'src_width': 3840, 'src_height': 2160, 'crop': [320, 720, 1280, 720], 'pixels': 1.0});
    sock.text({'type': 'config', 'codec': 'h264', 'width': 1280, 'height': 720, 'fps': 30, 'zoom': true});
    await tester.pump(const Duration(milliseconds: 100));
    log.found(find.text('Zoom 3× · 1:1 pixels'), 'zoom badge');
    log.found(find.byKey(const Key('zoom-locator')), 'where the crop lies');
    log.found(find.text('Whole picture'), 'way back');
    ex(find.byKey(const Key('monitor-overlay')), findsNothing, reason: 'overlays hidden while zoomed');
    ex(find.text('3840×2160 · 30 fps'), findsNothing, reason: 'the zoom badge takes the signal badge place');
    ex(native.last, 'configure {width: 1280, height: 720}', reason: 'the decoder follows the new size');

    // a tap in the crop moves the zoom to that point of the whole picture
    await tester.tapAt(at(tester, 0.75, 0.25));
    await tester.pump();
    ex(sock.sent.last['on'], true);
    ex(sock.sent.last['x'], closeTo((320 + 0.75 * 1280) / 3840, 1e-4));
    ex(sock.sent.last['y'], closeTo((720 + 0.25 * 720) / 2160, 1e-4));

    await tester.tap(find.text('Whole picture'));
    await tester.pump();
    ex(sock.sent.last, {'type': 'zoom', 'on': false});

    // Android back leaves the zoom (and does not leave the app)
    sock.text({'type': 'zoom', 'on': true, 'ratio': 3, 'x': 0.5, 'y': 0.5, 'width': 1280, 'height': 720,
               'src_width': 3840, 'src_height': 2160, 'crop': [1280, 720, 1280, 720], 'pixels': 1.0});
    await tester.pump(const Duration(milliseconds: 100));
    final before = sock.sent.length;
    await tester.binding.handlePopRoute();
    await tester.pump();
    ex(sock.sent.sublist(before), [{'type': 'zoom', 'on': false}]);

    // the Pi brings the whole picture back
    sock.text({'type': 'zoom', 'on': false, 'error': null});
    sock.text({'type': 'config', 'codec': 'h264', 'width': 960, 'height': 720, 'fps': 30});
    await tester.pump(const Duration(milliseconds: 100));
    ex(find.text('Whole picture'), findsNothing);
    log.found(find.byKey(const Key('monitor-overlay')), 'overlays back');

    // a refusal is said
    await tester.tapAt(at(tester, 0.5, 0.5));
    sock.text({'type': 'zoom', 'on': false, 'error': '3 viewers are zoomed in already'});
    await tester.pump(const Duration(milliseconds: 100));
    log.found(find.text('Focus zoom: 3 viewers are zoomed in already'), 'refusal snack bar');
    log.check('focus zoom');
    await tester.pump(const Duration(seconds: 5));
    log.done();
  });

  // covers: REC-14
  filmTest('decoder: frames after a new config wait until the decoder has the new size', (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final sock = FakePreviewSocket();
    await openLive(tester, sock);
    native.clear();
    configureGate = Completer<void>();
    sock.text({'type': 'config', 'codec': 'h264', 'width': 1280, 'height': 720, 'fps': 30, 'zoom': true});
    sock.text({'type': 'zoom', 'on': true, 'ratio': 3, 'x': 0.5, 'y': 0.5, 'width': 1280, 'height': 720,
               'src_width': 3840, 'src_height': 2160, 'crop': [1280, 720, 1280, 720], 'pixels': 1.0});
    sock.bin([1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x67]);            // the crop's first keyframe
    sock.bin(analysisMessage());                                           // version 2: never to the decoder
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['configure {width: 1280, height: 720}'], reason: 'the keyframe waits for the decoder');
    configureGate!.complete();
    await tester.pump(const Duration(milliseconds: 50));
    ex(native, ['configure {width: 1280, height: 720}', 'frame 1']);
    sock.bin([1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0x41]);
    await tester.pump(const Duration(milliseconds: 50));
    ex(native.last, 'frame 1');
    await tester.pump(const Duration(seconds: 5));
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
    // REC-14: the zoom ratio
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
