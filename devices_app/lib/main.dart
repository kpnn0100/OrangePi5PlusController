import 'package:flutter/material.dart';

import 'src/api.dart';
import 'src/devices_page.dart';
import 'src/theme.dart';

/// Arstro Devices: every device of your Arstro device managers in one app (MGR-01..06). The list
/// and the device pages are native; a device's own pages (its web UI, any port) open in a WebView
/// through an end-to-end encrypted channel (android/.../core/). Controlling the Pi over Bluetooth is
/// Arstro Remote (app/); sharing this phone is Arstro Remote Client (client_app/).
void main() {
  Arstro.registerFontLicence();
  runApp(DevicesApp(api: ChannelApi()));
}

class DevicesApp extends StatelessWidget {
  const DevicesApp({super.key, required this.api});
  final DevicesApi api;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Arstro Devices',
      debugShowCheckedModeBanner: false,
      theme: Arstro.theme(),                // dark only, the same tokens as the web UIs (docs/design.md)
      home: DevicesPage(api: api),
    );
  }
}
