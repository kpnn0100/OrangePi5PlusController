import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'phone_api.dart';
import 'phone_widgets.dart';
import 'theme.dart';
import 'widgets.dart';

/// The first-run wizard of "Share this phone" (PHN-02): welcome → pick features → allow what they
/// need → start sharing → how to open it (address and password). Skippable: everything (also a device
/// manager) can be changed later on the app's main page.
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
  static const _steps = 5;
  final api = PhoneApi.instance;
  int _step = 0;
  final Set<String> _picked = {};
  String? _password;
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
      });
    });
  }

  @override
  void dispose() {
    api.removeListener(_changed);
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
          if (api.state?.running != true) await api.start();
          _password = await api.webPassword();
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
                  child: ListView(padding: const EdgeInsets.fromLTRB(16, 24, 16, 24), children: [
                    Overline('Step ${_step + 1} of $_steps'),
                    const SizedBox(height: 10),
                    ..._page(theme),
                  ]),
                ),
                const Divider(),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
                  child: Row(children: [
                    if (_step > 0)
                      TextButton(onPressed: _busy ? null : () => setState(() => _step--), child: const Text('Back')),
                    const Spacer(),
                    FilledButton(
                      onPressed: _busy ? null : _next,
                      child: Text(switch (_step) {
                        0 => 'Set up',
                        3 => api.state?.running == true ? 'Next' : 'Start sharing',
                        4 => 'Done',
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
    const muted = TextStyle(fontSize: 14, color: Arstro.text2, height: 1.5);
    switch (_step) {
      case 0:
        return [
          Text('Manage this phone from a browser', style: theme.textTheme.headlineSmall),
          const SizedBox(height: 12),
          const Text('Arstro Remote Client runs a small web server on this phone (port 8080, like an Arstro '
              'machine). From a browser on the same Wi-Fi you log in with a password and check the front '
              'and back camera, ring and find the phone, see its screen, messages, files and more - only '
              'the features you turn on.',
              style: TextStyle(fontSize: 15, height: 1.55)),
          const SizedBox(height: 12),
          Text('Android shows its own camera, microphone and screen-sharing signs while they are used. '
              'To reach the phone from anywhere, add your Arstro device manager later on the main page.',
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
          Text('Start sharing', style: theme.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('The web server keeps running in the background and after a restart; a notification shows it, '
              'with a Stop button.', style: muted),
          const SizedBox(height: 16),
          Row(children: [
            PhoneDot(state: s),
            const SizedBox(width: 10),
            Expanded(child: Text(s?.summary ?? '')),
          ]),
        ];
      default:
        final w = s?.web;
        final a = w?.best;
        return [
          Text('Open it in a browser', style: theme.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('On a computer or another phone on the same Wi-Fi (or on this phone\'s hotspot), open this '
              'address and log in with the password. Both are on the app\'s main page too.', style: muted),
          const SizedBox(height: 16),
          const FieldLabel('Address'),
          _box(a == null || w == null ? 'Join a Wi-Fi network first' : w.url(a), key: const Key('wizard-url')),
          if (a != null && w != null && w.https) ...[
            const SizedBox(height: 6),
            Text('For live video and calls: ${w.url(a, secure: true)} (accept the certificate warning once).',
                style: const TextStyle(fontSize: 13, color: Arstro.muted, height: 1.45)),
          ],
          const SizedBox(height: 16),
          const FieldLabel('Password'),
          _box(_password ?? '', key: const Key('wizard-password')),
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: OutlinedButton.icon(
              style: Arstro.small,
              onPressed: () async {
                final p = _password;
                if (p == null) return;
                await Clipboard.setData(ClipboardData(text: p));
                if (mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Password copied')));
                }
              },
              icon: const Icon(Icons.copy_outlined, size: 16),
              label: const Text('Copy password'),
            ),
          ),
        ];
    }
  }

  Widget _box(String text, {Key? key}) => Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
        decoration: BoxDecoration(
          color: Arstro.surface2,
          border: Border.all(color: Arstro.line2),
          borderRadius: BorderRadius.circular(Arstro.rButton),
        ),
        child: SelectableText(text, key: key, style: Arstro.monoStyle(size: 14)),
      );

  Widget _needRow(ThemeData theme, String title, bool ok, VoidCallback onAllow, {String? detail}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(children: [
        Icon(ok ? Icons.check_circle_outline : Icons.radio_button_unchecked, size: 20, color: ok ? Arstro.ok : Arstro.line2),
        const SizedBox(width: 12),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w500, height: 1.35)),
            if (detail != null && detail.isNotEmpty)
              Text(detail, style: const TextStyle(fontSize: 13, color: Arstro.muted, height: 1.45)),
          ]),
        ),
        if (!ok) OutlinedButton(style: Arstro.small, onPressed: onAllow, child: const Text('Allow')),
      ]),
    );
  }
}
