import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:xterm/xterm.dart';

import 'remote_client.dart';

enum TermStatus { opening, live, detached, ended }

class _TermWriter implements Sink<String> {
  _TermWriter(this.terminal);
  final Terminal terminal;
  @override
  void add(String data) => terminal.write(data);
  @override
  void close() {}
}

/// One remote shell. The xterm [Terminal] lives here (not in the widget) so the
/// screen content survives tab switches and Bluetooth reconnects.
class TermSession {
  TermSession(this.hub, this.localNumber) {
    terminal = Terminal(maxLines: 5000);
    terminal.onOutput = _onUserInput;
    terminal.onResize = (w, h, pw, ph) => _onResize(w, h);
    terminal.onTitleChange = (t) {
      title = t;
      hub._changed();
    };
    _resetDecoder();
  }

  final TerminalHub hub;
  final int localNumber;
  late final Terminal terminal;
  int? termId;
  TermStatus status = TermStatus.opening;
  String? title;
  int received = 0;
  int cols = 80, rows = 24;
  late ByteConversionSink _decoder;
  Timer? _resizeTimer;

  /// Sticky modifiers from the extra-keys bar, applied to the next keystroke.
  bool ctrl = false;
  bool alt = false;

  String get label => title?.isNotEmpty == true ? title! : 'Shell $localNumber';

  void _resetDecoder() {
    _decoder = const Utf8Decoder(allowMalformed: true).startChunkedConversion(_TermWriter(terminal));
  }

  /// [offset] is the stream position of bytes[0]. Anything before [received]
  /// is already on screen and is dropped, so replays never show twice.
  void onData(int offset, Uint8List bytes) {
    final end = offset + bytes.length;
    if (end <= received) return;
    if (offset > received) received = offset; // should not happen: bytes lost upstream
    final skip = received - offset;
    _decoder.add(skip == 0 ? bytes : Uint8List.sublistView(bytes, skip));
    received = end;
  }

  void _onUserInput(String data) {
    if (status != TermStatus.live || termId == null) return;
    if ((ctrl || alt) && data.length == 1) {
      var code = data.codeUnitAt(0);
      if (ctrl) {
        if (code >= 0x61 && code <= 0x7a) code -= 0x20; // a-z -> A-Z
        if (code >= 0x40 && code <= 0x5f) {
          data = String.fromCharCode(code - 0x40);
        } else if (data == ' ') {
          data = '\x00';
        }
      }
      if (alt) data = '\x1b$data';
      ctrl = false;
      alt = false;
      hub._changed();
    }
    hub.client.sendTerm(termId!, utf8.encode(data));
  }

  /// Sends raw text to the shell (used by the snippet/paste buttons).
  void sendText(String text) {
    if (status == TermStatus.live && termId != null) {
      hub.client.sendTerm(termId!, utf8.encode(text));
    }
  }

  void _onResize(int w, int h) {
    cols = w;
    rows = h;
    _resizeTimer?.cancel();
    _resizeTimer = Timer(const Duration(milliseconds: 150), () {
      if (status == TermStatus.live && termId != null) {
        hub.client.request('term.resize', {'term': termId, 'cols': cols, 'rows': rows}).catchError((_) => null);
      }
    });
  }

  void _note(String text) {
    terminal.write('\r\n\x1b[2;33m[$text]\x1b[0m\r\n');
  }
}

/// Keeps the list of shells and routes TERM frames to them.
class TerminalHub extends ChangeNotifier {
  TerminalHub(this.client);

  final RemoteClient client;
  final List<TermSession> sessions = [];
  int active = 0;
  int _counter = 0;
  bool _disposed = false;

  TermSession? get current => sessions.isEmpty ? null : sessions[active.clamp(0, sessions.length - 1)];

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  /// Repaint listeners after a session's sticky modifiers changed.
  void refresh() => _changed();

  TermSession? _byId(int id) {
    for (final s in sessions) {
      if (s.termId == id && s.status != TermStatus.ended) return s;
    }
    return null;
  }

  Future<TermSession> open() async {
    final s = TermSession(this, ++_counter);
    sessions.add(s);
    active = sessions.length - 1;
    _changed();
    try {
      // Bind the id synchronously: the shell's first output can arrive in the
      // same chunk as this reply and must not be dropped.
      await client.requestSync('term.open', {'cols': s.cols, 'rows': s.rows}, (r) {
        s.termId = (r as Map)['term'] as int;
        s.status = TermStatus.live;
      });
      // the view may have measured itself while we waited
      client.request('term.resize', {'term': s.termId, 'cols': s.cols, 'rows': s.rows}).catchError((_) => null);
    } catch (e) {
      s.status = TermStatus.ended;
      s._note('could not open shell: $e');
    }
    _changed();
    return s;
  }

  Future<void> close(TermSession s) async {
    sessions.remove(s);
    if (active >= sessions.length) active = sessions.isEmpty ? 0 : sessions.length - 1;
    _changed();
    if (s.termId != null && s.status != TermStatus.ended && client.isConnected) {
      try {
        await client.request('term.close', {'term': s.termId});
      } catch (_) {}
    }
  }

  void select(int index) {
    active = index;
    _changed();
  }

  Future<void> restart(TermSession s) async {
    final idx = sessions.indexOf(s);
    await close(s);
    final n = await open();
    if (idx >= 0 && idx < sessions.length) {
      sessions.remove(n);
      sessions.insert(idx, n);
      active = idx;
      _changed();
    }
  }

  void onData(int termId, int offset, Uint8List bytes) {
    _byId(termId)?.onData(offset, bytes);
  }

  void onExit(int termId, int? code) {
    final s = _byId(termId);
    if (s == null) return;
    s.status = TermStatus.ended;
    s._note('shell exited${code != null ? ' with code $code' : ''}');
    _changed();
  }

  void onDisconnected() {
    for (final s in sessions) {
      if (s.status == TermStatus.live || s.status == TermStatus.opening) s.status = TermStatus.detached;
    }
    _changed();
  }

  /// After (re)connecting: re-attach shells the Pi kept alive, adopt shells left
  /// from a previous app run, and mark the rest as ended.
  Future<void> onConnected(List<Map<String, dynamic>> remote) async {
    final remoteIds = {for (final t in remote) t['term'] as int};
    for (final s in sessions) {
      if (s.status != TermStatus.detached && s.status != TermStatus.opening) continue;
      if (s.termId != null && remoteIds.contains(s.termId)) {
        await _attach(s, since: s.received);
      } else {
        s.status = TermStatus.ended;
        s._note('shell was closed on the Pi while disconnected');
      }
    }
    final known = {for (final s in sessions) s.termId};
    for (final t in remote) {
      final id = t['term'] as int;
      if (known.contains(id)) continue;
      final s = TermSession(this, ++_counter)..termId = id;
      sessions.add(s);
      await _attach(s);
    }
    _changed();
  }

  Future<void> _attach(TermSession s, {int? since}) async {
    debugPrint('arstro: attach term=${s.termId} since=$since');
    try {
      // The Pi sends the reply before the replayed bytes; handle it synchronously
      // so the reset (on a gap) happens before those bytes are drawn.
      await client.requestSync('term.attach',
          {'term': s.termId, 'cols': s.cols, 'rows': s.rows, 'since': ?since}, (data) {
        final r = data as Map;
        if (r['gap'] == true) {
          final t = s.terminal;
          t.useMainBuffer();
          t.eraseDisplay();
          t.eraseScrollbackOnly();
          t.resetCursorStyle();
          t.setCursor(0, 0);
          s._resetDecoder();
        }
        s.received = r['start'] as int;
        s.status = TermStatus.live;
      });
    } catch (e) {
      s.status = TermStatus.ended;
      s._note('could not re-attach: $e');
    }
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
