import 'dart:async';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// "Share this phone" (docs/phone/PHONE.md): the Kotlin phone service through
/// MethodChannel `arstro/phone` and EventChannel `arstro/phone/events`.
class PhoneApi extends ChangeNotifier {
  PhoneApi._();
  static final PhoneApi instance = PhoneApi._();

  static const _methods = MethodChannel('arstro/phone');
  static const _events = EventChannel('arstro/phone/events');

  /// Only Android phones can be shared.
  static bool get supported => !kIsWeb && Platform.isAndroid;

  PhoneState? state;
  StreamSubscription<dynamic>? _sub;

  /// Follow the service's state (idempotent).
  void listen() {
    if (!supported || _sub != null) return;
    _sub = _events.receiveBroadcastStream().listen((m) {
      if (m is Map) _set(PhoneState.fromMap(m));
    }, onError: (_) {});
  }

  void _set(PhoneState s) {
    state = s;
    notifyListeners();
  }

  Future<PhoneState?> _call(String method, [Map<String, Object?>? args]) async {
    final r = await _methods.invokeMethod<Object?>(method, args);
    if (r is Map) {
      final s = PhoneState.fromMap(r);
      _set(s);
      return s;
    }
    return state;
  }

  Future<PhoneState?> refresh() async {
    if (!supported) return null;
    try {
      return await _call('state');
    } on MissingPluginException {
      return null;
    }
  }

  Future<PhoneState?> setFeature(String id, bool on) => _call('setFeature', {'id': id, 'on': on});

  /// Ask for what a feature still needs (`id`) or for one permission / special access (`name`);
  /// completes when the user is back.
  Future<PhoneState?> request({String? id, String? name}) => _call('request', {'id': id, 'name': name});

  Future<PhoneState?> setRelay({String? url, String? token, String? name}) =>
      _call('setRelay', {'url': url, 'token': token, 'name': name});

  Future<PhoneKey> key() async => PhoneKey.fromMap(await _methods.invokeMethod<Map<Object?, Object?>>('key') ?? {});

  Future<PhoneKey> rotateKey() async {
    final k = PhoneKey.fromMap(await _methods.invokeMethod<Map<Object?, Object?>>('rotateKey') ?? {});
    await refresh();
    return k;
  }

  Future<PhoneState?> start() => _call('start');
  Future<PhoneState?> stop() => _call('stop');
  Future<PhoneState?> finishFirstRun() => _call('finishFirstRun');
  Future<void> openAppInfo() => _methods.invokeMethod<void>('openAppInfo');
}

class PhoneKey {
  const PhoneKey(this.key, this.keyId);
  final String key;
  final String keyId;

  factory PhoneKey.fromMap(Map<Object?, Object?> m) => PhoneKey('${m['key'] ?? ''}', '${m['key_id'] ?? ''}');
}

/// Something a feature still needs: a runtime permission or a special access.
class PhoneNeed {
  const PhoneNeed(this.name, this.kind, this.title);
  final String name; // android.permission.X or the special access (OVERLAY, ACCESSIBILITY, ...)
  final String kind; // permission | special
  final String title;

  factory PhoneNeed.fromMap(Map<Object?, Object?> m) =>
      PhoneNeed('${m['name'] ?? ''}', '${m['kind'] ?? ''}', '${m['title'] ?? m['name'] ?? ''}');
}

class PhoneFeature {
  const PhoneFeature({
    required this.id,
    required this.title,
    required this.on,
    required this.ready,
    required this.missing,
    this.note,
  });

  final String id;
  final String title;
  final bool on;
  final bool ready;
  final List<PhoneNeed> missing;
  final String? note;

  factory PhoneFeature.fromMap(Map<Object?, Object?> m) => PhoneFeature(
        id: '${m['id']}',
        title: '${m['title'] ?? m['id']}',
        on: m['on'] == true,
        ready: m['ready'] == true,
        missing: _needs(m['missing']),
        note: m['note'] as String?,
      );
}

List<PhoneNeed> _needs(Object? v) =>
    v is List ? [for (final e in v) if (e is Map) PhoneNeed.fromMap(e)] : const [];

class PhoneState {
  const PhoneState({
    required this.running,
    required this.enabled,
    required this.relayState,
    required this.error,
    required this.url,
    required this.name,
    required this.id,
    required this.transport,
    required this.viewers,
    required this.tokenSet,
    required this.keyId,
    required this.firstRun,
    required this.features,
    required this.serviceMissing,
    required this.sdk,
    required this.defaultUrl,
  });

  final bool running;
  final bool enabled;

  /// offline | connecting | online | error
  final String relayState;
  final String? error;
  final String url;
  final String name;
  final String id;

  /// ws | http while online
  final String? transport;
  final int viewers;
  final bool tokenSet;
  final String keyId;
  final bool firstRun;
  final List<PhoneFeature> features;
  final List<PhoneNeed> serviceMissing;
  final int sdk;
  final String defaultUrl;

  bool get online => relayState == 'online';

  factory PhoneState.fromMap(Map<Object?, Object?> m) {
    final relay = (m['relay'] as Map?) ?? const {};
    final service = (m['service'] as Map?) ?? const {};
    final viewers = relay['viewers'];
    return PhoneState(
      running: m['running'] == true,
      enabled: m['enabled'] == true,
      relayState: '${relay['state'] ?? 'offline'}',
      error: relay['error'] as String?,
      url: '${relay['url'] ?? ''}',
      name: '${relay['name'] ?? ''}',
      id: '${relay['id'] ?? ''}',
      transport: relay['transport'] as String?,
      viewers: viewers is List ? viewers.length : 0,
      tokenSet: relay['token_set'] == true,
      keyId: '${m['key_id'] ?? ''}',
      firstRun: m['first_run'] == true,
      features: [
        for (final f in (m['features'] as List?) ?? const [])
          if (f is Map) PhoneFeature.fromMap(f)
      ],
      serviceMissing: _needs(service['missing']),
      sdk: (m['sdk'] as int?) ?? 0,
      defaultUrl: '${m['default_url'] ?? ''}',
    );
  }

  /// "Online over HTTP · 2 viewers", "Connecting…", "Offline".
  String get summary {
    if (!running) return 'Not sharing';
    switch (relayState) {
      case 'online':
        final how = transport == 'http' ? 'HTTP' : 'WebSocket';
        return 'Online over $how · $viewers viewer${viewers == 1 ? '' : 's'}';
      case 'connecting':
        return 'Connecting…';
      case 'error':
        return 'Offline - retrying';
      default:
        return 'Offline';
    }
  }
}

/// One line per feature for the page and the wizard, with the Android limits (PHONE.md).
const featureInfo = <String, String>{
  'status': 'Battery, storage, memory, network and signal - live.',
  'screen': 'See the screen live and control it: taps, swipes, typing, Back / Home keys. '
      'Android asks "Start now" when capture starts; control needs the accessibility service.',
  'camera': 'Watch the front or back camera, take a photo, record a video. Android shows its camera dot.',
  'call': 'Voice calls from the browser: the phone rings full screen (also locked); answer to talk.',
  'mic': "Listen to the phone's microphone live. Android shows its microphone dot.",
  'messages': 'Read SMS threads, send an SMS, see new messages live.',
  'contacts': 'Search the contacts and see the recent calls.',
  'find': 'Location now and a 7-day history, ring loudly also on silent, lock with a message. '
      'Needs location "Allow all the time".',
  'files': "Browse the phone's storage, download, upload, rename and delete files.",
  'notifications': 'See the notifications live, dismiss them, reply where the app allows it.',
  'clipboard': 'Put text on the clipboard. Reading it works only while this app is open (an Android rule).',
  'apps': 'List the installed apps and open one.',
  'tools': 'Volume, media keys, speak a text, vibrate, torch, brightness, open a link.',
  'shell': "Run commands in this app's own sandbox (no root).",
};
