import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../widgets.dart';
import 'first_run.dart';
import 'phone_api.dart';
import 'phone_widgets.dart';

/// "This phone" (PHN-01, PHN-02, PHN-19): share the phone with your device manager - the service
/// switch and state, the device's name and id, the access key, the server settings and every
/// feature with its switch and what it still needs.
class PhonePage extends StatefulWidget {
  const PhonePage({super.key});

  @override
  State<PhonePage> createState() => _PhonePageState();
}

class _PhonePageState extends State<PhonePage> {
  final api = PhoneApi.instance;
  final _url = TextEditingController();
  final _token = TextEditingController();
  final _name = TextEditingController();
  bool _filled = false;
  bool _busy = false;
  String? _key; // shown only while "Show" is on

  @override
  void initState() {
    super.initState();
    api.listen();
    api.addListener(_onState);
    api.refresh().then((_) => _onState());
  }

  @override
  void dispose() {
    api.removeListener(_onState);
    _url.dispose();
    _token.dispose();
    _name.dispose();
    super.dispose();
  }

  void _onState() {
    final s = api.state;
    if (s != null && !_filled) {
      _filled = true;
      _url.text = s.url;
      _name.text = s.name;
    }
    if (mounted) setState(() {});
  }

  Future<void> _guard(Future<void> Function() fn) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await fn();
    } on PlatformException catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message ?? e.code)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _toggleService(bool on) => _guard(() async {
        final s = api.state;
        if (on && s != null && !s.tokenSet) {
          ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(content: Text('Set the join token of your device manager first (below).')));
        }
        await (on ? api.start() : api.stop());
      });

  Future<void> _toggleFeature(PhoneFeature f, bool on) async {
    await _guard(() async {
      await api.setFeature(f.id, on);
    });
    if (!on || !mounted) return;
    final now = api.state?.features.firstWhere((g) => g.id == f.id, orElse: () => f);
    if (now != null && now.missing.isNotEmpty) await allowFor(context, f: now);
  }

  Future<void> _saveRelay() => _guard(() async {
        await api.setRelay(url: _url.text.trim(), token: _token.text.trim(), name: _name.text.trim());
        _token.clear();
        if (mounted) {
          FocusScope.of(context).unfocus();
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Saved')));
        }
      });

  Future<void> _showKey() => _guard(() async {
        if (_key != null) {
          setState(() => _key = null);
          return;
        }
        final k = await api.key();
        if (mounted) setState(() => _key = k.key);
      });

  Future<void> _copyKey() => _guard(() async {
        final k = await api.key();
        await Clipboard.setData(ClipboardData(text: k.key));
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Access key copied')));
        }
      });

  Future<void> _replaceKey() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Replace the access key?'),
        content: const Text('Everyone connected with the old key is disconnected and needs the new key '
            'to open this phone again.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Replace')),
        ],
      ),
    );
    if (ok != true) return;
    await _guard(() async {
      final k = await api.rotateKey();
      if (_key != null && mounted) setState(() => _key = k.key);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('New access key made')));
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final s = api.state;
    return Scaffold(
      appBar: AppBar(title: const Text('This phone')),
      body: !PhoneApi.supported
          ? const EmptyState(icon: Icons.phone_android, text: 'Only an Android phone can be shared.')
          : s == null
              ? const Center(child: CircularProgressIndicator())
              : Center(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 720),
                    child: ListView(
                      padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
                      children: [
                        _sharing(s),
                        const SizedBox(height: 12),
                        _keyCard(s),
                        const SizedBox(height: 12),
                        _server(s),
                        const SizedBox(height: 12),
                        _features(s),
                        const SizedBox(height: 12),
                        Center(
                          child: TextButton.icon(
                            onPressed: () => Navigator.of(context)
                                .push(MaterialPageRoute(builder: (_) => const FirstRunWizard())),
                            icon: const Icon(Icons.auto_fix_high_outlined),
                            label: const Text('Run the setup again'),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
    );
  }

  Widget _sharing(PhoneState s) {
    final theme = Theme.of(context);
    return SectionCard(
      title: 'Share this phone',
      icon: Icons.phone_android,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('Open this phone from any browser through your Arstro device manager, '
            'end-to-end encrypted with its access key.',
            style: theme.textTheme.bodyMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          title: Row(children: [
            PhoneDot(state: s),
            const SizedBox(width: 10),
            Flexible(child: Text(s.summary)),
          ]),
          subtitle: s.running && s.error != null
              ? Text(s.error!, style: TextStyle(color: theme.colorScheme.error))
              : null,
          value: s.enabled || s.running,
          onChanged: _busy ? null : _toggleService,
        ),
        for (final n in s.serviceMissing)
          Row(children: [
            Expanded(
              child: Text(
                  n.name == 'BATTERY_UNRESTRICTED'
                      ? 'Unrestricted battery use keeps the phone reachable while the screen is off.'
                      : 'Allow notifications to see that the phone is shared (and stop it from there).',
                  style: theme.textTheme.bodySmall),
            ),
            const SizedBox(width: 8),
            FilledButton.tonal(onPressed: () => allowFor(context, need: n), child: const Text('Allow')),
          ]),
        const SizedBox(height: 8),
        KeyValueRow('Name', s.name),
        KeyValueRow('Device id', s.id, mono: true, selectable: true),
      ]),
    );
  }

  Widget _keyCard(PhoneState s) {
    final theme = Theme.of(context);
    return SectionCard(
      title: 'Access key',
      icon: Icons.key_outlined,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('Whoever has this key can use the features you turned on. Keep it like a password.',
            style: theme.textTheme.bodyMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        const SizedBox(height: 10),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.5),
            borderRadius: BorderRadius.circular(10),
          ),
          child: SelectableText(
            _key ?? 'ak1_••••••••••••••••••••  (id ${s.keyId})',
            style: theme.textTheme.bodyMedium?.copyWith(fontFamily: 'monospace'),
          ),
        ),
        const SizedBox(height: 8),
        Wrap(spacing: 8, runSpacing: 4, children: [
          OutlinedButton.icon(
            onPressed: _showKey,
            icon: Icon(_key == null ? Icons.visibility_outlined : Icons.visibility_off_outlined),
            label: Text(_key == null ? 'Show' : 'Hide'),
          ),
          OutlinedButton.icon(onPressed: _copyKey, icon: const Icon(Icons.copy), label: const Text('Copy')),
          OutlinedButton.icon(onPressed: _replaceKey, icon: const Icon(Icons.autorenew), label: const Text('Replace')),
        ]),
      ]),
    );
  }

  Widget _server(PhoneState s) {
    return SectionCard(
      title: 'Device manager',
      icon: Icons.dns_outlined,
      child: Column(children: [
        TextField(
          controller: _url,
          keyboardType: TextInputType.url,
          autocorrect: false,
          decoration: const InputDecoration(labelText: 'Address', hintText: 'https://server/arstro/internal/devices'),
        ),
        const SizedBox(height: 8),
        TextField(
          controller: _token,
          obscureText: true,
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(
            labelText: 'Join token',
            helperText: s.tokenSet ? 'Set. Type a new one to replace it.' : 'Not set: the phone cannot join yet.',
          ),
        ),
        const SizedBox(height: 8),
        TextField(
          controller: _name,
          decoration: const InputDecoration(labelText: 'Name in the device list'),
        ),
        const SizedBox(height: 12),
        Align(
          alignment: Alignment.centerRight,
          child: FilledButton(onPressed: _busy ? null : _saveRelay, child: const Text('Save')),
        ),
      ]),
    );
  }

  Widget _features(PhoneState s) {
    return SectionCard(
      title: 'Features',
      icon: Icons.tune,
      child: s.features.isEmpty
          ? const Text('No features in this version of the app.')
          : Column(children: [
              for (final f in s.features) FeatureTile(feature: f, onChanged: (on) => _toggleFeature(f, on)),
            ]),
    );
  }
}
