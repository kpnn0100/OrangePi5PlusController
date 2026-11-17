// Film work in the app (REC-09, REC-11, REC-12, REC-15, REC-16, GAL-13) - all over Bluetooth
// (CON-06): the live view's timecode, markers and storage, the settings that drive the recorder
// (also its monitoring and loupe, which the web UI shows), and the gallery's take notes and CSV
// sheet - all answered from test/fixtures/recorder.json.
import 'dart:convert';
import 'dart:io';

import 'package:arstro_remote/main.dart';
import 'package:arstro_remote/src/app_scope.dart';
import 'package:arstro_remote/src/remote_client.dart';
import 'package:arstro_remote/src/ui/recorder/common.dart';
import 'package:arstro_remote/src/ui/recorder/gallery_view.dart' show avDrift;
import 'package:arstro_remote/src/ui/recorder/record_button.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'layout_test.dart' show ErrorLog, connectedClient, mockChannels, setSize, sizes;

TestDefaultBinaryMessenger get messenger => TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

/// The app on the Recorder › Live tab.
Future<RemoteClient> openLive(WidgetTester tester, {Future<dynamic> Function(String, Map<String, dynamic>)? ops}) async {
  final client = await connectedClient();
  if (ops != null) {
    final fixture = client.requestOverride!;
    client.requestOverride = (op, p) async => await ops(op, p) ?? await fixture(op, p);
  }
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(ArstroApp(client: client, settings: Settings(prefs)));
  await tester.pump(const Duration(milliseconds: 300));
  await tester.tap(find.text('Recorder').last);
  await tester.pump(const Duration(milliseconds: 400));
  return client;
}

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
  setUp(mockChannels);

  // covers: REC-09
  test('the A/V sync of a take', () {
    expect(avDrift({'drift_ms': -45.0, 'rate': '30000/1001'}, const []), (-45.0, true));
    expect(avDrift({'drift_ms': 20.0, 'rate': '30000/1001'}, const []), (20.0, false));
    expect(avDrift({'drift_ms': 20.0}, [{'fps': 60.0}]), (20.0, true));
  });

  // covers: REC-11, REC-12, REC-15
  filmTest('live view: signal, tally, timecode, markers, next take, storage, disk test and record anyway', (tester) async {
    await setSize(tester, sizes['phone portrait']!);
    final log = newLog();
    final asked = <(String, Map<String, dynamic>)>[];
    var starts = 0;
    final client = await openLive(tester, ops: (op, p) async {
      asked.add((op, p));
      if (op == 'recorder.start' && starts++ == 0) {
        throw RemoteError('disk too slow: this disk writes 120 MB/s; this mode needs 497 MB/s (+15 % margin) - frames '
            'would be lost: use H.265, a faster disk (NVMe) or a lower resolution (record anyway: force)');
      }
      return null;
    });
    log.found(find.byKey(const Key('signal-card')), 'the signal without a picture');
    log.found(find.byKey(const Key('tally')), 'recording tally');
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
    messenger.setMockMethodCallHandler(const MethodChannel('arstro/phone'), (call) async {
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
    log.found(card, 'slate title on the row');
    log.found(find.textContaining('TC 11:30:00;12'), 'start timecode on the row');
    log.found(find.byWidgetPredicate((w) => w is Icon && w.icon == Icons.star_rounded && w.semanticLabel == 'Good take'),
        'good-take star');
    log.found(find.text('A/V -45 ms'), 'A/V sync warning');
    log.found(find.text('2 markers'), 'markers');
    log.check('gallery rows');

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

    // GAL-13: the day's takes as the editor's sheet, saved into Downloads/Arstro
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
    await tester.tap(find.text('CSV'));
    await tester.pump(const Duration(milliseconds: 400));
    exOp(asked.last, 'gallery.csv', {'day': '2026-01-01'});
    ex(saved.single['method'], 'saveText');
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
    await tester.tap(find.text('CSV'));
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
