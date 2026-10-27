import 'package:flutter/foundation.dart' show setEquals;
import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import '../../media.dart';
import '../../remote_client.dart';
import 'common.dart';

/// Gallery (GAL-01..09): takes grouped by month and day with thumbnails, jobs, a sheet per take
/// to play, download, convert or delete its files, and many takes at once - a day, a month or any
/// set - saved to the phone. The list comes over Bluetooth; thumbnails, playback and downloads
/// come over Wi-Fi. For film work (GAL-13) each take shows its slate, start timecode, good-take
/// star, markers and A/V sync; a take's notes, good flag and slate are edited in its sheet, and
/// the selected takes (a day, a month) are exported as the editor's CSV sheet.
class GalleryView extends StatefulWidget {
  const GalleryView({super.key, required this.client, required this.active});
  final RemoteClient client;
  final bool active;

  @override
  State<GalleryView> createState() => _GalleryViewState();
}

const _kinds = {'all': 'All', 'RAW': 'RAW', 'H.265': 'H.265', 'H.264': 'H.264', 'FFV1': 'FFV1', 'VIDEO': 'Other'};

/// The board's date of a take ("2026-09-30", GAL-01) - published by the server; computed from
/// `created` for a server that predates it.
String dayOf(Map<String, dynamic> t) {
  final d = t['day'];
  if (d is String && d.length == 10) return d;
  final c = DateTime.fromMillisecondsSinceEpoch(((t['created'] as num?) ?? 0).toInt() * 1000);
  String two(int v) => v.toString().padLeft(2, '0');
  return '${c.year}-${two(c.month)}-${two(c.day)}';
}

const _months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
                 'November', 'December'];
const _weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

String monthLabel(String month) {
  final p = month.split('-');
  return '${_months[int.parse(p[1]) - 1]} ${p[0]}';
}

String dayLabel(String day) {
  final p = day.split('-').map(int.parse).toList();
  final d = DateTime(p[0], p[1], p[2]);
  return '${_weekdays[d.weekday - 1]} ${d.day} ${_months[d.month - 1].substring(0, 3)}';
}

/// Takes by month, then by day, in list order (newest first).
List<(String, List<(String, List<Map<String, dynamic>>)>)> groupTakes(List<Map<String, dynamic>> takes) {
  final months = <(String, List<(String, List<Map<String, dynamic>>)>)>[];
  for (final t in takes) {
    final day = dayOf(t), month = day.substring(0, 7);
    if (months.isEmpty || months.last.$1 != month) months.add((month, []));
    final days = months.last.$2;
    if (days.isEmpty || days.last.$1 != day) days.add((day, []));
    days.last.$2.add(t);
  }
  return months;
}

class _GalleryViewState extends State<GalleryView> {
  List<Map<String, dynamic>>? _takes;
  String _filter = 'all';
  bool _selecting = false;
  final Set<String> _selected = {};              // this phone's own choice, never shared
  int _version = -1;
  bool _loading = false;
  String? _error;

  RemoteClient get client => widget.client;
  ValueNotifier<dynamic> get _gallery => client.topic('gallery');

  @override
  void initState() {
    super.initState();
    _gallery.addListener(_onGallery);
    if (widget.active) _onGallery();
  }

  @override
  void didUpdateWidget(GalleryView old) {
    super.didUpdateWidget(old);
    if (widget.active && !old.active) _onGallery();
  }

  @override
  void dispose() {
    _gallery.removeListener(_onGallery);
    super.dispose();
  }

  void _onGallery() {
    final g = asMap(_gallery.value);
    if (!widget.active) return;
    if (_takes == null || g['version'] != _version) _load();
  }

  void _toggleGroup(List<Map<String, dynamic>> ts) {
    setState(() {
      _selecting = true;
      final on = !ts.every((t) => _selected.contains(t['id']));
      for (final t in ts) {
        on ? _selected.add(t['id'] as String) : _selected.remove(t['id']);
      }
    });
  }

  void _toggleTake(String id) => setState(() {
        _selecting = true;
        _selected.contains(id) ? _selected.remove(id) : _selected.add(id);
      });

  /// GAL-12: the server picks the files (and leaves out what is still being written); each one
  /// goes to the phone's download queue, into Downloads/Arstro like a single download.
  Future<void> _downloadSelected() async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      final r = asMap(await client.request(
          'gallery.bundle', {'takes': _selected.toList(), 'kind': _filter}, const Duration(seconds: 20)));
      final files = List<String>.from(r['files'] as List? ?? const []);
      for (final f in files) {
        await client.media.download('/api/media/$f', f);
      }
      final skipped = asList(r['skipped']).length;
      messenger.showSnackBar(SnackBar(
          content: Text('Saving ${files.length} file${files.length == 1 ? '' : 's'} (${size(r['size'] as num?)}) '
              'to Downloads/Arstro${skipped > 0 ? ' · $skipped still being written, left out' : ''}')));
      if (mounted) {
        setState(() {
          _selecting = false;
          _selected.clear();
        });
      }
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.toString())));
    }
  }

  /// GAL-13: the editor's sheet of the selection - a whole day or month by its name, else the
  /// takes - saved into Downloads/Arstro like the downloads (no Wi-Fi needed).
  Future<void> _exportCsv() async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      final r = asMap(await client.request('gallery.csv', _csvScope(), const Duration(seconds: 30)));
      final name = r['name'] as String? ?? 'arstro-takes.csv';
      final where = await MediaLink.saveText(name, r['csv'] as String? ?? '');
      messenger.showSnackBar(SnackBar(
          content: Text('Saved $name · ${r['rows']} row${r['rows'] == 1 ? '' : 's'} · ${where ?? 'Downloads/Arstro/$name'}')));
      if (mounted) {
        setState(() {
          _selecting = false;
          _selected.clear();
        });
      }
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('CSV export failed: $e')));
    }
  }

  Map<String, dynamic> _csvScope() {
    if (_filter == 'all') {
      for (final (month, days) in groupTakes(_takes ?? const [])) {
        for (final (day, ts) in days) {
          if (setEquals({for (final t in ts) t['id']}, _selected)) return {'day': day};
        }
        if (setEquals({for (final (_, ts) in days) for (final t in ts) t['id']}, _selected)) return {'month': month};
      }
    }
    return {'takes': _selected.toList()};
  }

  Future<void> _load() async {
    if (_loading || !client.isConnected) return;
    _loading = true;
    try {
      final r = asMap(await client.request('gallery.list', const {}, const Duration(seconds: 30)));
      if (!mounted) return;
      setState(() {
        _takes = asList(r['takes']);
        _version = (r['version'] as num?)?.toInt() ?? -1;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    } finally {
      _loading = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final media = client.media;
    return ListenableBuilder(
      listenable: Listenable.merge([media, client.topic('jobs'), _gallery]),
      builder: (context, _) {
        final g = asMap(_gallery.value);
        final jobs = asList(client.topic('jobs').value);
        final takes = _takes;
        final shown = takes == null
            ? const <Map<String, dynamic>>[]
            : _filter == 'all'
                ? takes
                : takes.where((t) => asList(t['items']).any((i) => i['kind'] == _filter)).toList();
        final theme = Theme.of(context);
        final list = RefreshIndicator(
          onRefresh: () async {
            await _load();
            await media.refresh();
          },
          child: CustomScrollView(slivers: [
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
              sliver: SliverToBoxAdapter(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  if (g.isNotEmpty)
                    Text('${g['takes']} takes · ${size(g['size'] as num?)} · ${g['folder']}',
                        maxLines: 2, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodySmall),
                  const SizedBox(height: 10),
                  Row(children: [
                    Expanded(
                      child: SingleChildScrollView(
                        scrollDirection: Axis.horizontal,
                        child: Row(children: [
                          for (final e in _kinds.entries)
                            Padding(
                              padding: const EdgeInsets.only(right: 6),
                              child: ChoiceChip(
                                label: Text(e.value),
                                selected: _filter == e.key,
                                showCheckmark: false,
                                onSelected: (_) => setState(() => _filter = e.key),
                              ),
                            ),
                        ]),
                      ),
                    ),
                    TextButton(
                      onPressed: () => setState(() {
                        _selecting = !_selecting;
                        if (!_selecting) _selected.clear();
                      }),
                      child: Text(_selecting ? 'Done' : 'Select'),
                    ),
                  ]),
                  if (!media.ready) _MediaBanner(media: media),
                  if (jobs.isNotEmpty) JobsCard(client: client, jobs: jobs),
                  const SizedBox(height: 12),
                ]),
              ),
            ),
            if (takes == null)
              SliverFillRemaining(
                hasScrollBody: false,
                child: Center(
                  child: _error != null
                      ? Padding(padding: const EdgeInsets.all(24), child: Text(_error!, textAlign: TextAlign.center))
                      : const CircularProgressIndicator(),
                ),
              )
            else if (shown.isEmpty)
              SliverFillRemaining(
                hasScrollBody: false,
                child: Center(
                  child: Column(mainAxisSize: MainAxisSize.min, children: [
                    Icon(Icons.movie_outlined, size: 44, color: theme.colorScheme.onSurfaceVariant),
                    const SizedBox(height: 10),
                    Text(takes.isEmpty ? 'No recordings yet.' : 'Nothing in this format.'),
                  ]),
                ),
              )
            else
              for (final (month, days) in groupTakes(shown)) ...[
                SliverToBoxAdapter(
                  child: _GroupHead(
                    label: monthLabel(month),
                    takes: [for (final (_, ts) in days) ...ts],
                    selected: _selected,
                    selecting: _selecting,
                    big: true,
                    onToggle: _toggleGroup,
                  ),
                ),
                for (final (day, ts) in days) ...[
                  SliverToBoxAdapter(
                    child: _GroupHead(
                        label: dayLabel(day), takes: ts, selected: _selected, selecting: _selecting, onToggle: _toggleGroup),
                  ),
                  _grid(ts, media),
                ],
              ],
            SliverToBoxAdapter(child: SizedBox(height: _selecting ? 96 : 24)),
          ]),
        );
        return Stack(children: [
          list,
          Positioned(
            left: 12,
            right: 12,
            bottom: 12,
            child: IgnorePointer(
              ignoring: !_selecting,
              child: AnimatedSlide(
                offset: _selecting ? Offset.zero : const Offset(0, 0.4),
                duration: const Duration(milliseconds: 200),
                curve: Curves.easeOutCubic,
                child: AnimatedOpacity(
                  opacity: _selecting ? 1 : 0,
                  duration: const Duration(milliseconds: 200),
                  child: _SelectionBar(
                    takes: (takes ?? const []).where((t) => _selected.contains(t['id'])).toList(),
                    kind: _filter,
                    canDownload: media.ready,
                    onClear: () => setState(_selected.clear),
                    onDownload: _downloadSelected,
                    onCsv: _exportCsv,
                  ),
                ),
              ),
            ),
          ),
        ]);
      },
    );
  }

  // rows as tall as a 16:9 thumbnail plus the text under it, whatever the width
  Widget _grid(List<Map<String, dynamic>> ts, MediaLink media) => SliverPadding(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
        sliver: SliverLayoutBuilder(builder: (context, c) {
          const gap = 12.0;
          final cols = (c.crossAxisExtent / (280 + gap)).ceil().clamp(1, 12);
          final w = (c.crossAxisExtent - (cols - 1) * gap) / cols;
          final tsc = MediaQuery.textScalerOf(context).scale(14) / 14;
          return SliverGrid(
            gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
              crossAxisCount: cols,
              mainAxisSpacing: gap,
              crossAxisSpacing: gap,
              mainAxisExtent: w * 9 / 16 + 30 + 40 * tsc + 24,
            ),
            delegate: SliverChildBuilderDelegate(
              (context, i) {
                final id = ts[i]['id'] as String;
                return _TakeCard(
                  take: ts[i],
                  media: media,
                  selecting: _selecting,
                  selected: _selected.contains(id),
                  onTap: () => _selecting ? _toggleTake(id) : showTake(context, client, id),
                  onLongPress: () => _toggleTake(id),
                );
              },
              childCount: ts.length,
            ),
          );
        }),
      );
}

/// A month's or a day's heading: its name, count and size, and - while selecting - a checkbox
/// that takes the whole group (ticked, empty, or mixed).
class _GroupHead extends StatelessWidget {
  const _GroupHead({required this.label, required this.takes, required this.selected, required this.selecting,
                    required this.onToggle, this.big = false});
  final String label;
  final List<Map<String, dynamic>> takes;
  final Set<String> selected;
  final bool selecting;
  final bool big;
  final void Function(List<Map<String, dynamic>>) onToggle;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final n = takes.where((t) => selected.contains(t['id'])).length;
    final total = takes.fold<num>(0, (a, t) => a + ((t['size'] as num?) ?? 0));
    return Padding(
      padding: EdgeInsets.fromLTRB(selecting ? 4 : 16, big ? 14 : 8, 16, 6),
      child: Row(children: [
        AnimatedSize(
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOutCubic,
          child: selecting
              ? Checkbox(
                  tristate: true,
                  value: n == 0 ? false : n == takes.length ? true : null,
                  onChanged: (_) => onToggle(takes),
                  semanticLabel: 'Select $label',
                )
              : const SizedBox(width: 0),
        ),
        Flexible(
          child: Text(label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: big
                  ? theme.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)
                  : theme.textTheme.titleSmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        ),
        const SizedBox(width: 8),
        Text('${takes.length} take${takes.length == 1 ? '' : 's'} · ${size(total)}', style: theme.textTheme.bodySmall),
      ]),
    );
  }
}

/// The selection's sum and the one action (GAL-12): how many takes, how many files of the kind
/// the filter shows, how big - and Download.
class _SelectionBar extends StatelessWidget {
  const _SelectionBar({required this.takes, required this.kind, required this.canDownload, required this.onClear,
                       required this.onDownload, required this.onCsv});
  final List<Map<String, dynamic>> takes;
  final String kind;
  final bool canDownload;
  final VoidCallback onClear;
  final VoidCallback onDownload;
  final VoidCallback onCsv;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final files = [
      for (final t in takes)
        for (final i in asList(t['items']))
          if (kind == 'all' || i['kind'] == kind) i
    ];
    final ready = files.where((i) => i['recording'] != true && i['busy'] != true).toList();
    final bytes = ready.fold<num>(0, (a, i) => a + ((i['size'] as num?) ?? 0));
    final text = takes.isEmpty
        ? 'Choose takes, a day or a month'
        : '${takes.length} take${takes.length == 1 ? '' : 's'} · ${ready.length} file${ready.length == 1 ? '' : 's'} · ${size(bytes)}';
    return Material(
      elevation: 6,
      color: theme.colorScheme.surfaceContainerHigh,
      borderRadius: BorderRadius.circular(14),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 8, 8, 8),
        child: Row(children: [
          Expanded(child: Text(text, maxLines: 2, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodyMedium)),
          TextButton(onPressed: takes.isEmpty ? null : onClear, child: const Text('Clear')),
          Tooltip(
            message: 'Export the takes as a CSV for the editor (DaVinci Resolve metadata)',
            child: TextButton(onPressed: takes.isEmpty ? null : onCsv, child: const Text('CSV')),
          ),
          FilledButton.icon(
            onPressed: ready.isEmpty || !canDownload ? null : onDownload,
            icon: const Icon(Icons.download, size: 18),
            label: const Text('Download'),
          ),
        ]),
      ),
    );
  }
}

class _MediaBanner extends StatelessWidget {
  const _MediaBanner({required this.media});
  final MediaLink media;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.only(top: 12),
      padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
      decoration: BoxDecoration(
        color: kWarn.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: kWarn.withValues(alpha: 0.35)),
      ),
      child: Row(children: [
        const Icon(Icons.wifi_off_rounded, color: kWarn, size: 20),
        const SizedBox(width: 10),
        Expanded(
          child: Text(
            media.checking ? 'Looking for the Pi on Wi-Fi…' : (media.error ?? 'Pictures and playback need Wi-Fi.'),
            style: TextStyle(fontSize: 12.5, color: scheme.onSurface),
          ),
        ),
        TextButton(onPressed: media.checking ? null : media.refresh, child: const Text('Retry')),
      ]),
    );
  }
}

String _thumbPath(Map<String, dynamic> take) {
  var v = 0;
  for (final i in asList(take['items'])) {
    final m = (i['mtime'] as num?)?.toInt() ?? 0;
    if (m > v) v = m;
  }
  return 'api/thumb/${Uri.encodeComponent(take['id'] as String)}?v=$v';
}

/// "27 Sep 2026  22:52:52" -> ("27 Sep 2026", "22:52:52")
(String, String?) _titleParts(String title) {
  final m = RegExp(r'^(.*\S)\s{2,}(\S+)$').firstMatch(title);
  return m == null ? (title, null) : (m.group(1)!, m.group(2));
}

/// A take's A/V sync (REC-09, GAL-13): (drift in ms, more than one frame?) or null if unknown.
(double, bool)? avDrift(Map<String, dynamic> meta, List<Map<String, dynamic>> items) {
  final d = (meta['drift_ms'] as num?)?.toDouble();
  if (d == null) return null;
  double? fps;
  final rate = RegExp(r'^(\d+)/(\d+)$').firstMatch('${meta['rate'] ?? ''}');
  if (rate != null && int.parse(rate.group(2)!) > 0) fps = int.parse(rate.group(1)!) / int.parse(rate.group(2)!);
  fps ??= items.map((i) => (i['fps'] as num?)?.toDouble()).whereType<double>().firstOrNull;
  final frameMs = 1000 / (fps == null || fps <= 0 ? 30 : fps);
  return (d, d.abs() > frameMs);
}

class Thumb extends StatelessWidget {
  const Thumb({super.key, required this.take, required this.media, this.fit = BoxFit.cover});
  final Map<String, dynamic> take;
  final MediaLink media;
  final BoxFit fit;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final readable = asList(take['items']).any((i) => !('${i['problem'] ?? ''}').startsWith("can't"));
    final ph = ColoredBox(
      color: scheme.surfaceContainerHighest,
      child: Center(child: Icon(readable ? Icons.movie_outlined : Icons.error_outline, color: scheme.onSurfaceVariant)),
    );
    if (!media.ready || !readable) return ph;
    return Image.network(
      media.url(_thumbPath(take)).toString(),
      headers: media.headers,
      fit: fit,
      gaplessPlayback: true,
      frameBuilder: (context, child, frame, sync) => AnimatedOpacity(
        opacity: frame == null ? 0 : 1,
        duration: const Duration(milliseconds: 350),
        child: frame == null && !sync ? ph : child,
      ),
      errorBuilder: (_, _, _) => ph,
    );
  }
}

class _TakeCard extends StatelessWidget {
  const _TakeCard({required this.take, required this.media, required this.onTap, this.onLongPress,
                   this.selecting = false, this.selected = false});
  final Map<String, dynamic> take;
  final MediaLink media;
  final VoidCallback onTap;
  final VoidCallback? onLongPress;
  final bool selecting;
  final bool selected;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final items = asList(take['items']);
    final (date, time) = _titleParts(take['title'] as String? ?? '');
    final meta = asMap(take['meta']);
    final slate = take['slate'] as String?;
    final markers = asList(meta['markers']).length;
    final drift = avDrift(meta, items);
    return Card(
      margin: EdgeInsets.zero,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: selected ? theme.colorScheme.primary : Colors.transparent, width: 2),
      ),
      child: InkWell(
        onTap: onTap,
        onLongPress: onLongPress,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          AspectRatio(
            aspectRatio: 16 / 9,
            child: Stack(fit: StackFit.expand, children: [
              Thumb(take: take, media: media),
              if (take['recording'] == true)
                Positioned(left: selecting ? 40 : 8, top: 8, child: const Tag('REC', color: kRec, dot: true)),
              Positioned(
                left: 6,
                top: 6,
                child: AnimatedOpacity(
                  opacity: selecting ? 1 : 0,
                  duration: const Duration(milliseconds: 150),
                  child: Icon(selected ? Icons.check_circle : Icons.radio_button_unchecked,
                      color: selected ? theme.colorScheme.primary : Colors.white, size: 26,
                      shadows: const [Shadow(blurRadius: 4)]),
                ),
              ),
              if (meta['good'] == true)
                const Positioned(
                  right: 6,
                  top: 6,
                  child: Icon(Icons.star_rounded, color: kWarn, size: 24, shadows: [Shadow(blurRadius: 4)],
                      semanticLabel: 'Good take'),
                ),
              if (meta['tc'] != null && take['recording'] != true)
                Positioned(
                  left: 8,
                  bottom: 8,
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.7), borderRadius: BorderRadius.circular(6)),
                    child: Text('${meta['tc']}',
                        style: const TextStyle(
                            fontSize: 11.5, fontWeight: FontWeight.w600, color: Colors.white,
                            fontFeatures: [FontFeature.tabularFigures()])),
                  ),
                ),
              if (take['duration'] != null)
                Positioned(
                  right: 8,
                  bottom: 8,
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.7), borderRadius: BorderRadius.circular(6)),
                    child: Text(clock(take['duration'] as num?),
                        style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: Colors.white)),
                  ),
                ),
            ]),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
              Text(slate ?? time ?? date,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.titleSmall?.copyWith(fontWeight: FontWeight.w600)),
              Text('${slate != null && time != null ? '$time · ' : ''}${time != null ? '$date · ' : ''}'
                  '${items.length} file${items.length == 1 ? '' : 's'} · ${size(take['size'] as num?)}',
                  maxLines: 1, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodySmall),
              const SizedBox(height: 6),
              // one line of format tags; extra ones are clipped, never overflow
              SizedBox(
                height: 22,
                child: Wrap(spacing: 4, runSpacing: 4, clipBehavior: Clip.hardEdge, children: [
                  if (drift != null && drift.$2) Tag('A/V ${drift.$1.round()} ms', color: kWarn),
                  for (final k in List<String>.from(take['kinds'] as List? ?? const []))
                    Tag(kindLabel(k), color: kindColor(k)),
                  if (markers > 0) Tag('$markers marker${markers == 1 ? '' : 's'}'),
                ]),
              ),
            ]),
          ),
        ]),
      ),
    );
  }
}

// ------------------------------------------------------------------ jobs
class JobsCard extends StatelessWidget {
  const JobsCard({super.key, required this.client, required this.jobs});
  final RemoteClient client;
  final List<Map<String, dynamic>> jobs;

  static bool finished(Map<String, dynamic> j) => const {'done', 'failed', 'cancelled'}.contains(j['state']);

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.only(top: 12),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 10, 6, 6),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(
              child: Text('CONVERSIONS',
                  style: theme.textTheme.labelMedium?.copyWith(letterSpacing: 1.1, fontWeight: FontWeight.w600)),
            ),
            if (jobs.any(finished))
              TextButton(onPressed: () => runOp(context, client, 'jobs.clear'), child: const Text('Clear finished')),
          ]),
          for (final j in jobs) _JobRow(client: client, job: j),
        ]),
      ),
    );
  }
}

class _JobRow extends StatelessWidget {
  const _JobRow({required this.client, required this.job});
  final RemoteClient client;
  final Map<String, dynamic> job;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final state = job['state'] as String? ?? '';
    final pct = (((job['progress'] as num?) ?? 0) * 100).round();
    final done = JobsCard.finished(job);
    final v = asMap(job['verify']);
    final verdict = v.isEmpty
        ? null
        : v['ok'] == true
            ? Tag(v['raw_deleted'] == true ? 'Verified · RAW deleted' : 'Verified', color: kOk)
            : const Tag('Not identical', color: kRec);
    final detail = state == 'running' || state == 'verifying'
        ? [
            if (state == 'verifying') 'comparing every frame with the RAW',
            if ((job['fps'] as num? ?? 0) > 0) '${(job['fps'] as num).round()} fps',
            if (job['eta'] != null) '${clock(job['eta'] as num)} left',
            if (job['live'] == true) 'following the recording',
          ].join(' · ')
        : v.isNotEmpty
            ? '${v['detail']}${v['ok'] == true ? (v['raw_deleted'] == true ? ' · the FFV1 is now the original' : '') : ' · the RAW is kept'}'
            : (job['error'] as String? ?? '');
    final tag = switch (state) {
      'running' => Tag('$pct%', color: const Color(0xFF9DB4FF)),
      'verifying' => Tag('Checking $pct%', color: const Color(0xFF9DB4FF)),
      'done' => verdict ?? const Tag('Done', color: kOk),
      'failed' => verdict ?? const Tag('Failed', color: kRec),
      'cancelled' => const Tag('Cancelled'),
      _ => const Tag('Queued'),
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 8, right: 8),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(job['output'] as String? ?? '', maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w500)),
              Text('${job['title']} · ${job['codec'] == 'verify' ? 'against' : 'from'} ${job['source']}',
                  maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall),
            ]),
          ),
          const SizedBox(width: 8),
          tag,
          if (!done)
            IconButton(
              tooltip: 'Cancel',
              visualDensity: VisualDensity.compact,
              icon: const Icon(Icons.close_rounded, size: 18),
              onPressed: () => runOp(context, client, 'jobs.cancel', {'job': job['id']}),
            ),
        ]),
        if (!done)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(4),
              child: TweenAnimationBuilder<double>(
                tween: Tween(end: pct / 100),
                duration: const Duration(milliseconds: 500),
                builder: (_, v, _) => LinearProgressIndicator(value: state == 'queued' ? null : v, minHeight: 5),
              ),
            ),
          ),
        if (detail.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(detail,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: state == 'failed' || (v.isNotEmpty && v['ok'] != true) ? kRec : null)),
          ),
      ]),
    );
  }
}

// ------------------------------------------------------------------ take sheet
bool playable(Map<String, dynamic> i) {
  if (i['recording'] == true || i['problem'] != null) return false;
  return const {'H.264', 'H.265', 'VIDEO'}.contains(i['kind']) &&
      const {'h264', 'hevc', 'vp9', 'vp8', 'av1', 'mpeg4'}.contains(i['codec']);
}

Future<void> showTake(BuildContext context, RemoteClient client, String takeId) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    builder: (_) => DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.9,
      minChildSize: 0.4,
      maxChildSize: 0.96,
      builder: (context, scroll) => _TakeSheet(client: client, takeId: takeId, scroll: scroll),
    ),
  );
}

class _TakeSheet extends StatefulWidget {
  const _TakeSheet({required this.client, required this.takeId, required this.scroll});
  final RemoteClient client;
  final String takeId;
  final ScrollController scroll;

  @override
  State<_TakeSheet> createState() => _TakeSheetState();
}

class _TakeSheetState extends State<_TakeSheet> {
  Map<String, dynamic>? _take;
  String? _playing;
  VideoPlayerController? _player;
  String? _playerError;

  RemoteClient get client => widget.client;

  @override
  void initState() {
    super.initState();
    client.topic('gallery').addListener(_refresh);
    _refresh();
  }

  @override
  void dispose() {
    client.topic('gallery').removeListener(_refresh);
    _player?.dispose();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final t = asMap(await client.request('gallery.get', {'take': widget.takeId}));
      if (!mounted) return;
      setState(() => _take = t);
      final items = asList(t['items']);
      if (_playing != null && !items.any((i) => i['id'] == _playing)) _play(null);
    } catch (e) {
      if (mounted && _take != null) Navigator.of(context).maybePop();
    }
  }

  Future<void> _play(Map<String, dynamic>? item) async {
    final old = _player;
    setState(() {
      _player = null;
      _playing = item?['id'] as String?;
      _playerError = null;
    });
    await old?.dispose();
    final media = client.media;
    if (item == null || !media.ready) return;
    final c = VideoPlayerController.networkUrl(media.url(item['url'] as String), httpHeaders: media.headers);
    setState(() => _player = c);
    try {
      await c.initialize();
      if (!mounted || _player != c) return;
      await c.play();
      setState(() {});
    } catch (e) {
      if (mounted && _player == c) setState(() => _playerError = "This phone can't play ${item['kind']} here ($e)");
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = _take;
    final theme = Theme.of(context);
    if (t == null) return const Center(child: CircularProgressIndicator());
    final items = asList(t['items']);
    final media = client.media;
    final locked = items.any((i) => i['recording'] == true || i['busy'] == true);
    return ListView(
      controller: widget.scroll,
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
      children: [
        Text(t['title'] as String? ?? '', style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
        if (t['slate'] != null)
          Text(t['slate'] as String, style: theme.textTheme.titleSmall?.copyWith(color: theme.colorScheme.primary)),
        const SizedBox(height: 12),
        // at most ~half the screen height, so the file list stays visible in landscape
        Center(
          child: ConstrainedBox(
            constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * 0.5),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(16),
              child: AspectRatio(
                aspectRatio: 16 / 9,
                child: ColoredBox(color: Colors.black, child: _playerArea(t, items, media)),
              ),
            ),
          ),
        ),
        const SizedBox(height: 14),
        _TakeInfo(
          key: ValueKey(t['id']),
          client: client,
          take: t,
          onMeta: (m) => setState(() => _take = {...t, 'meta': m}),
        ),
        const SizedBox(height: 14),
        Row(children: [
          Text('Files', style: theme.textTheme.titleSmall),
          const SizedBox(width: 8),
          Text('${items.length} · ${size(t['size'] as num?)}', style: theme.textTheme.bodySmall),
        ]),
        for (final i in items) _variant(i, media),
        const SizedBox(height: 16),
        OutlinedButton.icon(
          style: OutlinedButton.styleFrom(foregroundColor: kRec),
          onPressed: locked
              ? null
              : () async {
                  if (!await confirm(context, 'Delete the whole take?',
                      'All ${items.length} files (${size(t['size'] as num?)}) of ${t['title']} will be removed from the Pi.',
                      ok: 'Delete all')) {
                    return;
                  }
                  if (!context.mounted) return;
                  final r = await runOp(context, client, 'gallery.delete_take', {'take': t['id']}, 'Take deleted');
                  if (r != null && context.mounted) Navigator.of(context).pop();
                },
          icon: const Icon(Icons.delete_outline),
          label: const Text('Delete take'),
        ),
      ],
    );
  }

  Widget _playerArea(Map<String, dynamic> t, List<Map<String, dynamic>> items, MediaLink media) {
    final p = _player;
    if (p != null && _playerError == null) {
      return ValueListenableBuilder<VideoPlayerValue>(
        valueListenable: p,
        builder: (context, v, _) => Stack(fit: StackFit.expand, children: [
          if (v.isInitialized)
            Center(child: AspectRatio(aspectRatio: v.aspectRatio, child: VideoPlayer(p)))
          else
            const Center(child: CircularProgressIndicator()),
          Positioned(
            left: 0,
            right: 0,
            bottom: 0,
            child: Container(
              color: Colors.black45,
              child: Row(children: [
                IconButton(
                  color: Colors.white,
                  icon: Icon(v.isPlaying ? Icons.pause_rounded : Icons.play_arrow_rounded),
                  onPressed: () => v.isPlaying ? p.pause() : p.play(),
                ),
                Expanded(child: VideoProgressIndicator(p, allowScrubbing: true, padding: const EdgeInsets.symmetric(vertical: 12))),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                  child: Text('${clock(v.position.inSeconds)} / ${clock(v.duration.inSeconds)}',
                      style: const TextStyle(color: Colors.white, fontSize: 12)),
                ),
              ]),
            ),
          ),
        ]),
      );
    }
    final best = items.where(playable).toList()
      ..sort((a, b) => ['H.264', 'VIDEO', 'H.265'].indexOf(a['kind'] as String).compareTo(
          ['H.264', 'VIDEO', 'H.265'].indexOf(b['kind'] as String)));
    final note = _playerError ??
        (!media.ready
            ? (media.error ?? 'Playback needs Wi-Fi to the Pi.')
            : t['recording'] == true
                ? 'Still recording.'
                : best.isEmpty
                    ? 'No copy this phone can play. Make an H.264 copy to watch it here.'
                    : null);
    final src = items.where((i) => i['recording'] != true && i['kind'] != 'H.264').firstOrNull;
    return Stack(fit: StackFit.expand, children: [
      Thumb(take: t, media: media, fit: BoxFit.contain),
      if (note == null)
        Center(
          child: IconButton.filled(
            iconSize: 34,
            style: IconButton.styleFrom(backgroundColor: Colors.black54, foregroundColor: Colors.white),
            onPressed: () => _play(best.first),
            icon: const Icon(Icons.play_arrow_rounded),
          ),
        )
      else
        Positioned(
          left: 10,
          right: 10,
          bottom: 10,
          child: Container(
            padding: const EdgeInsets.fromLTRB(12, 8, 8, 8),
            decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.72), borderRadius: BorderRadius.circular(12)),
            child: Row(children: [
              Expanded(child: Text(note, style: const TextStyle(color: Colors.white, fontSize: 12.5))),
              if (best.isEmpty && src != null && t['recording'] != true)
                TextButton(
                  onPressed: () => showConvert(context, client, src, preselect: 'h264-vpu'),
                  child: const Text('Make H.264'),
                ),
            ]),
          ),
        ),
    ]);
  }

  Future<void> _verify(Map<String, dynamic> i) async {
    final raw = asList(_take?['items']).firstWhere((x) => x['kind'] == 'RAW');
    final choice = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Check the FFV1 copy'),
        content: Text('Every frame and audio sample of ${i['id']} is compared byte for byte with ${raw['id']}. '
            '${size(raw['size'] as num?)} could be freed. If anything differs, nothing is deleted.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.pop(c, 'check'), child: const Text('Check only')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: kRec, foregroundColor: Colors.white),
            onPressed: () => Navigator.pop(c, 'delete'),
            child: const Text('Check, then delete RAW'),
          ),
        ],
      ),
    );
    if (choice == null || !mounted) return;
    await runOp(context, client, 'gallery.verify', {'file': i['id'], 'delete_raw': choice == 'delete'},
        'Checking ${i['id']} against the RAW');
  }

  Widget _variant(Map<String, dynamic> i, MediaLink media) {
    final theme = Theme.of(context);
    final locked = i['recording'] == true || i['busy'] == true;
    final meta = [
      if (i['width'] != null) '${i['width']}×${i['height']}',
      if (i['fps'] != null) '${((i['fps'] as num) * 100).round() / 100} fps',
      if (i['duration'] != null) clock(i['duration'] as num),
      size(i['size'] as num?),
      ?i['pixel_format'] as String?,
      if (i['audio'] == true) 'audio',
    ].join(' · ');
    final isPlaying = _playing == i['id'];
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Padding(padding: const EdgeInsets.only(top: 2), child: Tag(kindLabel(i['kind'] as String), color: kindColor(i['kind'] as String))),
        const SizedBox(width: 10),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(i['id'] as String,
                style: theme.textTheme.bodyMedium?.copyWith(
                    fontWeight: FontWeight.w500, color: isPlaying ? theme.colorScheme.primary : null)),
            Text(meta, style: theme.textTheme.bodySmall),
            if (i['recording'] == true) const Padding(padding: EdgeInsets.only(top: 4), child: Tag('Recording', color: kRec, dot: true)),
            if (i['busy'] == true && i['recording'] != true)
              const Padding(padding: EdgeInsets.only(top: 4), child: Tag('In use', color: Color(0xFF9DB4FF))),
            if (i['verified'] != null)
              const Padding(padding: EdgeInsets.only(top: 4), child: Tag('Verified lossless', color: kOk)),
            if (i['problem'] != null)
              Text(i['problem'] as String, style: theme.textTheme.bodySmall?.copyWith(color: kWarn)),
          ]),
        ),
        PopupMenuButton<String>(
          tooltip: 'Actions',
          icon: const Icon(Icons.more_vert_rounded),
          onSelected: (a) async {
            switch (a) {
              case 'play':
                _play(i);
              case 'download':
                try {
                  await media.download(i['url'] as String, i['id'] as String);
                  if (mounted) {
                    ScaffoldMessenger.of(context)
                        .showSnackBar(SnackBar(content: Text('Downloading ${i['id']} to Downloads/Arstro')));
                  }
                } catch (e) {
                  if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Download failed: $e')));
                }
              case 'convert':
                showConvert(context, client, i);
              case 'verify':
                _verify(i);
              case 'delete':
                final others = asList(_take?['items']).length - 1;
                if (!await confirm(context, 'Delete this file?',
                    '${i['id']} (${size(i['size'] as num?)}) will be removed from the Pi.'
                    '${others > 0 ? ' The other $others file${others == 1 ? '' : 's'} of this take stay.' : ''}')) {
                  return;
                }
                if (!mounted) return;
                final r = await runOp(context, client, 'gallery.delete', {'file': i['id']}, 'Deleted');
                if (r != null && others == 0 && mounted) Navigator.of(context).pop();
            }
          },
          itemBuilder: (_) => [
            if (playable(i) && media.ready) const PopupMenuItem(value: 'play', child: Text('Play')),
            if (media.ready) const PopupMenuItem(value: 'download', child: Text('Download to phone')),
            if (i['recording'] != true) const PopupMenuItem(value: 'convert', child: Text('Convert…')),
            if (i['kind'] == 'FFV1' && asList(_take?['items']).any((x) => x['kind'] == 'RAW'))
              PopupMenuItem(value: 'verify', enabled: !locked, child: const Text('Check against the RAW…')),
            PopupMenuItem(value: 'delete', enabled: !locked, child: Text(locked ? 'Delete (in use)' : 'Delete this file')),
          ],
        ),
      ]),
    );
  }
}

// ------------------------------------------------------------------ take notes (GAL-13)
/// A take's film data: slate, start timecode, A/V sync, markers - and what the crew adds
/// afterwards: good take, notes, a corrected scene / shot / take (`gallery.annotate`).
class _TakeInfo extends StatefulWidget {
  const _TakeInfo({super.key, required this.client, required this.take, required this.onMeta});
  final RemoteClient client;
  final Map<String, dynamic> take;
  final ValueChanged<Map<String, dynamic>> onMeta;

  @override
  State<_TakeInfo> createState() => _TakeInfoState();
}

class _TakeInfoState extends State<_TakeInfo> {
  late final _notes = TextEditingController(text: '${_meta['notes'] ?? ''}');
  late final _scene = TextEditingController(text: '${_meta['scene'] ?? ''}');
  late final _shot = TextEditingController(text: '${_meta['shot'] ?? ''}');
  late final _takeNo = TextEditingController(text: '${_meta['take'] ?? ''}');
  bool _busy = false;

  Map<String, dynamic> get _meta => asMap(widget.take['meta']);

  @override
  void didUpdateWidget(_TakeInfo old) {
    super.didUpdateWidget(old);
    final was = asMap(old.take['meta']), now = _meta;
    void follow(TextEditingController c, String k) {
      if ('${was[k] ?? ''}' != '${now[k] ?? ''}' && c.text == '${was[k] ?? ''}') c.text = '${now[k] ?? ''}';
    }
    follow(_notes, 'notes');
    follow(_scene, 'scene');
    follow(_shot, 'shot');
    follow(_takeNo, 'take');
  }

  @override
  void dispose() {
    _notes.dispose();
    _scene.dispose();
    _shot.dispose();
    _takeNo.dispose();
    super.dispose();
  }

  Future<void> _annotate(Map<String, dynamic> change, String ok) async {
    setState(() => _busy = true);
    final r = await runOp(context, widget.client, 'gallery.annotate', {'take': widget.take['id'], ...change}, ok);
    if (!mounted) return;
    setState(() => _busy = false);
    if (r is Map && r.isNotEmpty) widget.onMeta(asMap(r));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final m = _meta;
    final muted = theme.colorScheme.onSurfaceVariant;
    final good = m['good'] == true;
    final drift = avDrift(m, asList(widget.take['items']));
    final markers = asList(m['markers']);
    final lines = <(String, Color?)>[
      if (m['tc'] != null) ('Start TC ${m['tc']}${_fps(m['rate']) != null ? ' · ${_fps(m['rate'])} fps' : ''}', null),
      if (m['clock_synced'] == false) ('The clock was not synced (NTP) when this take was made', kWarn),
      if (drift != null)
        drift.$2
            ? ('A/V sync: audio and video differ by ${drift.$1.abs().round()} ms - more than a frame', kWarn)
            : ('A/V in sync (${drift.$1.abs().round()} ms)', null),
      if ((m['drops'] as num? ?? 0) > 0) ('${m['drops']} dropped frames', kRec),
      if (m['audio_source'] != null) ('Audio: ${m['audio_source']}', null),
    ];
    return Container(
      padding: const EdgeInsets.fromLTRB(14, 6, 14, 14),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainer,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: theme.colorScheme.outlineVariant.withValues(alpha: 0.5)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Expanded(
            child: Text('TAKE', style: theme.textTheme.labelMedium?.copyWith(letterSpacing: 1.1, fontWeight: FontWeight.w600)),
          ),
          FilterChip(
            key: const Key('take-good'),
            avatar: Icon(good ? Icons.star_rounded : Icons.star_border_rounded, color: good ? kWarn : muted, size: 18),
            label: const Text('Good take'),
            showCheckmark: false,
            selected: good,
            onSelected: _busy ? null : (v) => _annotate({'good': v}, v ? 'Marked as a good take' : 'Take updated'),
          ),
        ]),
        for (final (text, colour) in lines)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(text, style: theme.textTheme.bodySmall?.copyWith(color: colour)),
          ),
        if (markers.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text('Markers', style: theme.textTheme.labelLarge),
          for (final mk in markers)
            Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Row(children: [
                const Icon(Icons.bookmark_outline_rounded, size: 15),
                const SizedBox(width: 6),
                Text('${mk['tc'] ?? ''}',
                    style: theme.textTheme.bodySmall?.copyWith(fontFeatures: const [FontFeature.tabularFigures()])),
                const SizedBox(width: 8),
                Expanded(child: Text('${mk['label'] ?? ''}', style: theme.textTheme.bodySmall, overflow: TextOverflow.ellipsis)),
              ]),
            ),
        ],
        const SizedBox(height: 12),
        TextField(
          key: const Key('take-notes'),
          controller: _notes,
          minLines: 1,
          maxLines: 4,
          decoration: const InputDecoration(labelText: 'Notes', hintText: 'Notes for the editor', isDense: true),
        ),
        Align(
          alignment: Alignment.centerRight,
          child: TextButton(
            key: const Key('take-notes-save'),
            onPressed: _busy ? null : () => _annotate({'notes': _notes.text}, 'Take updated'),
            child: const Text('Save notes'),
          ),
        ),
        Row(children: [
          Expanded(
            child: TextField(key: const Key('take-scene'), controller: _scene,
                decoration: const InputDecoration(labelText: 'Scene', isDense: true)),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: TextField(key: const Key('take-shot'), controller: _shot,
                decoration: const InputDecoration(labelText: 'Shot', isDense: true)),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: TextField(key: const Key('take-no'), controller: _takeNo, keyboardType: TextInputType.number,
                decoration: const InputDecoration(labelText: 'Take', isDense: true)),
          ),
        ]),
        Align(
          alignment: Alignment.centerRight,
          child: TextButton(
            key: const Key('take-slate-save'),
            onPressed: _busy
                ? null
                : () => _annotate({
                      'scene': _scene.text.trim(),
                      'shot': _shot.text.trim(),
                      if (int.tryParse(_takeNo.text.trim()) != null) 'take_no': int.parse(_takeNo.text.trim()),
                    }, 'Take updated'),
            child: const Text('Save slate'),
          ),
        ),
      ]),
    );
  }

  static String? _fps(Object? rate) {
    final m = RegExp(r'^(\d+)/(\d+)$').firstMatch('${rate ?? ''}');
    if (m == null || int.parse(m.group(2)!) == 0) return null;
    final f = int.parse(m.group(1)!) / int.parse(m.group(2)!);
    return f == f.roundToDouble() ? f.toStringAsFixed(0) : f.toStringAsFixed(2);
  }
}

// ------------------------------------------------------------------ convert
List<Map<String, dynamic>>? _targetsCache;

const _targetKind = {'h264-vpu': 'H.264', 'h265-vpu': 'H.265', 'h265-x265': 'H.265', 'ffv1': 'FFV1', 'ffv1-gpu': 'FFV1'};

Future<void> showConvert(BuildContext context, RemoteClient client, Map<String, dynamic> item, {String? preselect}) async {
  try {
    _targetsCache ??= asList(asMap(await client.request('gallery.targets'))['targets']);
  } catch (e) {
    if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.toString())));
    return;
  }
  if (!context.mounted) return;
  await showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    builder: (_) => _ConvertSheet(client: client, item: item, targets: _targetsCache!, preselect: preselect),
  );
}

class _ConvertSheet extends StatefulWidget {
  const _ConvertSheet({required this.client, required this.item, required this.targets, this.preselect});
  final RemoteClient client;
  final Map<String, dynamic> item;
  final List<Map<String, dynamic>> targets;
  final String? preselect;

  @override
  State<_ConvertSheet> createState() => _ConvertSheetState();
}

class _ConvertSheetState extends State<_ConvertSheet> {
  late String? _sel;
  String _quality = 'high', _scale = 'source', _preset = 'fast';
  late bool _replaceRaw = asMap(asMap(widget.client.topic('recorder.settings').value)['raw'])['ffv1_replace_raw'] != false;
  final _bitrate = TextEditingController();
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    final avail = widget.targets.where((t) => t['available'] == true).toList();
    _sel = avail.any((t) => t['id'] == widget.preselect)
        ? widget.preselect
        : (avail.where((t) => _targetKind[t['id']] != widget.item['kind']).firstOrNull ?? avail.firstOrNull)?['id'] as String?;
  }

  @override
  void dispose() {
    _bitrate.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final t = widget.targets.where((x) => x['id'] == _sel).firstOrNull;
    final o = asMap(t?['options']);
    return SingleChildScrollView(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 16 + MediaQuery.viewInsetsOf(context).bottom),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('Convert', style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
        Text('From ${widget.item['id']}', style: theme.textTheme.bodySmall),
        const SizedBox(height: 12),
        for (final x in widget.targets)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: _TargetTile(
              target: x,
              selected: x['id'] == _sel,
              onTap: x['available'] == true ? () => setState(() => _sel = x['id'] as String) : null,
            ),
          ),
        if (t != null) ...[
          const SizedBox(height: 6),
          Wrap(spacing: 12, runSpacing: 12, children: [
            if (o['scale'] is List)
              _drop('Size', _scale, {for (final v in o['scale'] as List) '$v': v == 'source' ? 'Same as source' : '${v}p'},
                  (v) => setState(() => _scale = v)),
            if (o['quality'] is List)
              _drop('Quality', _quality, {for (final v in o['quality'] as List) '$v': '${v[0].toUpperCase()}${'$v'.substring(1)}'},
                  (v) => setState(() => _quality = v)),
            if (o['preset'] is List)
              _drop('x265 preset', _preset, {for (final v in o['preset'] as List) '$v': '$v'}, (v) => setState(() => _preset = v)),
            if (o['bitrate'] == true)
              SizedBox(
                width: 160,
                child: TextField(
                  controller: _bitrate,
                  keyboardType: TextInputType.number,
                  decoration: InputDecoration(
                      labelText: 'Bitrate Mb/s',
                      hintText: o['bitrate_max'] is num ? 'auto · max ${o['bitrate_max']}' : 'auto',
                      isDense: true),
                ),
              ),
          ]),
        ],
        if (t != null && '$_sel'.startsWith('ffv1') && widget.item['kind'] == 'RAW')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Delete the RAW when the copy is verified'),
            subtitle: const Text('Every frame and audio sample is compared byte for byte first'),
            value: _replaceRaw,
            onChanged: (v) => setState(() => _replaceRaw = v),
          ),
        const SizedBox(height: 18),
        FilledButton.icon(
          onPressed: t == null || t['available'] != true || _busy
              ? null
              : () async {
                  setState(() => _busy = true);
                  final args = <String, dynamic>{
                    'file': widget.item['id'],
                    'target': _sel,
                    'quality': _quality,
                    'scale': _scale,
                    'preset': _preset,
                    if (double.tryParse(_bitrate.text) != null) 'bitrate': double.parse(_bitrate.text),
                    if ('$_sel'.startsWith('ffv1') && widget.item['kind'] == 'RAW') 'replace_raw': _replaceRaw,
                  };
                  final job = await runOp(context, widget.client, 'gallery.convert', args);
                  if (!context.mounted) return;
                  setState(() => _busy = false);
                  if (job != null) {
                    ScaffoldMessenger.of(context)
                        .showSnackBar(SnackBar(content: Text('Converting to ${asMap(job)['output']}')));
                    Navigator.of(context).pop();
                  }
                },
          icon: _busy
              ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.autorenew_rounded),
          label: const Text('Convert'),
        ),
      ]),
    );
  }

  Widget _drop(String label, String value, Map<String, String> items, ValueChanged<String> onChanged) {
    return SizedBox(
      width: 160,
      child: DropdownButtonFormField<String>(
        initialValue: items.containsKey(value) ? value : items.keys.first,
        isDense: true,
        decoration: InputDecoration(labelText: label, isDense: true),
        items: [for (final e in items.entries) DropdownMenuItem(value: e.key, child: Text(e.value))],
        onChanged: (v) => v == null ? null : onChanged(v),
      ),
    );
  }
}

class _TargetTile extends StatelessWidget {
  const _TargetTile({required this.target, required this.selected, this.onTap});
  final Map<String, dynamic> target;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final enabled = onTap != null;
    return AnimatedContainer(
      duration: const Duration(milliseconds: 200),
      decoration: BoxDecoration(
        color: selected ? scheme.primary.withValues(alpha: 0.12) : Colors.transparent,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: selected ? scheme.primary : scheme.outlineVariant),
      ),
      child: Opacity(
        opacity: enabled ? 1 : 0.45,
        child: InkWell(
          borderRadius: BorderRadius.circular(14),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Icon(selected ? Icons.radio_button_checked : Icons.radio_button_off,
                  size: 20, color: selected ? scheme.primary : scheme.onSurfaceVariant),
              const SizedBox(width: 10),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(target['title'] as String? ?? '', style: const TextStyle(fontWeight: FontWeight.w600)),
                  Text(enabled ? target['description'] as String? ?? '' : 'Not available on this board',
                      style: Theme.of(context).textTheme.bodySmall),
                ]),
              ),
            ]),
          ),
        ),
      ),
    );
  }
}
