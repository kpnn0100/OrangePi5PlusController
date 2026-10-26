import 'package:flutter/foundation.dart';

import 'bt/bluetooth.dart';
import 'remote_client.dart';
import 'transport.dart';

/// One A/B slot of the Pi as the app sees it: its client (the phone's own Bluetooth link for
/// the slot that owns it, a Wi-Fi link for the others), its web address and password.
class SlotEntry {
  SlotEntry({required this.slot, required this.client, required this.self, this.base, this.token, this.note});

  /// "a", "b", ... (null for an unslotted server).
  final String? slot;

  /// null while the slot can't be reached ([note] says why).
  final RemoteClient? client;

  /// The slot the phone's Bluetooth link belongs to.
  final bool self;

  /// e.g. `http://<pi>:8081/` (null while the phone can't reach the Pi over Wi-Fi).
  final String? base;
  final String? token;

  /// Why this slot can't be used right now (stopped, no Wi-Fi), or null.
  final String? note;

  String get label => slot == null ? 'This Pi' : 'Slot ${slot!.toUpperCase()}';
  String get via => self ? 'Bluetooth' : 'Wi-Fi';
}

/// TERM-06: the app works with every A/B slot of the Pi it is connected to. The Bluetooth link
/// reaches one slot; `web.info` (asked over that link, so it carries passwords) lists the
/// others with their ports, and each running one gets its own [RemoteClient] over a WebSocket
/// to the address the media link found reachable. Shells, apps and everything else of a slot
/// then work exactly as over Bluetooth - the same frames on another pipe.
class SlotLinks extends ChangeNotifier {
  SlotLinks(this.primary) {
    primary.addListener(_sync);
    primary.media.addListener(_sync);
  }

  final RemoteClient primary;
  final Map<String, RemoteClient> _peers = {};
  final Map<String, String> _peerUrl = {};
  List<SlotEntry> _entries = const [];
  bool _disposed = false;

  /// Every slot, this link's first (just this one until `web.info` names others).
  List<SlotEntry> get entries => _entries.isEmpty
      ? [SlotEntry(slot: null, client: primary, self: true, base: primary.media.base, token: primary.media.token)]
      : _entries;

  SlotEntry entry(String? slot) => entries.firstWhere((e) => e.slot == slot, orElse: () => entries.first);

  /// Ask the Pi again (a slot may have been started or stopped since).
  Future<void> refresh() => primary.media.refresh();

  void _sync() {
    if (_disposed) return;
    final info = primary.media.info;
    if (!primary.isConnected || info == null) {
      if (!primary.isConnected) _dropAll();
      _publish(const []);
      return;
    }
    final me = info['slot'] as String?;
    final base = primary.media.base;
    final host = base == null ? null : Uri.parse(base).host;
    final out = <SlotEntry>[];
    final wanted = <String>{};
    for (final raw in (info['slots'] as List? ?? const [])) {
      final s = Map<String, dynamic>.from(raw as Map);
      final slot = s['slot'] as String;
      if (s['self'] == true || slot == me) {
        out.insert(0, SlotEntry(slot: slot, client: primary, self: true, base: base, token: primary.media.token));
        continue;
      }
      final port = s['port'] as int;
      final token = s['token'] as String?;
      String? note;
      if (s['running'] != true) {
        note = '${slot.toUpperCase()} is installed but not running';
      } else if (host == null) {
        note = "${primary.media.error ?? "The phone can't reach the Pi over Wi-Fi"} - slot ${slot.toUpperCase()} is reached over Wi-Fi.";
      } else {
        wanted.add(slot);
        _ensure(slot, Uri(scheme: 'ws', host: host, port: port, path: '/ws'), token);
      }
      out.add(SlotEntry(
          slot: slot,
          client: _peers[slot],
          self: false,
          base: host == null ? null : 'http://$host:$port/',
          token: token,
          note: note));
    }
    for (final slot in _peers.keys.toList()) {
      if (!wanted.contains(slot)) _drop(slot);
    }
    if (out.isEmpty || !out.first.self) {
      out.insert(0, SlotEntry(slot: me, client: primary, self: true, base: base, token: primary.media.token));
    }
    _publish(out);
  }

  void _ensure(String slot, Uri url, String? token) {
    if (_peerUrl[slot] == url.toString() && _peers[slot] != null) return;
    _drop(slot);
    final c = RemoteClient(primary.prefs, primary: false);
    _peers[slot] = c;
    _peerUrl[slot] = url.toString();
    c.addListener(_changed);
    c.connect(BtDevice(address: url.toString(), name: 'Slot ${slot.toUpperCase()}'),
        transport: WebSocketTransport(url, token));
  }

  void _drop(String slot) {
    final c = _peers.remove(slot);
    _peerUrl.remove(slot);
    if (c == null) return;
    c.removeListener(_changed);
    c.disconnect().whenComplete(c.dispose);
  }

  void _dropAll() {
    for (final slot in _peers.keys.toList()) {
      _drop(slot);
    }
  }

  void _publish(List<SlotEntry> list) {
    _entries = list;
    _changed();
  }

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    primary.removeListener(_sync);
    primary.media.removeListener(_sync);
    _dropAll();
    super.dispose();
  }
}
