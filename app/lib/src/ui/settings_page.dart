import 'package:flutter/material.dart';

import '../app_scope.dart';
import '../proto/frames.dart';
import '../remote_client.dart';
import 'system_section.dart';

class SettingsPage extends StatelessWidget {
  const SettingsPage({super.key});

  @override
  Widget build(BuildContext context) {
    final scope = AppScope.of(context);
    final s = scope.settings;
    final client = scope.client;
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListenableBuilder(
        listenable: Listenable.merge([s, client]),
        builder: (context, _) => Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: ListView(
              padding: const EdgeInsets.symmetric(vertical: 8),
              children: [
                const _Header('Touchpad'),
                _SliderTile(
                  title: 'Pointer speed',
                  value: s.pointerSpeed,
                  min: 0.5,
                  max: 4,
                  label: '${s.pointerSpeed.toStringAsFixed(1)}×',
                  onChanged: (v) => s.pointerSpeed = v,
                ),
                _SliderTile(
                  title: 'Scroll speed',
                  value: s.scrollSpeed,
                  min: 0.3,
                  max: 3,
                  label: '${s.scrollSpeed.toStringAsFixed(1)}×',
                  onChanged: (v) => s.scrollSpeed = v,
                ),
                SwitchListTile(
                  title: const Text('Natural scrolling'),
                  subtitle: const Text('Content follows your fingers'),
                  value: s.naturalScroll,
                  onChanged: (v) => s.naturalScroll = v,
                ),
                SwitchListTile(
                  title: const Text('Tap pad to click'),
                  subtitle: const Text('Off: the pad only moves the pointer, use the buttons to click'),
                  value: s.tapToClick,
                  onChanged: (v) => s.tapToClick = v,
                ),
                SwitchListTile(
                  title: const Text('Vibrate on click'),
                  value: s.haptics,
                  onChanged: (v) => s.haptics = v,
                ),
                const _Header('Terminal'),
                _SliderTile(
                  title: 'Font size',
                  value: s.terminalFontSize,
                  min: 9,
                  max: 24,
                  divisions: 15,
                  label: s.terminalFontSize.toStringAsFixed(0),
                  onChanged: (v) => s.terminalFontSize = v,
                ),
                const _Header('Connection'),
                SwitchListTile(
                  title: const Text('Auto-connect on start'),
                  subtitle: Text(client.lastDevice == null
                      ? 'Connects to the last used Pi'
                      : 'Last used: ${client.lastDevice!.label}'),
                  value: s.autoConnect,
                  onChanged: (v) => s.autoConnect = v,
                ),
                if (client.isConnected) SystemSections(client: client),
                const _Header('About'),
                ListTile(
                  leading: const Icon(Icons.info_outline),
                  title: const Text('Arstro Remote $kAppVersion'),
                  subtitle: Text(client.server == null
                      ? 'Protocol v$kProtoVersion'
                      : 'Pi service ${client.server!['version']} on ${client.hostname} · '
                          'protocol v${client.server!['proto']}'),
                ),
                const ListTile(
                  leading: Icon(Icons.fingerprint),
                  title: Text('Service UUID'),
                  subtitle: SelectableText(kServiceUuid),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 20, 16, 4),
        child: Text(text,
            style: Theme.of(context).textTheme.titleSmall?.copyWith(color: Theme.of(context).colorScheme.primary)),
      );
}

class _SliderTile extends StatelessWidget {
  const _SliderTile({
    required this.title,
    required this.value,
    required this.min,
    required this.max,
    required this.label,
    required this.onChanged,
    this.divisions,
  });

  final String title;
  final double value, min, max;
  final String label;
  final int? divisions;
  final ValueChanged<double> onChanged;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      title: Text(title),
      subtitle: Slider(
        value: value.clamp(min, max),
        min: min,
        max: max,
        divisions: divisions,
        label: label,
        onChanged: onChanged,
      ),
      trailing: SizedBox(width: 44, child: Text(label, textAlign: TextAlign.right)),
    );
  }
}
