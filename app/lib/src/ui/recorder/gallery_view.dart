import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import '../../media.dart';
import '../../remote_client.dart';
import 'common.dart';

/// Gallery (GAL-01..07): takes grid with thumbnails, jobs, and a sheet per take to play,
/// download, convert or delete its files. The list comes over Bluetooth; thumbnails and
/// playback come over Wi-Fi.
class GalleryView extends StatefulWidget {
  const GalleryView({super.key, required this.client, required this.active});
  final RemoteClient client;
  final bool active;

  @override
  State<GalleryView> createState() => _GalleryViewState();
}

const _kinds = {'all': 'All', 'RAW': 'RAW', 'H.265': 'H.265', 'H.264': 'H.264', 'FFV1': 'FFV1', 'VIDEO': 'Other'};

class _GalleryViewState extends State<GalleryView> {
  List<Map<String, dynamic>>? _takes;
  String _filter = 'all';
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
        return RefreshIndicator(
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
                  SingleChildScrollView(
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
              SliverPadding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
                // rows as tall as a 16:9 thumbnail plus the text under it, whatever the width
                sliver: SliverLayoutBuilder(builder: (context, c) {
                  const gap = 12.0;
                  final cols = (c.crossAxisExtent / (280 + gap)).ceil().clamp(1, 12);
                  final w = (c.crossAxisExtent - (cols - 1) * gap) / cols;
                  final ts = MediaQuery.textScalerOf(context).scale(14) / 14;
                  return SliverGrid(
                    gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                      crossAxisCount: cols,
                      mainAxisSpacing: gap,
                      crossAxisSpacing: gap,
                      mainAxisExtent: w * 9 / 16 + 30 + 40 * ts + 24,
                    ),
                    delegate: SliverChildBuilderDelegate(
                      (context, i) => _TakeCard(
                        take: shown[i],
                        media: media,
                        onTap: () => showTake(context, client, shown[i]['id'] as String),
                      ),
                      childCount: shown.length,
                    ),
                  );
                }),
              ),
          ]),
        );
      },
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
  const _TakeCard({required this.take, required this.media, required this.onTap});
  final Map<String, dynamic> take;
  final MediaLink media;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final items = asList(take['items']);
    final (date, time) = _titleParts(take['title'] as String? ?? '');
    return Card(
      margin: EdgeInsets.zero,
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          AspectRatio(
            aspectRatio: 16 / 9,
            child: Stack(fit: StackFit.expand, children: [
              Thumb(take: take, media: media),
              if (take['recording'] == true) const Positioned(left: 8, top: 8, child: Tag('REC', color: kRec, dot: true)),
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
              Text(time ?? date,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.titleSmall?.copyWith(fontWeight: FontWeight.w600)),
              Text('${time != null ? '$date · ' : ''}${items.length} file${items.length == 1 ? '' : 's'} · ${size(take['size'] as num?)}',
                  maxLines: 1, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodySmall),
              const SizedBox(height: 6),
              // one line of format tags; extra ones are clipped, never overflow
              SizedBox(
                height: 22,
                child: Wrap(spacing: 4, runSpacing: 4, clipBehavior: Clip.hardEdge, children: [
                  for (final k in List<String>.from(take['kinds'] as List? ?? const []))
                    Tag(kindLabel(k), color: kindColor(k)),
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
    final detail = state == 'running'
        ? [
            if ((job['fps'] as num? ?? 0) > 0) '${(job['fps'] as num).round()} fps',
            if (job['eta'] != null) '${clock(job['eta'] as num)} left',
            if (job['live'] == true) 'following the recording',
          ].join(' · ')
        : (job['error'] as String? ?? '');
    final tag = switch (state) {
      'running' => Tag('$pct%', color: const Color(0xFF9DB4FF)),
      'done' => const Tag('Done', color: kOk),
      'failed' => const Tag('Failed', color: kRec),
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
              Text('${job['title']} · from ${job['source']}', maxLines: 1, overflow: TextOverflow.ellipsis,
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
              onPressed: () => runOp(context, client, 'jobs.cancel', {'id': job['id']}),
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
            child: Text(detail, style: theme.textTheme.bodySmall?.copyWith(color: state == 'failed' ? kRec : null)),
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
              const Padding(padding: EdgeInsets.only(top: 4), child: Tag('Converting', color: Color(0xFF9DB4FF))),
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
            PopupMenuItem(value: 'delete', enabled: !locked, child: Text(locked ? 'Delete (in use)' : 'Delete this file')),
          ],
        ),
      ]),
    );
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
                  decoration: const InputDecoration(labelText: 'Bitrate Mb/s', hintText: 'auto', isDense: true),
                ),
              ),
          ]),
        ],
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
