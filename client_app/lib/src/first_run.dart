import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'phone_api.dart';
import 'phone_widgets.dart';

/// The first-run wizard of "Share this phone" (PHN-02): welcome → pick features → allow what they
/// need → device manager → start sharing → the access key. Skippable: everything can be changed
/// later on the app's main page.
class FirstRunWizard extends StatefulWidget {
  const FirstRunWizard({super.key});

  /// Shows the wizard once, on the app's first start (Android only).
  static Future<void> maybeShow(BuildContext context) async {
    if (!PhoneApi.supported) return;
    final s = await PhoneApi.instance.refresh();
    if (s == null || !s.firstRun || !context.mounted) return;
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FirstRunWizard()));
  }

  @override
  State<FirstRunWizard> createState() => _FirstRunWizardState();
}

class _FirstRunWizardState extends State<FirstRunWizard> {
  static const _steps = 6;
  final api = PhoneApi.instance;
  int _step = 0;
  final Set<String> _picked = {};
  final _url = TextEditingController();
  final _token = TextEditingController();
  final _name = TextEditingController();
  PhoneKey? _key;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    api.listen();
    api.addListener(_changed);
    api.refresh().then((s) {
      if (s == null || !mounted) return;
      setState(() {
        _picked.addAll(s.features.where((f) => f.on).map((f) => f.id));
        _url.text = s.url.isEmpty ? s.defaultUrl : s.url;
        _name.text = s.name;
      });
    });
  }

  @override
  void dispose() {
    api.removeListener(_changed);
    _url.dispose();
    _token.dispose();
    _name.dispose();
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  Future<void> _finish() async {
    try {
      await api.finishFirstRun();
    } on PlatformException {
      // nothing to do: the wizard shows again next time
    }
    if (mounted) Navigator.of(context).pop();
  }

  Future<void> _next() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      switch (_step) {
        case 1:
          for (final f in api.state?.features ?? const <PhoneFeature>[]) {
            await api.setFeature(f.id, _picked.contains(f.id));
          }
        case 3:
          await api.setRelay(url: _url.text.trim(), token: _token.text.trim(), name: _name.text.trim());
          _token.clear();
        case 4:
          _key = await api.key();
      }
      if (_step == _steps - 1) {
        await _finish();
        return;
      }
      if (mounted) setState(() => _step++);
    } on PlatformException catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message ?? e.code)));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return PopScope(
      canPop: _step == 0,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && _step > 0) setState(() => _step--);
      },
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Share this phone'),
          actions: [if (_step < _steps - 1) TextButton(onPressed: _finish, child: const Text('Skip'))],
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 640),
              child: Column(children: [
                LinearProgressIndicator(value: (_step + 1) / _steps),
                Expanded(
                  child: ListView(padding: const EdgeInsets.fromLTRB(20, 20, 20, 20), children: [
                    Text('Step ${_step + 1} of $_steps',
                        style: theme.textTheme.labelMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                    const SizedBox(height: 6),
                    ..._page(theme),
                  ]),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 8, 20, 16),
                  child: Row(children: [
                    if (_step > 0)
                      TextButton(onPressed: _busy ? null : () => setState(() => _step--), child: const Text('Back')),
                    const Spacer(),
                    FilledButton(
                      onPressed: _busy ? null : _next,
                      child: Text(switch (_step) {
                        0 => 'Set up',
                        5 => 'Done',
                        _ => 'Next',
                      }),
                    ),
                  ]),
                ),
              ]),
            ),
          ),
        ),
      ),
    );
  }

  List<Widget> _page(ThemeData theme) {
    final s = api.state;
    final muted = theme.textTheme.bodyMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant);
    switch (_step) {
      case 0:
        return [
          Text('Reach this phone from anywhere', style: theme.textTheme.headlineSmall),
          const SizedBox(height: 12),
          const Text('Arstro Remote Client can make this phone a device of your own Arstro device manager. '
              'From any browser you pick the phone, give its access key, and see its screen, camera, '
              'messages, files and more - only the features you turn on.'),
          const SizedBox(height: 12),
          Text('Everything goes through an end-to-end encrypted channel: the server in between sees '
              'nothing. Android shows its own camera, microphone and screen-sharing signs while they are used.',
              style: muted),
          const SizedBox(height: 12),
          Text('Not now? Tap Skip - everything here can also be set up later on the app\'s main page.',
              style: muted),
        ];
      case 1:
        final features = s?.features ?? const <PhoneFeature>[];
        return [
          Text('What should be reachable?', style: theme.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('Each one can be turned off later. Android asks for its permissions in the next step.', style: muted),
          const SizedBox(height: 8),
          if (features.isEmpty) const Text('No features in this version of the app.'),
          for (final f in features)
            CheckboxListTile(
              contentPadding: EdgeInsets.zero,
              value: _picked.contains(f.id),
              title: Text(f.title),
              subtitle: featureInfo[f.id] == null ? null : Text(featureInfo[f.id]!),
              onChanged: (v) => setState(() => v == true ? _picked.add(f.id) : _picked.remove(f.id)),
            ),
        ];
      case 2:
        final picked = (s?.features ?? const <PhoneFeature>[]).where((f) => f.on).toList();
        final service = s?.serviceMissing ?? const <PhoneNeed>[];
        return [
          Text('Allow what they need', style: theme.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('Some are system dialogs, some open a settings page: turn Arstro Remote Client on there, then come back.',
              style: muted),
          const SizedBox(height: 12),
          for (final n in service) _needRow(theme, n.title, false, () => allowFor(context, need: n)),
          if (picked.isEmpty) const Text('No features picked.'),
          for (final f in picked)
            _needRow(theme, f.title, f.ready, () => allowFor(context, f: f),
                detail: f.ready ? null : f.missing.map((n) => n.title).join(', ')),
          const SizedBox(height: 8),
          Text('Anything left can be allowed later on the app\'s main page.', style: muted),
        ];
      case 3:
        return [
          Text('Your device manager', style: theme.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('Its address and its join token (the device manager prints it when it starts, or shows '
              'it with "python3 -m arstro_relay admin token").', style: muted),
          const SizedBox(height: 12),
          TextField(
            controller: _url,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(labelText: 'Address'),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _token,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
              labelText: 'Join token',
              helperText: (s?.tokenSet ?? false) ? 'Set. Type a new one to replace it.' : null,
            ),
          ),
          const SizedBox(height: 8),
          TextField(controller: _name, decoration: const InputDecoration(labelText: 'Name in the device list')),
        ];
      case 4:
        return [
          Text('Start sharing', style: theme.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('The phone stays reachable in the background and after a restart; a notification shows it, '
              'with a Stop button.', style: muted),
          const SizedBox(height: 16),
          Row(children: [
            PhoneDot(state: s),
            const SizedBox(width: 10),
            Expanded(child: Text(s?.summary ?? '')),
          ]),
          if (s?.running == true && s?.error != null) ...[
            const SizedBox(height: 6),
            Text(s!.error!, style: TextStyle(color: theme.colorScheme.error)),
          ],
          const SizedBox(height: 16),
          if (s?.running != true)
            FilledButton.icon(
              onPressed: _busy
                  ? null
                  : () async {
                      try {
                        await api.start();
                      } on PlatformException catch (e) {
                        if (mounted) {
                          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message ?? e.code)));
                        }
                      }
                    },
              icon: const Icon(Icons.play_arrow),
              label: const Text('Start sharing'),
            ),
        ];
      default:
        return [
          Text('The access key', style: theme.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('In the device manager, pick "${s?.name ?? 'this phone'}" and give this key. Keep it like a '
              'password; you can see, copy or replace it any time on the app\'s main page.', style: muted),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.5),
              borderRadius: BorderRadius.circular(10),
            ),
            child: SelectableText(_key?.key ?? '', style: const TextStyle(fontFamily: 'monospace')),
          ),
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: OutlinedButton.icon(
              onPressed: () async {
                final k = _key?.key;
                if (k == null) return;
                await Clipboard.setData(ClipboardData(text: k));
                if (mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Access key copied')));
                }
              },
              icon: const Icon(Icons.copy),
              label: const Text('Copy'),
            ),
          ),
        ];
    }
  }

  Widget _needRow(ThemeData theme, String title, bool ok, VoidCallback onAllow, {String? detail}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(children: [
        Icon(ok ? Icons.check_circle : Icons.radio_button_unchecked,
            color: ok ? const Color(0xFF4CAF50) : theme.colorScheme.outline),
        const SizedBox(width: 12),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title),
            if (detail != null && detail.isNotEmpty)
              Text(detail, style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ]),
        ),
        if (!ok) FilledButton.tonal(onPressed: onAllow, child: const Text('Allow')),
      ]),
    );
  }
}
