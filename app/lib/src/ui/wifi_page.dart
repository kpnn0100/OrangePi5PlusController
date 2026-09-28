import 'package:flutter/material.dart';

import '../app_scope.dart';
import '../remote_client.dart';
import 'format.dart';
import 'widgets.dart';

class WifiPage extends StatefulWidget {
  const WifiPage({super.key});

  @override
  State<WifiPage> createState() => _WifiPageState();
}

class _WifiPageState extends State<WifiPage> {
  Map<String, dynamic>? _status;
  List<Map<String, dynamic>> _networks = [];
  List<Map<String, dynamic>> _saved = [];
  bool _scanning = false;
  bool _loading = false;
  String? _connecting; // SSID being connected
  String? _error;
  RemoteClient? _client;
  LinkState? _lastState;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final c = AppScope.of(context).client;
    if (c != _client) {
      _client?.removeListener(_onClient);
      _client = c..addListener(_onClient);
      _lastState = c.state;
      if (c.isConnected) _load(rescan: false);
    }
  }

  @override
  void dispose() {
    _client?.removeListener(_onClient);
    super.dispose();
  }

  void _onClient() {
    final s = _client!.state;
    if (s == LinkState.connected && _lastState != LinkState.connected) _load(rescan: false);
    _lastState = s;
  }

  Future<void> _load({bool rescan = true}) async {
    final c = _client!;
    if (!c.isConnected) return;
    setState(() {
      _loading = true;
      _scanning = rescan;
      _error = null;
    });
    try {
      final results = await Future.wait([
        c.request('wifi.status'),
        c.request('wifi.scan', {'rescan': rescan}, const Duration(seconds: 60)),
        c.request('wifi.saved'),
      ]);
      if (!mounted) return;
      setState(() {
        _status = Map<String, dynamic>.from(results[0] as Map);
        _networks = ((results[1] as Map)['networks'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
        _saved = ((results[2] as Map)['networks'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      if (mounted) {
        setState(() {
          _loading = false;
          _scanning = false;
        });
      }
    }
  }

  void _toast(String msg, {bool error = false}) {
    if (!mounted) return;
    final scheme = Theme.of(context).colorScheme;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: error ? scheme.error : null,
      duration: Duration(seconds: error ? 6 : 3),
    ));
  }

  Future<void> _connect(String ssid, {String? password, bool hidden = false}) async {
    setState(() => _connecting = ssid);
    try {
      final r = await _client!.request('wifi.connect', {
        'ssid': ssid,
        if (password != null && password.isNotEmpty) 'password': password,
        if (hidden) 'hidden': true,
      }, const Duration(seconds: 100));
      _toast((r as Map)['message']?.toString() ?? 'Connected to $ssid');
    } catch (e) {
      _toast('Could not connect to $ssid: $e', error: true);
    } finally {
      if (mounted) setState(() => _connecting = null);
      _load(rescan: false);
    }
  }

  Future<void> _simple(String op, String okMsg, [Map<String, dynamic>? params]) async {
    try {
      final r = await _client!.request(op, params, const Duration(seconds: 30));
      _toast((r is Map ? r['message']?.toString() : null) ?? okMsg);
    } catch (e) {
      _toast('$e', error: true);
    }
    _load(rescan: false);
  }

  Future<void> _onTapNetwork(Map<String, dynamic> n) async {
    final ssid = n['ssid'] as String;
    if (n['in_use'] == true) {
      final action = await showModalBottomSheet<String>(
        context: context,
        showDragHandle: true,
        builder: (ctx) => SafeArea(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            ListTile(
              leading: Icon(signalIcon(n['signal'] as int?)),
              title: Text(ssid),
              subtitle: Text('Connected · ${n['signal']}% · ${n['security']} · ${n['freq']}'),
            ),
            ListTile(
              leading: const Icon(Icons.link_off),
              title: const Text('Disconnect'),
              onTap: () => Navigator.pop(ctx, 'disconnect'),
            ),
            ListTile(
              leading: const Icon(Icons.delete_outline),
              title: const Text('Forget network'),
              onTap: () => Navigator.pop(ctx, 'forget'),
            ),
          ]),
        ),
      );
      if (action == 'disconnect' && await _confirmDrop('Disconnect the Pi from "$ssid"?')) {
        _simple('wifi.disconnect', 'Disconnected');
      } else if (action == 'forget') {
        final p = _saved.where((s) => s['ssid'] == ssid).toList();
        if (p.isNotEmpty && await _confirmDrop('Forget "$ssid"? The Pi will disconnect from it.')) {
          _simple('wifi.forget', 'Forgot $ssid', {'uuid': p.first['uuid']});
        }
      }
      return;
    }
    final secured = (n['security'] as String? ?? '').isNotEmpty;
    if (n['saved'] == true || !secured) {
      _connect(ssid);
      return;
    }
    final result = await showDialog<_Credentials>(
      context: context,
      builder: (_) => _PasswordDialog(ssid: ssid, security: n['security'] as String? ?? ''),
    );
    if (result != null) _connect(ssid, password: result.password);
  }

  Future<bool> _confirmDrop(String text) async {
    return await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: const Text('Are you sure?'),
            content: Text('$text\n\nThe Bluetooth remote keeps working, so you can reconnect from here.'),
            actions: [
              TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
              FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Continue')),
            ],
          ),
        ) ??
        false;
  }

  Future<void> _addHidden() async {
    final result = await showDialog<_Credentials>(context: context, builder: (_) => const _PasswordDialog());
    if (result != null) _connect(result.ssid!, password: result.password, hidden: true);
  }

  Future<void> _toggleRadio(bool on) async {
    if (!on && !await _confirmDrop('Turn Wi-Fi off on the Pi?')) return;
    await _simple('wifi.radio', on ? 'Wi-Fi on' : 'Wi-Fi off', {'enabled': on});
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final st = _status;
    if (st == null) {
      return _error != null
          ? EmptyState(
              icon: Icons.wifi_off,
              text: 'Could not read Wi-Fi status\n$_error',
              action: FilledButton(onPressed: () => _load(rescan: false), child: const Text('Try again')),
            )
          : const EmptyState(icon: Icons.wifi_find, text: 'Loading Wi-Fi status…');
    }
    final enabled = st['enabled'] == true;
    return RefreshIndicator(
      onRefresh: () => _load(),
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 900),
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                _StatusCard(status: st, onToggle: _toggleRadio, connecting: _connecting),
                const SizedBox(height: 12),
                SectionCard(
                  title: 'Available networks',
                  icon: Icons.wifi_find,
                  trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                    TextButton.icon(
                      onPressed: enabled ? _addHidden : null,
                      icon: const Icon(Icons.add),
                      label: const Text('Hidden'),
                    ),
                    const SizedBox(width: 4),
                    FilledButton.tonalIcon(
                      onPressed: _loading || !enabled ? null : () => _load(),
                      icon: _scanning
                          ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                          : const Icon(Icons.refresh),
                      label: Text(_scanning ? 'Scanning' : 'Scan'),
                    ),
                  ]),
                  padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
                  child: Column(children: [
                    if (!enabled)
                      const Padding(padding: EdgeInsets.all(12), child: Text('Wi-Fi is turned off on the Pi.')),
                    if (enabled && _networks.isEmpty && !_loading)
                      const Padding(padding: EdgeInsets.all(12), child: Text('No networks found. Tap Scan.')),
                    for (final n in _networks)
                      _NetworkTile(
                        n: n,
                        busy: _connecting == n['ssid'],
                        disabled: _connecting != null,
                        onTap: () => _onTapNetwork(n),
                      ),
                  ]),
                ),
                const SizedBox(height: 12),
                SectionCard(
                  title: 'Saved networks',
                  icon: Icons.bookmark_outline,
                  padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
                  child: Column(children: [
                    if (_saved.isEmpty) const Padding(padding: EdgeInsets.all(12), child: Text('None')),
                    for (final n in _saved)
                      ListTile(
                        contentPadding: const EdgeInsets.only(left: 4),
                        leading: Icon(n['active'] == true ? Icons.wifi : Icons.bookmark_border,
                            color: n['active'] == true ? theme.colorScheme.primary : null),
                        title: Text(n['ssid']?.toString() ?? n['name'].toString()),
                        subtitle: Text([
                          if (n['active'] == true) 'Active',
                          n['autoconnect'] == true ? 'Auto-connect' : 'Manual',
                        ].join(' · ')),
                        trailing: PopupMenuButton<String>(
                          onSelected: (v) async {
                            if (v == 'connect') {
                              _connect(n['ssid'] as String);
                            } else if (v == 'forget' &&
                                (n['active'] != true || await _confirmDrop('Forget the active network?'))) {
                              _simple('wifi.forget', 'Forgot ${n['ssid']}', {'uuid': n['uuid']});
                            }
                          },
                          itemBuilder: (_) => [
                            if (n['active'] != true) const PopupMenuItem(value: 'connect', child: Text('Connect')),
                            const PopupMenuItem(value: 'forget', child: Text('Forget')),
                          ],
                        ),
                      ),
                  ]),
                ),
              ]),
            ),
          ),
        ],
      ),
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.status, required this.onToggle, this.connecting});
  final Map<String, dynamic> status;
  final ValueChanged<bool> onToggle;
  final String? connecting;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final connected = status['connected'] == true;
    final enabled = status['enabled'] == true;
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            CircleAvatar(
              radius: 26,
              backgroundColor: connected ? theme.colorScheme.primary : theme.colorScheme.surfaceContainerHighest,
              foregroundColor: connected ? theme.colorScheme.onPrimary : theme.colorScheme.onSurfaceVariant,
              child: Icon(connected ? signalIcon(status['signal'] as int?) : Icons.wifi_off, size: 28),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(
                  connecting != null
                      ? 'Connecting to $connecting…'
                      : connected
                          ? (status['ssid'] ?? status['connection'] ?? 'Connected').toString()
                          : enabled
                              ? 'Not connected'
                              : 'Wi-Fi off',
                  style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700),
                ),
                Text(
                  connected
                      ? 'IP ${status['ip'] ?? '–'} · signal ${status['signal'] ?? '–'}% · ${status['device']}'
                      : '${status['device'] ?? 'no device'} · ${status['state'] ?? ''}',
                  style: theme.textTheme.bodyMedium,
                ),
              ]),
            ),
            Switch(value: enabled, onChanged: connecting == null ? onToggle : null),
          ]),
          if (connecting != null) ...[
            const SizedBox(height: 12),
            const LinearProgressIndicator(),
          ],
        ]),
      ),
    );
  }
}

class _NetworkTile extends StatelessWidget {
  const _NetworkTile({required this.n, required this.onTap, required this.busy, required this.disabled});
  final Map<String, dynamic> n;
  final VoidCallback onTap;
  final bool busy;
  final bool disabled;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final inUse = n['in_use'] == true;
    final secured = (n['security'] as String? ?? '').isNotEmpty;
    return ListTile(
      contentPadding: const EdgeInsets.only(left: 4, right: 8),
      enabled: !disabled || busy,
      onTap: disabled ? null : onTap,
      leading: Stack(clipBehavior: Clip.none, children: [
        Icon(signalIcon(n['signal'] as int?), color: inUse ? theme.colorScheme.primary : null),
        if (secured)
          const Positioned(right: -4, bottom: -2, child: Icon(Icons.lock, size: 12)),
      ]),
      title: Text(n['ssid'] as String,
          style: TextStyle(fontWeight: inUse ? FontWeight.w700 : FontWeight.w500)),
      subtitle: Text([
        if (inUse) 'Connected',
        if (!inUse && n['saved'] == true) 'Saved',
        '${n['signal']}%',
        (n['security'] as String?)?.isNotEmpty == true ? n['security'] : 'Open',
        n['freq'],
      ].join(' · ')),
      trailing: busy
          ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2.5))
          : inUse
              ? Icon(Icons.check_circle, color: theme.colorScheme.primary)
              : const Icon(Icons.chevron_right),
    );
  }
}

class _Credentials {
  _Credentials(this.ssid, this.password);
  final String? ssid;
  final String password;
}

class _PasswordDialog extends StatefulWidget {
  const _PasswordDialog({this.ssid, this.security = ''});
  final String? ssid; // null = ask for SSID too (hidden network)
  final String security;

  @override
  State<_PasswordDialog> createState() => _PasswordDialogState();
}

class _PasswordDialogState extends State<_PasswordDialog> {
  final _ssid = TextEditingController();
  final _pw = TextEditingController();
  bool _show = false;

  bool get _valid {
    final ssidOk = widget.ssid != null || _ssid.text.trim().isNotEmpty;
    final pwOk = _pw.text.isEmpty || _pw.text.length >= 8;
    final needPw = widget.ssid != null && widget.security.isNotEmpty;
    return ssidOk && pwOk && (!needPw || _pw.text.isNotEmpty);
  }

  @override
  void dispose() {
    _ssid.dispose();
    _pw.dispose();
    super.dispose();
  }

  void _submit() {
    if (!_valid) return;
    Navigator.pop(context, _Credentials(widget.ssid ?? _ssid.text.trim(), _pw.text));
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(widget.ssid == null ? 'Hidden network' : 'Connect to ${widget.ssid}'),
      content: SizedBox(
        width: 420,
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          if (widget.ssid == null) ...[
            TextField(
              controller: _ssid,
              autofocus: true,
              decoration: const InputDecoration(labelText: 'Network name (SSID)', border: OutlineInputBorder()),
              onChanged: (_) => setState(() {}),
            ),
            const SizedBox(height: 12),
          ],
          TextField(
            controller: _pw,
            autofocus: widget.ssid != null,
            obscureText: !_show,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
              labelText: widget.ssid == null ? 'Password (empty for open network)' : 'Password',
              helperText: widget.security.isEmpty ? null : widget.security,
              errorText: _pw.text.isNotEmpty && _pw.text.length < 8 ? 'At least 8 characters' : null,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                tooltip: _show ? 'Hide' : 'Show',
                icon: Icon(_show ? Icons.visibility_off : Icons.visibility),
                onPressed: () => setState(() => _show = !_show),
              ),
            ),
            onChanged: (_) => setState(() {}),
            onSubmitted: (_) => _submit(),
          ),
        ]),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(onPressed: _valid ? _submit : null, child: const Text('Connect')),
      ],
    );
  }
}
