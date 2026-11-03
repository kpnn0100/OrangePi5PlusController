import 'package:flutter/material.dart';

import 'src/api.dart';
import 'src/devices_page.dart';

/// Arstro Devices: every device of your Arstro device managers in one app (MGR-01..06). The list
/// and the device pages are native; a device's own pages (its web UI, any port) open in a WebView
/// through an end-to-end encrypted channel (android/.../core/). Controlling the Pi over Bluetooth is
/// Arstro Remote (app/); sharing this phone is Arstro Remote Client (client_app/).
void main() {
  runApp(DevicesApp(api: ChannelApi()));
}

class DevicesApp extends StatelessWidget {
  const DevicesApp({super.key, required this.api});
  final DevicesApi api;

  // The same accent colour and surfaces as Arstro Remote and the web UI (UX-01).
  static const _seed = Color(0xFF7C9CFF);

  ThemeData _theme(Brightness b) {
    var scheme = ColorScheme.fromSeed(seedColor: _seed, brightness: b);
    if (b == Brightness.dark) {
      scheme = scheme.copyWith(
        primary: _seed,
        onPrimary: const Color(0xFF0B0E18),
        surface: const Color(0xFF0B0C0F),
        surfaceContainerLowest: const Color(0xFF0A0B0D),
        surfaceContainerLow: const Color(0xFF15181D),
        surfaceContainer: const Color(0xFF1B1F25),
        surfaceContainerHigh: const Color(0xFF22272E),
        surfaceContainerHighest: const Color(0xFF2A3038),
        outlineVariant: const Color(0xFF30363F),
      );
    }
    return ThemeData(
      colorScheme: scheme,
      useMaterial3: true,
      visualDensity: VisualDensity.standard,
      cardTheme: CardThemeData(
        elevation: 0,
        color: b == Brightness.dark ? scheme.surfaceContainerLow : scheme.surfaceContainerLowest,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(18),
          side: BorderSide(color: scheme.outlineVariant.withValues(alpha: 0.5)),
        ),
      ),
      snackBarTheme: const SnackBarThemeData(behavior: SnackBarBehavior.floating),
    );
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Arstro Devices',
      debugShowCheckedModeBanner: false,
      theme: _theme(Brightness.light),
      darkTheme: _theme(Brightness.dark),
      themeMode: ThemeMode.dark,
      home: DevicesPage(api: api),
    );
  }
}
