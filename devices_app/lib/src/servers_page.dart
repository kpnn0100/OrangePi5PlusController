import 'package:flutter/material.dart';

import 'api.dart';

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
    final theme = Theme.of(context);
    final editing = widget.server != null;
    return Scaffold(
      appBar: AppBar(
        title: Text(editing ? 'Device manager' : 'Add a device manager'),
        automaticallyImplyLeading: !widget.firstRun,
        actions: [
          if (editing) IconButton(tooltip: 'Remove', icon: const Icon(Icons.delete_outline), onPressed: _busy ? null : _remove),
        ],
      ),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.fromLTRB(20, 8, 20, 24), children: [
          if (widget.firstRun) ...[
            Row(children: [
              Icon(Icons.devices_other, size: 40, color: theme.colorScheme.primary),
              const SizedBox(width: 14),
              Expanded(child: Text('Arstro Devices', style: theme.textTheme.headlineSmall)),
            ]),
            const SizedBox(height: 12),
            Text('Every machine and phone that connects to your device manager shows up here. '
                'Open one and you get its own pages - screen, terminal, files - end-to-end encrypted.',
                style: theme.textTheme.bodyMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const SizedBox(height: 24),
          ],
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
              labelText: 'Device manager address',
              hintText: 'arstro.example.com',
              helperText: 'The domain is enough; a full link works too.',
              border: OutlineInputBorder(),
              prefixIcon: Icon(Icons.dns_outlined),
            ),
          ),
          if (_needPassword) ...[
            const SizedBox(height: 16),
            TextField(
              key: const Key('password'),
              controller: _password,
              obscureText: true,
              autocorrect: false,
              enableSuggestions: false,
              textInputAction: TextInputAction.go,
              onSubmitted: (_) => _connect(),
              decoration: InputDecoration(
                labelText: 'Viewer password',
                helperText: editing && widget.server!.hasPassword ? 'Leave it empty to keep the saved one.' : null,
                border: const OutlineInputBorder(),
                prefixIcon: const Icon(Icons.lock_outline),
              ),
            ),
          ],
          if (_error != null) ...[
            const SizedBox(height: 14),
            Text(_error!, key: const Key('server-error'), style: TextStyle(color: theme.colorScheme.error)),
          ],
          const SizedBox(height: 20),
          FilledButton.icon(
            key: const Key('connect'),
            onPressed: _busy ? null : _connect,
            icon: _busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.link),
            label: Text(editing ? 'Save' : 'Connect'),
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
    showDragHandle: true,
    builder: (c) => SafeArea(
      child: ListView(shrinkWrap: true, children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
          child: Text('Device managers', style: Theme.of(c).textTheme.titleMedium),
        ),
        for (final s in list.servers)
          ListTile(
            leading: Icon(s.id == list.active?.id ? Icons.radio_button_checked : Icons.radio_button_unchecked,
                color: s.id == list.active?.id ? Theme.of(c).colorScheme.primary : null),
            title: Text(s.label),
            subtitle: Text([if (s.name.isNotEmpty) s.name, if (s.version.isNotEmpty) 'v${s.version}', if (s.hasPassword) 'password']
                .join(' · ')),
            trailing: IconButton(
              tooltip: 'Edit',
              icon: const Icon(Icons.edit_outlined),
              onPressed: () async {
                final r = await Navigator.of(c).push<bool>(MaterialPageRoute(builder: (_) => ServerPage(api: api, server: s)));
                if (r == true && c.mounted) Navigator.pop(c, true);
              },
            ),
            onTap: () async {
              await api.select(s.id);
              if (c.mounted) Navigator.pop(c, true);
            },
          ),
        ListTile(
          leading: const Icon(Icons.add),
          title: const Text('Add a device manager'),
          onTap: () async {
            final r = await Navigator.of(c).push<bool>(MaterialPageRoute(builder: (_) => ServerPage(api: api)));
            if (r == true && c.mounted) Navigator.pop(c, true);
          },
        ),
        const SizedBox(height: 8),
      ]),
    ),
  );
  return changed ?? false;
}
