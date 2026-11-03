import 'package:flutter/material.dart';

import 'src/first_run.dart';
import 'src/phone_page.dart';
import 'src/theme.dart';

/// Arstro Remote Client: shares this Android phone through the user's own Arstro device manager
/// (docs/phone/PHONE.md). Controlling the Pi is the other app, Arstro Remote (app/).
void main() {
  Arstro.registerFontLicence();
  runApp(const ClientApp());
}

class ClientApp extends StatelessWidget {
  const ClientApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Arstro Remote Client',
      debugShowCheckedModeBanner: false,
      theme: Arstro.theme(),                // dark only, the same tokens as the web UIs (docs/design.md)
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
