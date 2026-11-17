import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../phone.dart';
import '../remote_client.dart';
import '../theme.dart';
import 'recorder/common.dart';
import 'widgets.dart';

/// The machine's own Wi-Fi network (WIFI-08), started over Bluetooth: the way in where there is
/// no router. Whether it runs follows the pushed `wifi` state; its settings and password come
/// from `wifi.hotspot` (asked for, never pushed). Once it runs, this phone can join it and open
/// the web UI inside it in the browser - the app itself stays on Bluetooth (CON-06).
class HotspotCard extends StatefulWidget {
  const HotspotCard({super.key, required this.client});
  final RemoteClient client;

  @override
  State<HotspotCard> createState() => _HotspotCardState();
}

class _HotspotCardState extends State<HotspotCard> {
  Map<String, dynamic>? _hs;
  String? _error;
  bool _busy = false;
  bool _show = false;
  bool? _wasOn;

  RemoteClient get client => widget.client;
  Map<String, dynamic> get _live => asMap(asMap(client.topic('wifi').value)['hotspot']);
  bool get _on => _live['active'] == true;

  @override
  void initState() {
    super.initState();
    client.topic('wifi').addListener(_onWifi);
    client.addListener(_onClient);
    _load();
  }

  @override
  void dispose() {
    client.topic('wifi').removeListener(_onWifi);
    client.removeListener(_onClient);
    super.dispose();
  }

  void _onClient() {
    if (client.isConnected && _hs == null && !_busy) _load();
  }

  void _onWifi() {
    final on = _on;
    if (_wasOn != null && on != _wasOn) _load();      // started or stopped elsewhere
    _wasOn = on;
    if (mounted) setState(() {});
  }

  Future<void> _load() async {
    if (!client.isConnected) return;
    try {
      final r = asMap(await client.request('wifi.hotspot', const {}, const Duration(seconds: 20)));
      if (!mounted) return;
      setState(() {
        _hs = r;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    }
  }

  Future<void> _start([Map<String, dynamic> args = const {}]) async {
    final w = asMap(client.topic('wifi').value);
    final ssid = (args['ssid'] as String?) ?? _hs?['ssid'] as String? ?? 'the hotspot';
    if (w['connected'] == true && !_on) {
      final net = w['ssid'] ?? 'its Wi-Fi network';
      final ok = await confirm(context, 'Start the hotspot?',
          'The Pi leaves $net while its hotspot is on (one radio): whatever reaches it over $net loses it. '
          'This app stays connected over Bluetooth. Join $ssid and open the web address shown here; '
          'stopping the hotspot brings $net back.',
          ok: 'Start', danger: false);
      if (!ok || !mounted) return;
    }
    setState(() => _busy = true);
    final r = await runOp(context, client, 'wifi.hotspot.start', args, null, const Duration(seconds: 90));
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (r != null) _hs = asMap(asMap(r)['hotspot']);
    });
    if (r != null) _snack(asMap(r)['message'] as String? ?? 'Hotspot on');
  }

  Future<void> _stop() async {
    setState(() => _busy = true);
    final r = await runOp(context, client, 'wifi.hotspot.stop', const {}, null, const Duration(seconds: 90));
    if (!mounted) return;
    setState(() => _busy = false);
    if (r != null) {
      _snack(asMap(r)['message'] as String? ?? 'Hotspot off');
      _load();
    }
  }

  void _snack(String text) =>
      ScaffoldMessenger.maybeOf(context)?.showSnackBar(SnackBar(content: Text(text), duration: const Duration(seconds: 3)));

  Future<void> _settings() async {
    final hs = _hs ?? const {};
    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (_) => _SettingsDialog(ssid: hs['ssid'] as String? ?? '', band: hs['band'] == '5 GHz' ? '5' : '2.4',
          hasPassword: hs['has_password'] == true),
    );
    if (result != null && mounted) _start(result);
  }

  Future<void> _join() async {
    final hs = _hs ?? const {};
    final ssid = hs['ssid'] as String? ?? '';
    final pw = hs['password'] as String? ?? '';
    final r = await Phone.joinWifi(ssid, pw);
    if (!mounted) return;
    if (r == 'settings') {
      await Clipboard.setData(ClipboardData(text: pw));
      _snack('Password copied: pick $ssid in the Wi-Fi settings and paste it');
    } else if (r == 'saved') {
      _snack('$ssid saved on this phone - it joins when the phone picks it');
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final hs = _hs;
    final live = _live;
    final on = _on;
    final muted = theme.colorScheme.onSurfaceVariant;
    Widget body;
    if (hs == null) {
      body = _error != null
          ? Text(_error!, style: theme.textTheme.bodySmall?.copyWith(color: kWarn))
          : const Padding(padding: EdgeInsets.all(8), child: LinearProgressIndicator());
    } else if (hs['supported'] == false) {
      body = Text(hs['device'] == null ? 'This machine has no Wi-Fi device.' : '${hs['device']} cannot run a hotspot.',
          style: theme.textTheme.bodyMedium?.copyWith(color: muted));
    } else {
      final pw = hs['password'] as String?;
      final web = live['web'] as String? ?? hs['web'] as String?;
      final clients = live['clients'] as int?;
      body = Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: on ? kOk : theme.colorScheme.outline, shape: BoxShape.circle),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(hs['ssid'] as String? ?? '', key: const Key('hotspot-ssid'),
                  maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600)),
              Text(
                  [
                    on ? 'On' : 'Off',
                    hs['band'],
                    if (on && clients != null) '$clients device${clients == 1 ? '' : 's'} joined',
                  ].whereType<String>().join(' · '),
                  style: theme.textTheme.bodySmall?.copyWith(color: muted)),
            ]),
          ),
        ]),
        const SizedBox(height: 10),
        if (!on && hs['problem'] is String) ...[
          Text(hs['problem'] as String, key: const Key('hotspot-problem'),
              style: theme.textTheme.bodySmall?.copyWith(color: kWarn)),
          const SizedBox(height: 8),
        ],
        if (!on)
          Text('Where there is no router: the Pi makes its own Wi-Fi network. Join it from a laptop or this phone, '
              'then open the web address shown here. The Pi leaves its current Wi-Fi network while it runs.',
              style: theme.textTheme.bodySmall?.copyWith(color: muted)),
        if (pw != null || on) ...[
          const SizedBox(height: 4),
          _row(context, 'Password',
              Text(pw == null ? 'made when it first starts' : _show ? pw : '•' * pw.length.clamp(8, 12),
                  key: const Key('hotspot-password'),
                  style: theme.textTheme.bodyMedium?.copyWith(fontFamily: 'GeistMono')),
              pw == null
                  ? null
                  : [
                      IconButton(
                        tooltip: _show ? 'Hide' : 'Show',
                        visualDensity: VisualDensity.compact,
                        icon: Icon(_show ? Icons.visibility_off_outlined : Icons.visibility_outlined, size: 20),
                        onPressed: () => setState(() => _show = !_show),
                      ),
                      IconButton(
                        tooltip: 'Copy',
                        visualDensity: VisualDensity.compact,
                        icon: const Icon(Icons.copy_rounded, size: 20),
                        onPressed: () => Clipboard.setData(ClipboardData(text: pw)),
                      ),
                    ]),
        ],
        if (on && web != null)
          _row(context, 'Web address',
              Text(web, key: const Key('hotspot-web'), style: theme.textTheme.bodyMedium?.copyWith(fontFamily: 'GeistMono')),
              [
                IconButton(
                  tooltip: 'Copy',
                  visualDensity: VisualDensity.compact,
                  icon: const Icon(Icons.copy_rounded, size: 20),
                  onPressed: () => Clipboard.setData(ClipboardData(text: web)),
                ),
              ]),
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, alignment: WrapAlignment.end, children: [
          if (on) ...[
            OutlinedButton.icon(
              onPressed: pw == null ? null : _join,
              icon: const Icon(Icons.wifi_rounded, size: 18),
              label: const Text('Join on this phone'),
            ),
            if (web != null)
              OutlinedButton.icon(
                onPressed: () => Phone.openUrl(web),
                icon: const Icon(Icons.open_in_new_rounded, size: 18),
                label: const Text('Open web UI'),
              ),
            OutlinedButton(
              key: const Key('hotspot-stop'),
              onPressed: _busy ? null : _stop,
              child: const Text('Stop'),
            ),
          ] else ...[
            TextButton(onPressed: _busy ? null : _settings, child: const Text('Settings')),
            FilledButton(
              key: const Key('hotspot-start'),
              onPressed: _busy || !client.isConnected ? null : _start,
              child: _busy
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Start hotspot'),
            ),
          ],
        ]),
      ]);
    }
    return SectionCard(title: 'Hotspot', icon: Icons.wifi_tethering_rounded, child: body);
  }

  Widget _row(BuildContext context, String label, Widget value, List<Widget>? actions) {
    final theme = Theme.of(context);
    return Row(children: [
      SizedBox(
        width: 104,
        child: Text(label, style: theme.textTheme.bodyMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ),
      Expanded(child: value),
      ...?actions,
    ]);
  }
}

class _SettingsDialog extends StatefulWidget {
  const _SettingsDialog({required this.ssid, required this.band, required this.hasPassword});
  final String ssid;
  final String band;
  final bool hasPassword;

  @override
  State<_SettingsDialog> createState() => _SettingsDialogState();
}

class _SettingsDialogState extends State<_SettingsDialog> {
  late final _ssid = TextEditingController(text: widget.ssid);
  final _pw = TextEditingController();
  late String _band = widget.band;

  String? get _ssidError {
    final n = _ssid.text.trim();
    if (n.isEmpty) return 'A name is needed';
    return n.codeUnits.length > 32 ? 'At most 32 characters' : null;
  }

  String? get _pwError {
    final p = _pw.text;
    if (p.isEmpty) return null;
    if (p.length < 8 || p.length > 63) return '8 to 63 characters';
    return p.codeUnits.any((c) => c < 32 || c > 126) ? 'Plain letters, digits and symbols only' : null;
  }

  @override
  void dispose() {
    _ssid.dispose();
    _pw.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final ok = _ssidError == null && _pwError == null;
    return AlertDialog(
      title: const Text('Hotspot'),
      content: SizedBox(
        width: 420,
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          TextField(
            controller: _ssid,
            decoration: InputDecoration(labelText: 'Name', errorText: _ssidError),
            onChanged: (_) => setState(() {}),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _pw,
            autocorrect: false,
            enableSuggestions: false,
            style: const TextStyle(fontFamily: 'GeistMono'),
            decoration: InputDecoration(
              labelText: 'Password',
              hintText: widget.hasPassword ? 'unchanged' : 'a random one',
              helperText: '8 to 63 characters; empty keeps the current one',
              errorText: _pwError,
            ),
            onChanged: (_) => setState(() {}),
          ),
          const SizedBox(height: 16),
          Text('BAND', style: Arstro.overline),
          const SizedBox(height: 8),
          SegmentedButton<String>(
            showSelectedIcon: false,
            segments: const [
              ButtonSegment(value: '2.4', label: Text('2.4 GHz')),
              ButtonSegment(value: '5', label: Text('5 GHz')),
            ],
            selected: {_band},
            onSelectionChanged: (s) => setState(() => _band = s.first),
          ),
          const SizedBox(height: 6),
          Text('2.4 GHz reaches further and every device knows it; 5 GHz is faster.',
              style: Theme.of(context).textTheme.bodySmall),
        ]),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: ok
              ? () => Navigator.pop(context, {
                    'ssid': _ssid.text.trim(),
                    if (_pw.text.isNotEmpty) 'password': _pw.text,
                    'band': _band,
                  })
              : null,
          child: const Text('Save and start'),
        ),
      ],
    );
  }
}
