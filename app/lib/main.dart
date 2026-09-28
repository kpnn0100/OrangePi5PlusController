import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'src/app_scope.dart';
import 'src/remote_client.dart';
import 'src/ui/connect_page.dart';
import 'src/ui/home_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();
  runApp(ArstroApp(client: RemoteClient(prefs), settings: Settings(prefs)));
}

class ArstroApp extends StatelessWidget {
  const ArstroApp({super.key, required this.client, required this.settings});

  final RemoteClient client;
  final Settings settings;

  static const _seed = Color(0xFF3D7BFF);

  ThemeData _theme(Brightness b) {
    final scheme = ColorScheme.fromSeed(seedColor: _seed, brightness: b);
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
    return AppScope(
      client: client,
      settings: settings,
      child: MaterialApp(
        title: 'Arstro Remote',
        debugShowCheckedModeBanner: false,
        theme: _theme(Brightness.light),
        darkTheme: _theme(Brightness.dark),
        themeMode: ThemeMode.dark,
        home: const _Root(),
      ),
    );
  }
}

/// Shows the device picker until a session exists, then the main shell. While a
/// session is only reconnecting the shell stays up (terminals keep their state).
class _Root extends StatefulWidget {
  const _Root();

  @override
  State<_Root> createState() => _RootState();
}

class _RootState extends State<_Root> {
  bool _inSession = false;
  bool _everConnected = false;

  @override
  Widget build(BuildContext context) {
    final client = AppScope.of(context).client;
    return ListenableBuilder(
      listenable: client,
      builder: (context, _) {
        final s = client.state;
        if (s == LinkState.connected) {
          _inSession = true;
          _everConnected = true;
        } else if (s == LinkState.idle || s == LinkState.failed) {
          _inSession = false;
        }
        return AnimatedSwitcher(
          duration: const Duration(milliseconds: 250),
          child: _inSession
              ? const HomePage(key: ValueKey('home'))
              : ConnectPage(key: const ValueKey('connect'), allowAutoConnect: !_everConnected),
        );
      },
    );
  }
}
