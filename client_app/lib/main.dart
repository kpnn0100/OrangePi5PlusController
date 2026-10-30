import 'package:flutter/material.dart';

import 'src/first_run.dart';
import 'src/phone_page.dart';

/// Arstro Remote Client: shares this Android phone through the user's own Arstro device manager
/// (docs/phone/PHONE.md). Controlling the Pi is the other app, Arstro Remote (app/).
void main() {
  runApp(const ClientApp());
}

class ClientApp extends StatelessWidget {
  const ClientApp({super.key});

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
      title: 'Arstro Remote Client',
      debugShowCheckedModeBanner: false,
      theme: _theme(Brightness.light),
      darkTheme: _theme(Brightness.dark),
      themeMode: ThemeMode.dark,
      home: const _Home(),
    );
  }
}

/// The phone page, with the setup wizard over it on the first start (PHN-02; skippable).
class _Home extends StatefulWidget {
  const _Home();

  @override
  State<_Home> createState() => _HomeState();
}

class _HomeState extends State<_Home> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) FirstRunWizard.maybeShow(context).catchError((_) {});
    });
  }

  @override
  Widget build(BuildContext context) => const PhonePage();
}
