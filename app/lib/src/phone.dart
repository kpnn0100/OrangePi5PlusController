import 'package:flutter/services.dart';

/// What the app asks of the phone itself (Kotlin `PhoneBridge`). Nothing here talks to the
/// machine - that is the Bluetooth link's job alone (CON-06): it hands an address to the
/// phone's browser, a hotspot to the phone's Wi-Fi settings, a file to Downloads.
class Phone {
  Phone._();

  static const _channel = MethodChannel('arstro/phone');

  /// Opens [url] in the phone's browser. False when no app can open it.
  static Future<bool> openUrl(String url) async =>
      await _channel.invokeMethod<bool>('openUrl', {'url': url}) ?? false;

  /// Offers the hotspot to the phone's Wi-Fi (WIFI-08): Android 11+ shows its own "save this
  /// network?" sheet; older phones open the Wi-Fi settings. Returns what happened:
  /// `saved`, `settings` or `cancelled`.
  static Future<String> joinWifi(String ssid, String password) async =>
      await _channel.invokeMethod<String>('joinWifi', {'ssid': ssid, 'password': password}) ?? 'cancelled';

  /// Writes text the machine sent (the editor's CSV, GAL-13) into Downloads/Arstro; returns where.
  static Future<String?> saveText(String name, String text, {String mime = 'text/csv'}) =>
      _channel.invokeMethod<String>('saveText', {'name': name, 'text': text, 'mime': mime});
}
