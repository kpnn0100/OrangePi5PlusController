import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'phone_api.dart';
import 'theme.dart';
import 'widgets.dart';

/// Asks Android for everything [f] still needs (or only [need]), explaining the steps Android
/// makes hard first, then tells what is still missing.
Future<void> allowFor(BuildContext context, {PhoneFeature? f, PhoneNeed? need}) async {
  final api = PhoneApi.instance;
  final needs = need != null ? [need] : (f?.missing ?? const <PhoneNeed>[]);
  final sdk = api.state?.sdk ?? 0;
  if (needs.any((n) => n.name == 'ACCESSIBILITY') && sdk >= 33) {
    final go = await showDialog<bool>(context: context, builder: (_) => const _RestrictedDialog());
    if (go != true) return;
  }
  PhoneState? s;
  try {
    s = need != null ? await api.request(name: need.name) : await api.request(id: f!.id);
  } on PlatformException catch (e) {
    if (context.mounted) _snack(context, e.message ?? 'Could not ask Android');
    return;
  }
  if (!context.mounted || s == null) return;
  final still = need != null
      ? [
          for (final x in [...s.serviceMissing, for (final g in s.features) ...g.missing])
            if (x.name == need.name) x
        ].take(1).toList()
      : s.features.firstWhere((g) => g.id == f!.id, orElse: () => f!).missing;
  final denied = still.where((n) => n.kind == 'permission').map((n) => n.title).toList();
  if (denied.isNotEmpty) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text('Android did not allow ${denied.join(', ')}. Allow it in App info → Permissions.'),
      action: SnackBarAction(label: 'App info', onPressed: () => api.openAppInfo()),
    ));
  } else if (still.isNotEmpty) {
    _snack(context, 'Still needed: ${still.map((n) => n.title).join(', ')}');
  }
}

void _snack(BuildContext context, String text) =>
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

class _RestrictedDialog extends StatelessWidget {
  const _RestrictedDialog();

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Remote control needs accessibility'),
      content: const Text(
        'Turn on "Arstro Remote Client - remote control" in the list that opens.\n\n'
        'If Android says "Restricted setting" (apps installed from outside the Play Store):\n'
        '1. Open App info.\n'
        '2. Tap ⋮ at the top right, then "Allow restricted settings".\n'
        '3. Come back here and tap Allow again.',
      ),
      actions: [
        TextButton(
          onPressed: () {
            PhoneApi.instance.openAppInfo();
            Navigator.pop(context, false);
          },
          child: const Text('App info'),
        ),
        FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Open settings')),
      ],
    );
  }
}

/// Online `ok` (with its soft ring), connecting `warn`, an error `err`, not sharing `muted`.
class PhoneDot extends StatelessWidget {
  const PhoneDot({super.key, required this.state});
  final PhoneState? state;

  @override
  Widget build(BuildContext context) {
    final s = state;
    final (color, soft) = s == null || !s.running
        ? (Arstro.muted, null)
        : switch (s.relayState) {
            'online' => (Arstro.ok, Arstro.okSoft),
            'connecting' => (Arstro.warn, Arstro.warnSoft),
            _ => (Arstro.err, Arstro.errSoft),
          };
    return Container(
      width: 8,
      height: 8,
      decoration: BoxDecoration(
        color: color,
        shape: BoxShape.circle,
        boxShadow: soft == null ? null : [BoxShadow(color: soft, spreadRadius: 3)],
      ),
    );
  }
}

/// One feature: its switch, what it does, what it still needs and an Allow button.
class FeatureTile extends StatelessWidget {
  const FeatureTile({super.key, required this.feature, required this.onChanged});
  final PhoneFeature feature;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    final f = feature;
    final info = featureInfo[f.id];
    final needs = f.on && f.missing.isNotEmpty;
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      SwitchRow(
        title: Row(children: [
          Flexible(child: Text(f.title)),
          if (f.on) ...[
            const SizedBox(width: 8),
            Icon(f.ready ? Icons.check_circle_outline : Icons.error_outline,
                size: 16, color: f.ready ? Arstro.ok : Arstro.warn),
          ],
        ]),
        subtitle: info == null ? null : Text(info),
        value: f.on,
        onChanged: onChanged,
      ),
      if (needs)
        Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: Row(children: [
            Expanded(
              child: Text('Needs ${f.missing.map((n) => n.title).join(', ')}',
                  style: const TextStyle(fontSize: 13, color: Arstro.warn, height: 1.45)),
            ),
            const SizedBox(width: 12),
            OutlinedButton(style: Arstro.small, onPressed: () => allowFor(context, f: f), child: const Text('Allow')),
          ]),
        ),
      if (f.on && f.note != null)
        Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: Text(f.note!, style: const TextStyle(fontSize: 13, color: Arstro.muted, height: 1.45)),
        ),
    ]);
  }
}
