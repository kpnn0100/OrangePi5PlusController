import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../remote_client.dart';
import 'recorder/common.dart';

/// System (ADM-01..03, CON-05): web access, Bluetooth pairing, connected controllers.
/// Shown inside the settings page; all of it follows the Pi's live state.
class SystemSections extends StatefulWidget {
  const SystemSections({super.key, required this.client});
  final RemoteClient client;

  @override
  State<SystemSections> createState() => _SystemSectionsState();
}

class _SystemSectionsState extends State<SystemSections> {
  Map<String, dynamic>? _web;
  bool _reveal = false;
  Timer? _tick;
  DateTime _pairingAt = DateTime.now();

  RemoteClient get client => widget.client;

  @override
  void initState() {
    super.initState();
    _loadWeb();
    client.topic('web').addListener(_loadWeb);
    client.topic('pairing').addListener(_onPairing);
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && asMap(client.topic('pairing').value)['pairing_open'] == true) setState(() {});
    });
  }

  @override
  void dispose() {
    client.topic('web').removeListener(_loadWeb);
    client.topic('pairing').removeListener(_onPairing);
    _tick?.cancel();
    super.dispose();
  }

  void _onPairing() => _pairingAt = DateTime.now();

  Future<void> _loadWeb() async {
    if (!client.isConnected) return;
    try {
      final w = asMap(await client.request('web.info'));
      if (mounted) setState(() => _web = w);
    } catch (_) {}
  }

  Future<void> _changePassword() async {
    final a = TextEditingController(), b = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Change password'),
        content: Column(mainAxisSize: MainAxisSize.min, children: [
          const Text('Browsers and remote CLIs must sign in again. This app picks it up by itself.'),
          const SizedBox(height: 12),
          TextField(controller: a, obscureText: true, decoration: const InputDecoration(labelText: 'New password (8+ characters)')),
          TextField(controller: b, obscureText: true, decoration: const InputDecoration(labelText: 'Again')),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Save')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    if (a.text != b.text) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('The passwords differ')));
      return;
    }
    final r = await runOp(context, client, 'web.set_password', {'password': a.text}, 'Password changed');
    if (r != null && mounted) setState(() => _web = asMap(r));
    client.media.refresh();
  }

  Future<void> _setRequired(bool required) async {
    if (!required &&
        !await confirm(context, 'Turn the password off?',
            'Anyone on this network could then open a shell on the Pi, record, delete recordings and change Wi-Fi.',
            ok: 'Turn off')) {
      return;
    }
    if (!mounted) return;
    final r = await runOp(context, client, 'web.set_auth', {'required': required}, required ? 'Password required' : 'Open access');
    if (r != null && mounted) setState(() => _web = asMap(r));
    client.media.refresh();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([client.topic('pairing'), client.topic('controllers'), client.media]),
      builder: (context, _) => Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        ..._webSection(context),
        ..._btSection(context),
        ..._controllersSection(context),
      ]),
    );
  }

  List<Widget> _webSection(BuildContext context) {
    final w = _web ?? const {};
    final open = w['auth'] == 'open';
    final token = w['token'] as String? ?? '';
    final media = client.media;
    return [
      const _Header('Web access'),
      for (final u in List<String>.from(w['urls'] as List? ?? const []))
        ListTile(
          leading: const Icon(Icons.public),
          title: SelectableText(u),
          subtitle: const Text('Open it in any browser on the same network'),
          trailing: IconButton(
            tooltip: 'Copy',
            icon: const Icon(Icons.copy_rounded),
            onPressed: () => Clipboard.setData(ClipboardData(text: u)),
          ),
        ),
      ListTile(
        leading: Icon(media.ready ? Icons.wifi_rounded : Icons.wifi_off_rounded, color: media.ready ? kOk : kWarn),
        title: Text(media.ready ? 'Media over Wi-Fi: ${media.base}' : 'No Wi-Fi link to the Pi'),
        subtitle: media.ready ? const Text('Preview, thumbnails and playback') : Text(media.error ?? 'Looking…'),
        trailing: IconButton(tooltip: 'Check again', icon: const Icon(Icons.refresh_rounded), onPressed: media.refresh),
      ),
      SwitchListTile(
        secondary: const Icon(Icons.lock_outline),
        title: const Text('Require a password'),
        subtitle: Text(open ? 'Off: anyone on this network can use the Pi' : 'Browsers ask once and remember it'),
        value: !open,
        onChanged: _web == null ? null : _setRequired,
      ),
      if (!open && _web != null)
        ListTile(
          leading: const Icon(Icons.key_rounded),
          title: Text(_reveal ? token : '•' * 12, style: const TextStyle(fontFamily: 'monospace')),
          subtitle: const Text('Web and remote-CLI password'),
          trailing: Row(mainAxisSize: MainAxisSize.min, children: [
            IconButton(
              tooltip: _reveal ? 'Hide' : 'Show',
              icon: Icon(_reveal ? Icons.visibility_off_outlined : Icons.visibility_outlined),
              onPressed: () => setState(() => _reveal = !_reveal),
            ),
            IconButton(tooltip: 'Change', icon: const Icon(Icons.edit_outlined), onPressed: _changePassword),
          ]),
        ),
    ];
  }

  List<Widget> _btSection(BuildContext context) {
    final p = asMap(client.topic('pairing').value);
    final left = ((p['pairing_remaining'] as num? ?? 0) - DateTime.now().difference(_pairingAt).inSeconds).clamp(0, 1 << 30);
    final open = p['pairing_open'] == true && left > 0;
    final devices = asList(p['paired_devices']);
    return [
      const _Header('Bluetooth'),
      ListTile(
        leading: const Icon(Icons.bluetooth_rounded),
        title: Text(p['alias'] as String? ?? 'Pi'),
        subtitle: Text(open
            ? 'Pairing open · ${clock(left)} left'
            : 'Pairing closed: new phones can pair only while it is open'),
        trailing: open
            ? TextButton(onPressed: () => runOp(context, client, 'admin.pair', {'seconds': 0}, 'Pairing closed'),
                child: const Text('Close'))
            : FilledButton.tonal(
                onPressed: () => runOp(context, client, 'admin.pair', {'seconds': 600}, 'Pairing open for 10 min'),
                child: const Text('Open 10 min')),
      ),
      for (final d in devices)
        ListTile(
          leading: const Icon(Icons.smartphone_rounded),
          title: Text(d['name'] as String? ?? d['address'] as String? ?? ''),
          subtitle: Text('${d['address']}${d['connected'] == true ? ' · connected' : ''}'),
          trailing: TextButton(
            style: TextButton.styleFrom(foregroundColor: kRec),
            onPressed: () async {
              if (!await confirm(context, 'Forget ${d['name'] ?? d['address']}?',
                  'It must pair again (inside the pairing window) to connect.', ok: 'Forget')) {
                return;
              }
              if (context.mounted) runOp(context, client, 'admin.unpair', {'address': d['address']}, 'Device forgotten');
            },
            child: const Text('Forget'),
          ),
        ),
    ];
  }

  List<Widget> _controllersSection(BuildContext context) {
    final list = asList(client.topic('controllers').value);
    final me = client.server?['session'];
    const names = {'app': 'Android app', 'web': 'Web', 'cli': 'CLI', 'remote': 'Remote CLI', 'local': 'Local'};
    return [
      _Header('Connected controllers · ${list.length}'),
      for (final c in list)
        ListTile(
          leading: Icon(switch (c['controller']) {
            'app' => Icons.smartphone_rounded,
            'web' => Icons.public,
            _ => Icons.terminal_rounded,
          }),
          title: Text('${names[c['controller']] ?? c['controller']}${c['session'] == me ? ' (this phone)' : ''}'),
          subtitle: Text('${c['kind']} · ${c['peer']}'),
        ),
    ];
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
