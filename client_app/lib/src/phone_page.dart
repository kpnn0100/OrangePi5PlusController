import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'first_run.dart';
import 'phone_api.dart';
import 'phone_widgets.dart';
import 'theme.dart';
import 'widgets.dart';

/// The app's page (PHN-01, PHN-02, PHN-19): share the phone with your device manager - the service
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
  String? _storedUrl, _storedName; // what the fields were last filled with
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
    if (s != null) {
      // the stored settings - again when they change elsewhere (the first-run wizard runs over this
      // page), but not over a field that is being edited
      if ((_storedUrl == null || _url.text == _storedUrl) && _url.text != s.url) _url.text = s.url;
      if ((_storedName == null || _name.text == _storedName) && _name.text != s.name) _name.text = s.name;
      _storedUrl = s.url;
      _storedName = s.name;
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
      appBar: AppBar(
        title: const Row(children: [
          ArstroMark(size: 28),
          SizedBox(width: 12),
          Flexible(child: Text('Arstro Remote Client', overflow: TextOverflow.ellipsis)),
        ]),
      ),
      body: !PhoneApi.supported
          ? const Padding(
              padding: EdgeInsets.all(16),
              child: Align(
                alignment: Alignment.topCenter,
                child: EmptyState(icon: Icons.phone_android_outlined, text: 'Only an Android phone can be shared.'),
              ),
            )
          : s == null
              ? const Center(child: SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2)))
              : Center(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 720),
                    child: ListView(
                      padding: const EdgeInsets.fromLTRB(16, 20, 16, 32),
                      children: [
                        _sharing(s),
                        const SizedBox(height: 12),
                        _keyCard(s),
                        const SizedBox(height: 12),
                        _server(s),
                        const SizedBox(height: 12),
                        _features(s),
                        const SizedBox(height: 16),
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

  static const _body = TextStyle(fontSize: 14, color: Arstro.text2, height: 1.5);
  static const _small = TextStyle(fontSize: 13, color: Arstro.muted, height: 1.45);

  /// A hint with its button on the right (what the service still needs, the phone maker's battery rules).
  Widget _hint(String text, String label, VoidCallback onPressed, {Color color = Arstro.muted}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Row(children: [
          Expanded(child: Text(text, style: _small.copyWith(color: color))),
          const SizedBox(width: 12),
          OutlinedButton(style: Arstro.small, onPressed: onPressed, child: Text(label)),
        ]),
      );

  Widget _sharing(PhoneState s) {
    return SectionCard(
      title: 'Share this phone',
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Text('Open this phone from any browser through your Arstro device manager, '
            'end-to-end encrypted with its access key.',
            style: _body),
        const SizedBox(height: 6),
        SwitchRow(
          title: Row(children: [
            PhoneDot(state: s),
            const SizedBox(width: 10),
            Flexible(child: Text(s.summary)),
          ]),
          subtitle: s.running && s.error != null ? Text(s.error!, style: const TextStyle(color: Arstro.err)) : null,
          value: s.enabled || s.running,
          onChanged: _busy ? null : _toggleService,
        ),
        for (final n in s.serviceMissing)
          _hint(
              n.name == 'BATTERY_UNRESTRICTED'
                  ? 'Unrestricted battery use keeps the phone reachable while the screen is off.'
                  : 'Allow notifications to see that the phone is shared (and stop it from there).',
              'Allow',
              () => allowFor(context, need: n),
              color: Arstro.warn),
        const Divider(),
        SwitchRow(
          title: const Text('Stay reachable when the screen is off'),
          subtitle: Text(s.awake
              ? 'The phone stays online while it sleeps. Uses some more battery.'
              : 'Off: the phone saves battery but can go offline while it sleeps.'),
          value: s.awake,
          onChanged: _busy ? null : (on) => _guard(() => api.setAwake(on)),
        ),
        if (s.makerHint != null) _hint(s.makerHint!, 'App info', () => api.openAppInfo()),
        const Divider(),
        KeyValueRow('Name', s.name),
        const Divider(),
        KeyValueRow('Device id', s.id, mono: true, selectable: true),
      ]),
    );
  }

  Widget _keyCard(PhoneState s) {
    return SectionCard(
      title: 'Access key',
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Text('Whoever has this key can use the features you turned on. Keep it like a password.', style: _body),
        const SizedBox(height: 12),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
          decoration: BoxDecoration(
            color: Arstro.surface2,
            border: Border.all(color: Arstro.line2),
            borderRadius: BorderRadius.circular(Arstro.rButton),
          ),
          child: SelectableText(
            _key ?? 'ak1_••••••••••••••••••••  (id ${s.keyId})',
            style: Arstro.monoStyle(size: 13, color: _key == null ? Arstro.text2 : Arstro.text),
          ),
        ),
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, children: [
          OutlinedButton.icon(
            style: Arstro.small,
            onPressed: _showKey,
            icon: Icon(_key == null ? Icons.visibility_outlined : Icons.visibility_off_outlined, size: 16),
            label: Text(_key == null ? 'Show' : 'Hide'),
          ),
          OutlinedButton.icon(
              style: Arstro.small, onPressed: _copyKey, icon: const Icon(Icons.copy_outlined, size: 16), label: const Text('Copy')),
          OutlinedButton.icon(
              style: Arstro.small, onPressed: _replaceKey, icon: const Icon(Icons.autorenew, size: 16), label: const Text('Replace')),
        ]),
      ]),
    );
  }

  Widget _server(PhoneState s) {
    return SectionCard(
      title: 'Device manager',
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const FieldLabel('Address'),
        TextField(
          controller: _url,
          keyboardType: TextInputType.url,
          autocorrect: false,
          decoration: const InputDecoration(hintText: 'https://server/arstro/internal/devices'),
        ),
        const SizedBox(height: 16),
        const FieldLabel('Join token'),
        TextField(
          controller: _token,
          obscureText: true,
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(
            helperText: s.tokenSet ? 'Set. Type a new one to replace it.' : 'Not set: the phone cannot join yet.',
          ),
        ),
        const SizedBox(height: 16),
        const FieldLabel('Name in the device list'),
        TextField(controller: _name),
        const SizedBox(height: 16),
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
      child: s.features.isEmpty
          ? const Text('No features in this version of the app.', style: _body)
          : Column(children: [
              for (final (i, f) in s.features.indexed) ...[
                if (i > 0) const Divider(),
                FeatureTile(feature: f, onChanged: (on) => _toggleFeature(f, on)),
              ],
            ]),
    );
  }
}
