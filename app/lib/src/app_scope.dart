import 'package:flutter/widgets.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'remote_client.dart';

/// User preferences (persisted).
class Settings extends ChangeNotifier {
  Settings(this._prefs);
  final SharedPreferences _prefs;

  double get pointerSpeed => _prefs.getDouble('pointer_speed') ?? 1.6;
  set pointerSpeed(double v) => _set(() => _prefs.setDouble('pointer_speed', v));

  double get scrollSpeed => _prefs.getDouble('scroll_speed') ?? 1.0;
  set scrollSpeed(double v) => _set(() => _prefs.setDouble('scroll_speed', v));

  bool get naturalScroll => _prefs.getBool('natural_scroll') ?? true;
  set naturalScroll(bool v) => _set(() => _prefs.setBool('natural_scroll', v));

  bool get tapToClick => _prefs.getBool('tap_to_click') ?? false;
  set tapToClick(bool v) => _set(() => _prefs.setBool('tap_to_click', v));

  bool get haptics => _prefs.getBool('haptics') ?? true;
  set haptics(bool v) => _set(() => _prefs.setBool('haptics', v));

  bool get autoConnect => _prefs.getBool('auto_connect') ?? true;
  set autoConnect(bool v) => _set(() => _prefs.setBool('auto_connect', v));

  double get terminalFontSize => _prefs.getDouble('term_font') ?? 14;
  set terminalFontSize(double v) => _set(() => _prefs.setDouble('term_font', v));

  void _set(Future<bool> Function() write) {
    write();
    notifyListeners();
  }
}

class AppScope extends InheritedWidget {
  const AppScope({super.key, required this.client, required this.settings, required super.child});

  final RemoteClient client;
  final Settings settings;

  static AppScope of(BuildContext context) {
    final scope = context.dependOnInheritedWidgetOfExactType<AppScope>();
    assert(scope != null, 'AppScope missing');
    return scope!;
  }

  @override
  bool updateShouldNotify(AppScope oldWidget) => client != oldWidget.client || settings != oldWidget.settings;
}
