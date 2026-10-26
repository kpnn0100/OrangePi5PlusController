import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../app_scope.dart';
import '../slot_links.dart';
import 'widgets.dart';

/// APP-10: the NTWB apps of every slot of the Pi, as the server lists them right now - a newly
/// installed app shows up without an update of this app. Opening one shows the app's own web
/// UI inside the app: a view of the app's core with its own view-model (NTWB-11), exactly what
/// a browser tab gets, so a phone and a desktop can share one session and each keep its layout.
class AppsPage extends StatefulWidget {
  const AppsPage({super.key});

  @override
  State<AppsPage> createState() => _AppsPageState();
}

class _AppsPageState extends State<AppsPage> {
  String? _slot;
  List<Map<String, dynamic>>? _apps;
  String? _error;
  bool _loading = false;
  Object? _loadedFor;

  Future<void> _load(SlotEntry e) async {
    final c = e.client;
    if (c == null || !c.isConnected) {
      setState(() {
        _apps = null;
        _error = e.note ?? 'Connecting to ${e.label}…';
      });
      return;
    }
    setState(() => _loading = true);
    try {
      final r = await c.request('apps.list', const {}, const Duration(seconds: 15));
      if (!mounted) return;
      setState(() {
        _apps = List<Map<String, dynamic>>.from(
            ((r as Map)['apps'] as List? ?? const []).map((a) => Map<String, dynamic>.from(a as Map)));
        _error = null;
      });
    } catch (err) {
      if (mounted) setState(() => _error = '$err');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final links = AppScope.of(context).client.slots;
    return Scaffold(
      appBar: AppBar(title: const Text('Apps')),
      body: ListenableBuilder(
        listenable: links,
        builder: (context, _) {
          final entries = links.entries;
          final entry = entries.firstWhere((e) => e.slot == _slot, orElse: () => entries.first);
          final ready = entry.client?.isConnected ?? false;
          final key = '${entry.slot}|$ready';       // reload when the slot or its link changes
          if (_loadedFor != key) {
            _loadedFor = key;
            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (mounted) _load(entry);
            });
          }
          return Column(children: [
            if (entries.length > 1)
              SizedBox(
                height: 48,
                child: ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                  children: [
                    for (final e in entries)
                      Padding(
                        padding: const EdgeInsets.only(right: 6),
                        child: ChoiceChip(
                          selected: e.slot == entry.slot,
                          showCheckmark: false,
                          label: Text('${e.label} · ${e.via}'),
                          onSelected: (_) => setState(() {
                            _slot = e.slot;
                            _apps = null;
                            _error = null;
                          }),
                        ),
                      ),
                  ],
                ),
              ),
            Expanded(
              child: RefreshIndicator(
                onRefresh: () => _load(entry),
                child: _list(context, entry),
              ),
            ),
          ]);
        },
      ),
    );
  }

  Widget _list(BuildContext context, SlotEntry entry) {
    final apps = _apps;
    if (apps == null) {
      return ListView(children: [
        const SizedBox(height: 80),
        _loading
            ? const Center(child: CircularProgressIndicator())
            : EmptyState(icon: Icons.apps, text: _error ?? 'Loading…'),
      ]);
    }
    if (apps.isEmpty) {
      return ListView(children: const [
        SizedBox(height: 80),
        EmptyState(icon: Icons.apps, text: 'No apps are installed on this slot'),
      ]);
    }
    final theme = Theme.of(context);
    return ListView.separated(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 24),
      itemCount: apps.length,
      separatorBuilder: (_, _) => const SizedBox(height: 8),
      itemBuilder: (context, i) {
        final a = apps[i];
        final problem = a['problem'] as String?;
        final state = a['state'] as String? ?? 'stopped';
        final viewers = (a['clients'] as num?)?.toInt() ?? 0;
        final status = problem ?? (state == 'running' ? 'running${viewers > 0 ? ' · $viewers viewing' : ''}' : state);
        return Card(
          child: ListTile(
            contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
            leading: _AppIcon(entry: entry, app: a),
            title: Text('${a['name'] ?? a['id']}', maxLines: 1, overflow: TextOverflow.ellipsis),
            subtitle: Text(
              [if ((a['description'] as String?)?.isNotEmpty == true) a['description'] as String, status].join('\n'),
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
              style: problem != null ? TextStyle(color: theme.colorScheme.error) : null,
            ),
            trailing: const Icon(Icons.chevron_right),
            enabled: problem == null && entry.base != null,
            onTap: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => AppViewPage(entry: entry, appId: '${a['id']}', title: '${a['name'] ?? a['id']}'))),
          ),
        );
      },
    );
  }
}

class _AppIcon extends StatelessWidget {
  const _AppIcon({required this.entry, required this.app});
  final SlotEntry entry;
  final Map<String, dynamic> app;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final fallback = CircleAvatar(
        backgroundColor: scheme.surfaceContainerHighest, child: Icon(Icons.widgets_outlined, color: scheme.primary));
    final base = entry.base;
    if (app['icon'] == null || app['icon'] == false || base == null) return fallback;
    return ClipRRect(
      borderRadius: BorderRadius.circular(10),
      child: Image.network(
        '${base}apps/${Uri.encodeComponent('${app['id']}')}/icon',
        headers: {if (entry.token != null) 'Authorization': 'Bearer ${entry.token}'},
        width: 40,
        height: 40,
        fit: BoxFit.cover,
        errorBuilder: (_, _, _) => fallback,
      ),
    );
  }
}

/// One NTWB app's web UI in the app (APP-10). The web view logs in to the slot first
/// (POST /api/login sets its cookie, so the page, ntwb.js and the app WebSocket are all
/// authorised like a browser that typed the password), then opens `/apps/<id>/`.
class AppViewPage extends StatefulWidget {
  const AppViewPage({super.key, required this.entry, required this.appId, required this.title});
  final SlotEntry entry;
  final String appId;
  final String title;

  @override
  State<AppViewPage> createState() => _AppViewPageState();
}

class _AppViewPageState extends State<AppViewPage> {
  late final WebViewController _web;
  int _progress = 0;
  String? _error;
  bool _loggedIn = false;

  Uri get _appUri => Uri.parse('${widget.entry.base}apps/${Uri.encodeComponent(widget.appId)}/');

  @override
  void initState() {
    super.initState();
    _web = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFF0B0C0F))
      ..setNavigationDelegate(NavigationDelegate(
        onProgress: (p) => mounted ? setState(() => _progress = p) : null,
        onPageFinished: (url) {
          if (!_loggedIn && url.contains('/api/login')) {
            _loggedIn = true;
            _web.loadRequest(_appUri);
          }
        },
        onWebResourceError: (e) {
          if (e.isForMainFrame ?? true) setState(() => _error = e.description);
        },
      ));
    _open();
  }

  void _open() {
    _error = null;
    final token = widget.entry.token;
    if (token == null) {
      _loggedIn = true;
      _web.loadRequest(_appUri);
      return;
    }
    _loggedIn = false;
    _web.loadRequest(Uri.parse('${widget.entry.base}api/login'),
        method: LoadRequestMethod.post,
        headers: const {'Content-Type': 'application/json'},
        body: utf8.encode(jsonEncode({'token': token})));
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) async {
        if (didPop) return;
        final nav = Navigator.of(context);
        if (await _web.canGoBack() && !(await _web.currentUrl() ?? '').contains('/api/login')) {
          await _web.goBack();
        } else {
          nav.pop();
        }
      },
      child: Scaffold(
        appBar: AppBar(
          title: Text(widget.entry.self ? widget.title : '${widget.title} · ${widget.entry.label}'),
          actions: [
            IconButton(tooltip: 'Reload', icon: const Icon(Icons.refresh), onPressed: () => setState(_open)),
          ],
          bottom: _progress < 100
              ? PreferredSize(
                  preferredSize: const Size.fromHeight(2), child: LinearProgressIndicator(value: _progress / 100))
              : null,
        ),
        body: _error != null
            ? EmptyState(
                icon: Icons.cloud_off,
                text: "Couldn't open ${widget.title}: $_error",
                action: FilledButton.icon(
                    onPressed: () => setState(_open), icon: const Icon(Icons.refresh), label: const Text('Try again')),
              )
            : WebViewWidget(controller: _web),
      ),
    );
  }
}
