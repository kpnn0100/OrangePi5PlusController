import 'package:flutter/material.dart';

import '../../remote_client.dart';
import 'common.dart';

/// Recording settings (REC-04, REC-07, REC-08). Every change is sent at once and the
/// sheet follows `recorder.settings`, so edits from the web or the CLI show up live.
Future<void> showRecorderSettings(BuildContext context, RemoteClient client) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    builder: (_) => DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.88,
      minChildSize: 0.4,
      maxChildSize: 0.96,
      builder: (context, scroll) => _SettingsBody(client: client, scroll: scroll),
    ),
  );
}

class _SettingsBody extends StatefulWidget {
  const _SettingsBody({required this.client, required this.scroll});
  final RemoteClient client;
  final ScrollController scroll;

  @override
  State<_SettingsBody> createState() => _SettingsBodyState();
}

class _SettingsBodyState extends State<_SettingsBody> {
  double? _bitrate;                       // while the slider is dragged
  final _storage = TextEditingController();
  final _sim = TextEditingController(text: '1920x1080@30');
  bool _storageEdited = false;
  bool _switching = false;

  RemoteClient get client => widget.client;

  @override
  void dispose() {
    _storage.dispose();
    _sim.dispose();
    super.dispose();
  }

  Future<void> _set(Map<String, dynamic> patch) =>
      runOp(context, client, 'recorder.settings.set', {'settings': patch});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([client.topic('recorder.settings'), client.topic('recorder')]),
      builder: (context, _) {
        final st = asMap(client.topic('recorder.settings').value);
        final rec = asMap(client.topic('recorder').value);
        final caps = asMap(rec['caps']);
        if (st.isEmpty) return const Center(child: CircularProgressIndicator());
        final h265 = asMap(st['h265']), raw = asMap(st['raw']);
        if (!_storageEdited) _storage.text = st['storage'] as String? ?? '';
        final sim = rec['simulate'] as String?;
        final theme = Theme.of(context);
        return ListView(
          controller: widget.scroll,
          padding: const EdgeInsets.fromLTRB(18, 0, 18, 28),
          children: [
            Text('Recording settings', style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
            const SizedBox(height: 4),
            Text('Shared with the web page and the CLI', style: theme.textTheme.bodySmall),
            const _Group('Format'),
            _Card([
              _Row('Recording format',
                  st['mode'] == 'h265' ? 'Hardware H.265, small files, real time' : 'Uncompressed .arh, exact picture, very large',
                  _seg<String>({'h265': 'H.265', 'raw': 'RAW'}, st['mode'] as String?, (v) => _set({'mode': v}),
                      disabled: {if (caps['vpu_h265'] != true) 'h265'})),
            ]),
            if (st['mode'] == 'h265')
              _Card([
                _Row('Bitrate', '${(_bitrate ?? (h265['bitrate'] as num? ?? 80)).round()} Mb/s',
                    Slider(
                      value: (_bitrate ?? (h265['bitrate'] as num? ?? 80).toDouble()).clamp(2, 200),
                      min: 2,
                      max: 200,
                      divisions: 198,
                      onChanged: (v) => setState(() => _bitrate = v),
                      onChangeEnd: (v) async {
                        await _set({'h265': {'bitrate': v.round()}});
                        if (mounted) setState(() => _bitrate = null);
                      },
                    ),
                    stacked: true),
                _Row('Rate control', 'CBR keeps a steady rate; VBR saves space on calm scenes',
                    _seg<String>({'cbr': 'CBR', 'vbr': 'VBR'}, h265['rc'] as String?, (v) => _set({'h265': {'rc': v}}))),
                _Row('Keyframe interval', 'Shorter = easier editing, bigger files',
                    _seg<double>({0.5: '0.5 s', 1.0: '1 s', 2.0: '2 s', 5.0: '5 s'}, (h265['gop'] as num?)?.toDouble(),
                        (v) => _set({'h265': {'gop': v}}))),
                _Row('Container', 'MKV survives a power cut; MP4 plays everywhere',
                    _seg<String>({'mp4': 'MP4', 'mkv': 'MKV'}, h265['container'] as String?,
                        (v) => _set({'h265': {'container': v}}))),
              ])
            else ...[
              const _Group('Extra copies'),
              _Card([
                _SwitchRow('High-quality H.265', 'A compact copy made from the RAW file', raw['hq'] == true,
                    caps['vpu_h265'] == true || caps['x265'] == true ? (v) => _set({'raw': {'hq': v}}) : null),
                if (raw['hq'] == true) ...[
                  _Row('Encoder', raw['hq_engine'] == 'x265' ? 'CPU, best quality, slow' : 'Hardware, about real time',
                      _seg<String>({'vpu': 'VPU', 'x265': 'x265'}, raw['hq_engine'] as String?,
                          (v) => _set({'raw': {'hq_engine': v}}),
                          disabled: {if (caps['vpu_h265'] != true) 'vpu', if (caps['x265'] != true) 'x265'})),
                  _Row('Quality', null,
                      _seg<String>({'high': 'High', 'higher': 'Higher', 'max': 'Max'}, raw['hq_quality'] as String?,
                          (v) => _set({'raw': {'hq_quality': v}}))),
                  if (raw['hq_engine'] == 'x265')
                    _Row('x265 preset', 'Slower presets compress better',
                        DropdownButton<String>(
                          value: raw['x265_preset'] as String?,
                          items: [
                            for (final p in ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow'])
                              DropdownMenuItem(value: p, child: Text(p)),
                          ],
                          onChanged: (v) => _set({'raw': {'x265_preset': v}}),
                        )),
                  _Row('Chroma', '4:2:0 plays everywhere; source keeps 4:2:2 / 4:4:4',
                      _seg<String>({'420': '4:2:0', 'source': 'Source'}, raw['hq_chroma'] as String?,
                          (v) => _set({'raw': {'hq_chroma': v}}))),
                ],
                _SwitchRow('Lossless FFV1', 'Exact copy at a fraction of the RAW size', raw['ffv1'] == true,
                    caps['ffv1'] == true || caps['gpu_ffv1'] == true ? (v) => _set({'raw': {'ffv1': v}}) : null),
                if (raw['ffv1'] == true)
                  _Row('FFV1 engine', 'GPU is slower but leaves the CPU free',
                      _seg<String>({'cpu': 'CPU', 'gpu': 'GPU'}, raw['ffv1_engine'] as String?,
                          (v) => _set({'raw': {'ffv1_engine': v}}),
                          disabled: {if (caps['ffv1'] != true) 'cpu', if (caps['gpu_ffv1'] != true) 'gpu'})),
                if (raw['ffv1'] == true)
                  _SwitchRow(
                      'Delete RAW after a verified copy',
                      'Every frame and audio sample is compared byte for byte first; if anything differs the RAW stays',
                      raw['ffv1_replace_raw'] != false,
                      (v) => _set({'raw': {'ffv1_replace_raw': v}})),
                if (raw['hq'] == true || raw['ffv1'] == true)
                  _Row('Make copies', 'During recording needs more CPU/VPU',
                      _seg<String>({'during': 'During', 'after': 'After'}, raw['when'] as String?,
                          (v) => _set({'raw': {'when': v}}))),
              ]),
            ],
            const _Group('Input'),
            _Card([
              _SwitchRow('Record audio', caps['audio'] == true ? 'HDMI audio' : 'No HDMI audio device',
                  asMap(st['audio'])['record'] == true, caps['audio'] == true ? (v) => _set({'audio': {'record': v}}) : null),
              _Row('Preview quality', 'Shared by every viewer',
                  _seg<String>({'low': '360p', 'medium': '720p', 'high': '1080p'}, asMap(st['preview'])['quality'] as String?,
                      (v) => _set({'preview': {'quality': v}}))),
              _Row('EDID', 'What the board tells the source it supports',
                  _seg<String>({'4k60': '4K60', '4k30': '4K30', '1080p': '1080p', 'keep': 'Keep'}, st['edid'] as String?,
                      (v) => _set({'edid': v})),
                  stacked: true),
            ]),
            const _Group('Storage'),
            _Card([
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 10),
                child: Row(children: [
                  Expanded(
                    child: TextField(
                      controller: _storage,
                      decoration: const InputDecoration(labelText: 'Folder on the Pi', isDense: true),
                      onChanged: (_) => _storageEdited = true,
                    ),
                  ),
                  const SizedBox(width: 10),
                  FilledButton.tonal(
                    onPressed: () async {
                      final r = await runOp(context, client, 'recorder.settings.set',
                          {'settings': {'storage': _storage.text}}, 'Storage folder saved');
                      if (r != null) _storageEdited = false;
                    },
                    child: const Text('Save'),
                  ),
                ]),
              ),
            ]),
            const _Group('Source'),
            _Card([
              _Row('Input', sim != null ? 'Test pattern $sim' : 'HDMI RX',
                  _switching
                      ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2))
                      : _seg<String>({'hdmi': 'HDMI', 'test': 'Test pattern'}, sim != null ? 'test' : 'hdmi', (v) async {
                          setState(() => _switching = true);
                          await runOp(context, client, 'recorder.source', {'simulate': v == 'test' ? _sim.text : null},
                              v == 'test' ? 'Test pattern on' : 'HDMI input on');
                          if (mounted) setState(() => _switching = false);
                        })),
              Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: TextField(
                  controller: _sim,
                  decoration: const InputDecoration(labelText: 'Test pattern size (width×height@fps)', isDense: true),
                ),
              ),
            ]),
          ],
        );
      },
    );
  }

  Widget _seg<T>(Map<T, String> options, T? value, ValueChanged<T> onChanged, {Set<T> disabled = const {}}) {
    return SegmentedButton<T>(
      showSelectedIcon: false,
      style: const ButtonStyle(visualDensity: VisualDensity.compact, tapTargetSize: MaterialTapTargetSize.shrinkWrap),
      segments: [
        for (final e in options.entries)
          ButtonSegment<T>(value: e.key, label: Text(e.value), enabled: !disabled.contains(e.key)),
      ],
      selected: {if (value != null && options.containsKey(value)) value},
      emptySelectionAllowed: true,
      onSelectionChanged: (s) {
        if (s.isNotEmpty && s.first != value) onChanged(s.first);
      },
    );
  }
}

class _Group extends StatelessWidget {
  const _Group(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 22, 4, 8),
        child: Text(text.toUpperCase(),
            style: Theme.of(context).textTheme.labelSmall?.copyWith(
                letterSpacing: 1.1, fontWeight: FontWeight.w700, color: Theme.of(context).colorScheme.onSurfaceVariant)),
      );
}

class _Card extends StatelessWidget {
  const _Card(this.children);
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.symmetric(horizontal: 14),
      decoration: BoxDecoration(
        color: scheme.surfaceContainer,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: scheme.outlineVariant.withValues(alpha: 0.5)),
      ),
      child: Column(children: [
        for (var i = 0; i < children.length; i++) ...[
          if (i > 0) Divider(height: 1, color: scheme.outlineVariant.withValues(alpha: 0.4)),
          children[i],
        ],
      ]),
    );
  }
}

/// Title + subtitle + control; the control goes under the text on narrow screens.
class _Row extends StatelessWidget {
  const _Row(this.title, this.sub, this.control, {this.stacked = false});
  final String title;
  final String? sub;
  final Widget control;
  final bool stacked;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final text = Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
      Text(title, style: t.bodyLarge?.copyWith(fontWeight: FontWeight.w500)),
      if (sub != null) Text(sub!, style: t.bodySmall?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant)),
    ]);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: LayoutBuilder(builder: (context, c) {
        if (stacked || c.maxWidth < 560) {
          return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            text,
            const SizedBox(height: 10),
            SizedBox(width: double.infinity, child: FittedBox(fit: BoxFit.scaleDown, alignment: Alignment.centerLeft, child: control)),
          ]);
        }
        return Row(children: [Expanded(child: text), const SizedBox(width: 12), control]);
      }),
    );
  }
}

class _SwitchRow extends StatelessWidget {
  const _SwitchRow(this.title, this.sub, this.value, this.onChanged);
  final String title;
  final String? sub;
  final bool value;
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: t.bodyLarge?.copyWith(fontWeight: FontWeight.w500)),
            if (sub != null) Text(sub!, style: t.bodySmall?.copyWith(color: Theme.of(context).colorScheme.onSurfaceVariant)),
          ]),
        ),
        Switch(value: value, onChanged: onChanged),
      ]),
    );
  }
}
