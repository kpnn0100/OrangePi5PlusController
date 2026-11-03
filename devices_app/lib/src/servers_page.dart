import 'package:flutter/material.dart';

import 'api.dart';
import 'theme.dart';
import 'widgets.dart';

/// Add or edit a device manager (MGR-01): the user types its address (just the domain is enough),
/// the app finds the device manager there and asks for its viewer password only when it has one.
class ServerPage extends StatefulWidget {
  const ServerPage({super.key, required this.api, this.server, this.firstRun = false});
  final DevicesApi api;
  final Server? server;
  final bool firstRun;

  @override
  State<ServerPage> createState() => _ServerPageState();
}

class _ServerPageState extends State<ServerPage> {
  late final _address = TextEditingController(text: widget.server?.url ?? '');
  final _password = TextEditingController();
  bool _needPassword = false;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _needPassword = widget.server?.hasPassword ?? false;
  }

  @override
  void dispose() {
    _address.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _connect() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final pw = _password.text.isEmpty ? null : _password.text;
    try {
      final p = await widget.api.probe(_address.text, password: pw);
      // an edit with an empty password field keeps the saved one
      await widget.api.saveServer(id: widget.server?.id, url: p.url, name: p.name, version: p.version, password: pw);
      if (mounted) Navigator.of(context).pop(true);
    } on ApiError catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.message;
        if (e.code == 'password') _needPassword = true;
      });
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _remove() async {
    final s = widget.server!;
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Remove this device manager?'),
        content: Text('${s.label} and the access keys saved for its devices are removed from this phone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Remove')),
        ],
      ),
    );
    if (ok != true) return;
    await widget.api.removeServer(s.id);
    if (mounted) Navigator.of(context).pop(true);
  }

  @override
  Widget build(BuildContext context) {
    final editing = widget.server != null;
    return Scaffold(
      appBar: AppBar(
        title: Text(editing ? 'Device manager' : 'Add a device manager'),
        automaticallyImplyLeading: !widget.firstRun,
        actions: [
          if (editing)
            IconButton(
              tooltip: 'Remove',
              icon: const Icon(Icons.delete_outline),
              color: Arstro.err,
              onPressed: _busy ? null : _remove,
            ),
          const SizedBox(width: 4),
        ],
      ),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 28, 16, 24), children: [
          if (widget.firstRun) ...[
            const Align(alignment: Alignment.centerLeft, child: ArstroMark(size: 40)),
            const SizedBox(height: 20),
            Text('Arstro Devices', style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 8),
            const Text('Every machine and phone that connects to your device manager shows up here. '
                'Open one and you get its own pages - screen, terminal, files - end-to-end encrypted.',
                style: TextStyle(fontSize: 14.5, color: Arstro.text2, height: 1.55)),
            const SizedBox(height: 32),
          ],
          const FieldLabel('Device manager address'),
          TextField(
            key: const Key('address'),
            controller: _address,
            autofocus: !editing,
            keyboardType: TextInputType.url,
            autocorrect: false,
            enableSuggestions: false,
            textInputAction: _needPassword ? TextInputAction.next : TextInputAction.go,
            onSubmitted: (_) => _needPassword ? null : _connect(),
            decoration: const InputDecoration(
              hintText: 'arstro.example.com',
              helperText: 'The domain is enough; a full link works too.',
            ),
          ),
          if (_needPassword) ...[
            const SizedBox(height: 18),
            const FieldLabel('Viewer password'),
            TextField(
              key: const Key('password'),
              controller: _password,
              obscureText: true,
              autocorrect: false,
              enableSuggestions: false,
              textInputAction: TextInputAction.go,
              onSubmitted: (_) => _connect(),
              decoration: InputDecoration(
                helperText: editing && widget.server!.hasPassword ? 'Leave it empty to keep the saved one.' : null,
              ),
            ),
          ],
          if (_error != null) ...[
            const SizedBox(height: 14),
            Text(_error!, key: const Key('server-error'), style: const TextStyle(color: Arstro.err, fontSize: 13.5)),
          ],
          const SizedBox(height: 24),
          FilledButton(
            key: const Key('connect'),
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            onPressed: _busy ? null : _connect,
            child: _busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : Text(editing ? 'Save' : 'Connect'),
          ),
        ]),
      ),
    );
  }
}

/// The device managers in a sheet: switch, edit, add (MGR-01).
Future<bool> showServersSheet(BuildContext context, DevicesApi api, ServerList list) async {
  final changed = await showModalBottomSheet<bool>(
    context: context,
    builder: (c) => SafeArea(
      child: ListView(shrinkWrap: true, padding: const EdgeInsets.fromLTRB(16, 0, 16, 16), children: [
        const Padding(
          padding: EdgeInsets.fromLTRB(2, 0, 2, 14),
          child: Text('Device managers', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w600, letterSpacing: -0.2)),
        ),
        RowGroup(children: [
          for (final s in list.servers)
            InkWell(
              onTap: () async {
                await api.select(s.id);
                if (c.mounted) Navigator.pop(c, true);
              },
              child: Padding(
                padding: const EdgeInsets.fromLTRB(14, 10, 4, 10),
                child: Row(children: [
                  SizedBox(
                    width: 20,
                    child: s.id == list.active?.id ? const Icon(Icons.check, size: 18, color: Arstro.text) : null,
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(s.label, maxLines: 1, overflow: TextOverflow.ellipsis,
                          style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w500, height: 1.35)),
                      Text.rich(
                        TextSpan(children: [
                          if (s.name.isNotEmpty) TextSpan(text: s.name),
                          if (s.version.isNotEmpty) ...[
                            if (s.name.isNotEmpty) const TextSpan(text: ' · '),
                            TextSpan(text: 'v${s.version}', style: Arstro.monoStyle(size: 11.5, color: Arstro.muted)),
                          ],
                          if (s.hasPassword) TextSpan(text: '${s.name.isEmpty && s.version.isEmpty ? '' : ' · '}password'),
                        ]),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: Arstro.meta,
                      ),
                    ]),
                  ),
                  IconButton(
                    tooltip: 'Edit',
                    icon: const Icon(Icons.edit_outlined, size: 18),
                    onPressed: () async {
                      final r = await Navigator.of(c).push<bool>(MaterialPageRoute(builder: (_) => ServerPage(api: api, server: s)));
                      if (r == true && c.mounted) Navigator.pop(c, true);
                    },
                  ),
                ]),
              ),
            ),
        ]),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(44)),
          icon: const Icon(Icons.add),
          label: const Text('Add a device manager'),
          onPressed: () async {
            final r = await Navigator.of(c).push<bool>(MaterialPageRoute(builder: (_) => ServerPage(api: api)));
            if (r == true && c.mounted) Navigator.pop(c, true);
          },
        ),
      ]),
    ),
  );
  return changed ?? false;
}
