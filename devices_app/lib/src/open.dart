import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'theme.dart';

/// Where the owner finds a device's access key (MGR-02), by kind.
String keyHint(Device d) => switch (d.kind) {
      DeviceKind.backup => 'On that machine run: arstro-backup key',
      DeviceKind.phone => 'In Arstro Remote Client on that phone: the access key on its main page.',
      DeviceKind.computer => 'On that machine run: arstro-remote relay key (for a slot: arstro-remote-a / -b), '
          'or open its web page › Internet access.',
    };

/// Asks for a device's access key and saves it; when the device is online the key is checked at
/// once and a wrong one is not kept. Returns true when a key is saved.
Future<bool> showKeySheet(BuildContext context, DevicesApi api, String server, Device device) async {
  final saved = await showModalBottomSheet<bool>(
    context: context,
    isScrollControlled: true,
    builder: (c) => Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.of(c).viewInsets.bottom),
      child: _KeySheet(api: api, server: server, device: device),
    ),
  );
  return saved ?? false;
}

class _KeySheet extends StatefulWidget {
  const _KeySheet({required this.api, required this.server, required this.device});
  final DevicesApi api;
  final String server;
  final Device device;

  @override
  State<_KeySheet> createState() => _KeySheetState();
}

class _KeySheetState extends State<_KeySheet> {
  final _key = TextEditingController();
  bool _show = false;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _key.dispose();
    super.dispose();
  }

  Future<void> _paste() async {
    final d = await Clipboard.getData(Clipboard.kTextPlain);
    if (d?.text != null) setState(() => _key.text = d!.text!.trim());
  }

  Future<void> _save() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await widget.api.setKey(widget.server, widget.device.id, _key.text);
      if (widget.device.online) {
        try {
          await widget.api.check(widget.server, widget.device.id);
        } on ApiError catch (e) {
          if (e.code == 'wrong_key') {
            await widget.api.forgetKey(widget.server, widget.device.id);
            throw ApiError('wrong_key', 'That key is not the key of ${widget.device.name}. Check you copied the right one.');
          }
          // offline now, busy, ...: keep the key, it is checked when the device is opened
        }
      }
      if (mounted) Navigator.of(context).pop(true);
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Text('Access key of ${widget.device.name}',
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600, letterSpacing: -0.2, height: 1.3)),
          const SizedBox(height: 6),
          Text(keyHint(widget.device), style: const TextStyle(fontSize: 13.5, color: Arstro.text2, height: 1.5)),
          const SizedBox(height: 18),
          TextField(
            key: const Key('key'),
            controller: _key,
            autofocus: true,
            obscureText: !_show,
            autocorrect: false,
            enableSuggestions: false,
            style: Arstro.monoStyle(size: 14),
            onSubmitted: (_) => _save(),
            decoration: InputDecoration(
              hintText: 'ak1_…',
              hintStyle: Arstro.monoStyle(size: 14, color: Arstro.muted),
              suffixIcon: Row(mainAxisSize: MainAxisSize.min, children: [
                IconButton(
                  tooltip: _show ? 'Hide' : 'Show',
                  icon: Icon(_show ? Icons.visibility_off_outlined : Icons.visibility_outlined, size: 20),
                  onPressed: () => setState(() => _show = !_show),
                ),
                IconButton(tooltip: 'Paste', icon: const Icon(Icons.content_paste_outlined, size: 20), onPressed: _paste),
              ]),
            ),
          ),
          if (_error != null) ...[
            const SizedBox(height: 10),
            Text(_error!, key: const Key('key-error'), style: const TextStyle(color: Arstro.err, fontSize: 13.5)),
          ],
          const SizedBox(height: 10),
          const Text('It stays on this phone, encrypted; the device manager never sees it.', style: Arstro.meta),
          const SizedBox(height: 18),
          FilledButton(
            key: const Key('save-key'),
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            onPressed: _busy ? null : _save,
            child: _busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Save key'),
          ),
        ]),
      ),
    );
  }
}

/// Opens one port of a device (MGR-04): asks for the key first when there is none, shows
/// "Connecting" while the app checks the port answers (MGR-05 remembers it then), and explains a
/// refusal - with "Replace key" when the key is wrong. Returns true when the page was opened.
Future<bool> openPort(BuildContext context, DevicesApi api, String server, Device device, int port) async {
  final messenger = ScaffoldMessenger.of(context);
  if (!device.hasKey) {
    if (!await showKeySheet(context, api, server, device)) return false;
    if (!context.mounted) return false;
  }
  final nav = Navigator.of(context, rootNavigator: true);
  var shown = true;
  showDialog<void>(
    context: context,
    barrierDismissible: false,
    builder: (c) => PopScope(
      canPop: false,
      child: AlertDialog(
        content: Row(children: [
          const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)),
          const SizedBox(width: 16),
          Expanded(
            child: Text('Opening ${device.name}, port $port…',
                style: const TextStyle(fontSize: 14.5, color: Arstro.text, height: 1.45)),
          ),
        ]),
      ),
    ),
  ).whenComplete(() => shown = false);
  try {
    await api.open(server, device.id, device.name, port);
    return true;
  } on ApiError catch (e) {
    if (shown) {
      nav.pop();
      shown = false;
    }
    messenger.showSnackBar(SnackBar(
      content: Text(e.message),
      duration: const Duration(seconds: 6),
      action: e.code == 'wrong_key' || e.code == 'no_key'
          ? SnackBarAction(label: 'Replace key', onPressed: () {
              if (context.mounted) showKeySheet(context, api, server, device);
            })
          : null,
    ));
    return false;
  } finally {
    if (shown) nav.pop();
  }
}
