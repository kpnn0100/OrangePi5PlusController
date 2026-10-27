import 'dart:async';

import 'package:flutter/material.dart';

import '../../remote_client.dart';
import 'common.dart';

/// Recording settings (REC-04, REC-07, REC-08) and the film work (REC-09 audio input and
/// container, REC-12 storage guard, REC-13/14 monitoring and focus loupe, REC-15 slate, REC-16
/// record button and tally). Every change is sent at once and the sheet follows
/// `recorder.settings`, so edits from the web or the CLI show up live.
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
  double? _zebra;                         // while the zebra level is dragged
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

  Future<void> _set(Map<String, dynamic> patch) async {
    if (!mounted) {
      // a field saved as the sheet closes: no snack bar to show it in
      try {
        await client.request('recorder.settings.set', {'settings': patch});
      } catch (_) {}
      return;
    }
    await runOp(context, client, 'recorder.settings.set', {'settings': patch});
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([client.topic('recorder.settings'), client.topic('recorder')]),
      builder: (context, _) {
        final st = asMap(client.topic('recorder.settings').value);
        final rec = asMap(client.topic('recorder').value);
        final caps = asMap(rec['caps']);
        if (st.isEmpty) return const Center(child: CircularProgressIndicator());
        final h265 = asMap(st['h265']), raw = asMap(st['raw']), audio = asMap(st['audio']);
        final inputs = [
          ...asList(rec['audio_inputs']),
          if (rec['audio_inputs'] == null) {'id': 'hdmi', 'name': 'HDMI input'},
        ];
        if (!inputs.any((i) => i['id'] == (audio['source'] ?? 'hdmi'))) {
          inputs.add({'id': audio['source'], 'name': '${audio['source']} (not connected)'});
        }
        final audioName = inputs.where((i) => i['id'] == (audio['source'] ?? 'hdmi')).firstOrNull?['name'] as String?;
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
                // REC-09: the container decides the audio: PCM as it came (MOV, MKV) or AAC (MP4)
                _Row('Container', _containers[h265['container']] ?? _containers['mov'],
                    _seg<String>({'mov': 'MOV', 'mkv': 'MKV', 'mp4': 'MP4'}, h265['container'] as String?,
                        (v) => _set({'h265': {'container': v}}))),
              ])
            else ...[
              const _Group('Extra copies'),
              // REC-04: one sub-section per kind of copy - H.264, H.265, FFV1 - its switch in the
              // header, its options inside (tap the header; switching a copy on opens it too)
              _Card([
                _CopySection(
                  id: 'h264',
                  title: 'H.264',
                  what: 'MP4 share copy that plays everywhere',
                  summary: '${{'source': 'Source size', '1080': '1080p', '720': '720p'}[raw['h264_scale']] ?? 'Source size'}'
                      ' · ${_cap(raw['h264_quality'])}',
                  on: raw['h264'] == true,
                  onChanged: caps['vpu_h264'] == true ? (v) => _set({'raw': {'h264': v}}) : null,
                  open: _openCopies,
                  children: [
                    _Row('Size', 'Smaller copies are faster to make and to send',
                        _seg<String>({'source': 'Source', '1080': '1080p', '720': '720p'}, raw['h264_scale'] as String?,
                            (v) => _set({'raw': {'h264_scale': v}}))),
                    _Row('Quality', 'Hardware encoder (VPU)',
                        _seg<String>({'high': 'High', 'higher': 'Higher', 'max': 'Max'}, raw['h264_quality'] as String?,
                            (v) => _set({'raw': {'h264_quality': v}}))),
                  ],
                ),
                _CopySection(
                  id: 'h265',
                  title: 'H.265',
                  what: 'Compact high-quality copy',
                  summary: '${raw['hq_engine'] == 'x265' ? 'x265 ${raw['x265_preset']}' : 'VPU'} · ${_cap(raw['hq_quality'])}'
                      ' · ${raw['hq_chroma'] == 'source' ? 'source chroma' : '4:2:0'}',
                  on: raw['hq'] == true,
                  onChanged: caps['vpu_h265'] == true || caps['x265'] == true ? (v) => _set({'raw': {'hq': v}}) : null,
                  open: _openCopies,
                  children: [
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
                ),
                _CopySection(
                  id: 'ffv1',
                  title: 'FFV1',
                  what: 'Lossless: the exact picture at a fraction of the RAW size',
                  summary: '${raw['ffv1_engine'] == 'gpu' ? 'GPU' : 'CPU'}'
                      ' · ${raw['ffv1_replace_raw'] != false ? 'replaces the RAW' : 'keeps the RAW'}',
                  on: raw['ffv1'] == true,
                  onChanged: caps['ffv1'] == true || caps['gpu_ffv1'] == true ? (v) => _set({'raw': {'ffv1': v}}) : null,
                  open: _openCopies,
                  children: [
                    _Row('Engine', 'GPU is slower but leaves the CPU free',
                        _seg<String>({'cpu': 'CPU', 'gpu': 'GPU'}, raw['ffv1_engine'] as String?,
                            (v) => _set({'raw': {'ffv1_engine': v}}),
                            disabled: {if (caps['ffv1'] != true) 'cpu', if (caps['gpu_ffv1'] != true) 'gpu'})),
                    _SwitchRow(
                        'Delete RAW after a verified copy',
                        'Every frame and audio sample is compared byte for byte first; if anything differs the RAW stays',
                        raw['ffv1_replace_raw'] != false,
                        (v) => _set({'raw': {'ffv1_replace_raw': v}})),
                  ],
                ),
                if (raw['h264'] == true || raw['hq'] == true || raw['ffv1'] == true)
                  _Row('Make copies', 'During recording needs more CPU/VPU',
                      _seg<String>({'during': 'During', 'after': 'After'}, raw['when'] as String?,
                          (v) => _set({'raw': {'when': v}}))),
              ]),
            ],
            ..._monitoring(st, rec),
            const _Group('Input'),
            _Card([
              _SwitchRow('Record audio', audioName ?? (caps['audio'] == true ? 'HDMI audio' : 'No HDMI audio device'),
                  audio['record'] == true,
                  caps['audio'] == true || (audio['source'] ?? 'hdmi') != 'hdmi' ? (v) => _set({'audio': {'record': v}}) : null),
              // REC-09: the HDMI input's audio or an audio interface (XLR microphones over USB)
              _Row('Audio input', 'The HDMI input, or an audio interface (e.g. USB with XLR microphones)',
                  DropdownButton<String>(
                    key: const Key('audio-source'),
                    value: audio['source'] as String? ?? 'hdmi',
                    items: [
                      for (final i in inputs)
                        DropdownMenuItem(value: i['id'] as String, child: Text(i['name'] as String? ?? '${i['id']}')),
                    ],
                    onChanged: (v) => v == null ? null : _set({'audio': {'source': v}}),
                  )),
              _Row('Preview quality', 'Shared by every viewer',
                  _seg<String>({'low': '360p', 'medium': '720p', 'high': '1080p'}, asMap(st['preview'])['quality'] as String?,
                      (v) => _set({'preview': {'quality': v}}))),
              _Row('EDID', 'What the board tells the source it supports',
                  _seg<String>({'4k60': '4K60', '4k30': '4K30', '1080p': '1080p', 'keep': 'Keep'}, st['edid'] as String?,
                      (v) => _set({'edid': v})),
                  stacked: true),
            ]),
            ..._slate(st, rec),
            ..._gpio(st, rec),
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
              // REC-12
              _SwitchRow('Storage guard', 'Refuse a take the measured disk cannot keep up with (you can still record anyway)',
                  st['storage_guard'] != false, (v) => _set({'storage_guard': v})),
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

  // ----------------------------------------------------------- monitoring (REC-13, REC-14)
  List<Widget> _monitoring(Map<String, dynamic> st, Map<String, dynamic> rec) {
    final m = asMap(st['monitor']);
    final status = asMap(rec['monitor']);
    final luts = asList(status['luts']);
    final lut = m['lut'] as String? ?? 'off';
    final userLut = luts.where((l) => l['id'] == lut && l['builtin'] != true).firstOrNull;
    final level = (_zebra ?? ((m['zebra_level'] as num?) ?? 95).toDouble()).clamp(50.0, 109.0);
    final bands = asList(status['bands']);
    return [
      const _Group('Monitoring'),
      Padding(
        padding: const EdgeInsets.fromLTRB(4, 0, 4, 8),
        child: Text('On the live picture, the same for every viewer. Never in the recording.',
            style: Theme.of(context).textTheme.bodySmall),
      ),
      _Card([
        _Row('Frame guides', 'Where the picture will be cut',
            DropdownButton<String>(
              key: const Key('mon-guides'),
              value: _guides.containsKey(m['guides']) ? m['guides'] as String : 'off',
              items: [for (final e in _guides.entries) DropdownMenuItem(value: e.key, child: Text(e.value))],
              onChanged: (v) => v == null ? null : _set({'monitor': {'guides': v}}),
            )),
        _SwitchRow('Safe areas', 'Action 93 % · title 90 %', m['safe'] == true, (v) => _set({'monitor': {'safe': v}}),
            key: const Key('mon-safe')),
        _SwitchRow('Centre cross', null, m['center'] == true, (v) => _set({'monitor': {'center': v}}),
            key: const Key('mon-center')),
        _SwitchRow('Zebras', 'Stripes where the picture is at or over the level', m['zebra'] == true,
            (v) => _set({'monitor': {'zebra': v}}), key: const Key('mon-zebra')),
        if (m['zebra'] == true)
          _Row('Zebra level', '${level.round()} %',
              Slider(
                key: const Key('mon-zebra-level'),
                value: level,
                min: 50,
                max: 109,
                divisions: 59,
                onChanged: (v) => setState(() => _zebra = v),
                onChangeEnd: (v) async {
                  await _set({'monitor': {'zebra_level': v.round()}});
                  if (mounted) setState(() => _zebra = null);
                },
              ),
              stacked: true),
        _SwitchRow('False colour', 'Exposure as colours', m['false_color'] == true,
            (v) => _set({'monitor': {'false_color': v}}), key: const Key('mon-false-colour')),
        if (m['false_color'] == true && bands.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Wrap(spacing: 10, runSpacing: 6, children: [
              for (final b in bands)
                Row(mainAxisSize: MainAxisSize.min, children: [
                  Container(
                    width: 12,
                    height: 12,
                    decoration: BoxDecoration(color: _colour(b['colour']), borderRadius: BorderRadius.circular(3)),
                  ),
                  const SizedBox(width: 5),
                  Text('${b['meaning']}', style: Theme.of(context).textTheme.bodySmall),
                ]),
            ]),
          ),
        _Row('Scope', 'Luma of the full captured frame',
            _seg<String>({'off': 'Off', 'histogram': 'Histogram', 'waveform': 'Waveform'}, m['scope'] as String?,
                (v) => _set({'monitor': {'scope': v}}))),
        _SwitchRow('Audio meters', 'Peak and RMS per channel', m['meters'] != false,
            (v) => _set({'monitor': {'meters': v}}), key: const Key('mon-meters')),
        _Row('Focus loupe', 'Tap the live picture: a magnifying glass shows that spot this many times larger, '
            'from the full-resolution picture',
            _seg<int>({2: '2×', 3: '3×', 4: '4×', 6: '6×', 8: '8×'}, (m['zoom'] as num?)?.toInt(),
                (v) => _set({'monitor': {'zoom': v}}))),
        _Row('LUT', 'Shows log footage as normal video (preview only)',
            Row(mainAxisSize: MainAxisSize.min, children: [
              DropdownButton<String>(
                key: const Key('mon-lut'),
                value: lut == 'off' || luts.any((l) => l['id'] == lut) ? lut : 'off',
                items: [
                  const DropdownMenuItem(value: 'off', child: Text('Off')),
                  for (final l in luts)
                    DropdownMenuItem(value: l['id'] as String, child: Text(l['name'] as String? ?? '${l['id']}')),
                ],
                onChanged: (v) => v == null ? null : _set({'monitor': {'lut': v}}),
              ),
              if (userLut != null)
                IconButton(
                  tooltip: 'Delete this LUT',
                  icon: const Icon(Icons.delete_outline),
                  onPressed: () async {
                    if (!await confirm(context, 'Delete the LUT?', '${userLut['name']} is removed from the Pi.')) return;
                    if (mounted) await runOp(context, client, 'recorder.lut.delete', {'lut': userLut['id']}, 'LUT deleted');
                  },
                ),
            ]),
            stacked: true),
        if (status['lut_error'] != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Text('${status['lut_error']}', style: Theme.of(context).textTheme.bodySmall?.copyWith(color: kWarn)),
          ),
      ]),
    ];
  }

  // ------------------------------------------------------------------- slate (REC-15)
  List<Widget> _slate(Map<String, dynamic> st, Map<String, dynamic> rec) {
    final sl = asMap(st['slate']);
    final next = asMap(rec['take'])['next'];
    return [
      const _Group('Slate'),
      _Card([
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 10),
          child: Row(children: [
            Expanded(
              child: _TextSetting(
                key: const Key('slate-scene'),
                label: 'Scene',
                value: '${sl['scene'] ?? ''}',
                onSave: (v) => _set({'slate': {'scene': v}}),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _TextSetting(
                key: const Key('slate-shot'),
                label: 'Shot',
                value: '${sl['shot'] ?? ''}',
                onSave: (v) => _set({'slate': {'shot': v}}),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _TextSetting(
                key: const Key('slate-take'),
                label: 'Take',
                value: '${sl['take'] ?? 1}',
                number: true,
                onSave: (v) async {
                  final n = int.tryParse(v);
                  if (n != null) await _set({'slate': {'take': n}});
                },
              ),
            ),
          ]),
        ),
        _SwitchRow('Count up after each take',
            'The take number goes up by one when a take ends${next != null ? '\nNext take: $next' : ''}',
            sl['auto'] != false, (v) => _set({'slate': {'auto': v}}), key: const Key('slate-auto')),
      ]),
    ];
  }

  // ------------------------------------------------------ record button & tally (REC-16)
  List<Widget> _gpio(Map<String, dynamic> st, Map<String, dynamic> rec) {
    final g = asMap(st['gpio']);
    final status = asMap(rec['gpio']);
    final t = Theme.of(context).textTheme;
    Widget state(String which) {
      final s = asMap(status[which]);
      final (text, colour) = status['error'] != null && s.isEmpty
          ? ('${status['error']}', kWarn)
          : s['error'] != null
              ? ('${s['error']}', kRec)
              : s['ok'] == true
                  ? (which == 'button'
                      ? 'Ready${(s['presses'] ?? 0) > 0 ? ' · ${s['presses']} presses' : ''}'
                      : 'Ready · ${s['on'] == true ? 'on' : 'off'}', kOk)
                  : ('Not used', null);
      return Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(text, key: Key('gpio-$which-state'), style: t.bodySmall?.copyWith(color: colour)),
      );
    }

    Widget line(String which, String label) => Padding(
          padding: const EdgeInsets.only(top: 10, bottom: 4),
          child: Row(children: [
            Expanded(
              child: _TextSetting(
                key: Key('gpio-$which'),
                label: label,
                hint: 'gpiochip1:15 - empty = none',
                value: '${g[which] ?? ''}',
                onSave: (v) => _set({'gpio': {which: v}}),
              ),
            ),
            IconButton(
              tooltip: 'Choose a header pin',
              icon: const Icon(Icons.pin_outlined),
              onPressed: () async {
                final pin = await _pickPin(context, client);
                if (pin != null) await _set({'gpio': {which: pin}});
              },
            ),
          ]),
        );

    return [
      const _Group('Record button & tally'),
      _Card([
        Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          line('button', 'Record button'),
          Text('A GPIO input: a push button to ground starts or stops recording', style: t.bodySmall),
          state('button'),
        ]),
        _SwitchRow('Button active low', 'A push button to ground (pulled up)', g['button_active_low'] != false,
            (v) => _set({'gpio': {'button_active_low': v}})),
        Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          line('tally', 'Tally light'),
          Text('A GPIO output: on while recording, blinks while a take is finished', style: t.bodySmall),
          state('tally'),
        ]),
        _SwitchRow('Tally active low', 'The light is on when the line is low', g['tally_active_low'] == true,
            (v) => _set({'gpio': {'tally_active_low': v}})),
      ]),
    ];
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

const _containers = {
  'mov': 'MOV: PCM audio, opens in every editor',
  'mkv': 'MKV: PCM audio, survives a power cut',
  'mp4': 'MP4: AAC audio, plays everywhere',
};

const _guides = {'off': 'Off', '2.39': '2.39:1', '1.85': '1.85:1', '16:9': '16:9', '4:3': '4:3', '1:1': '1:1', '9:16': '9:16'};

Color _colour(Object? hex) {
  final m = RegExp(r'^#?([0-9a-fA-F]{6})$').firstMatch('${hex ?? ''}');
  return m == null ? Colors.grey : Color(0xFF000000 | int.parse(m.group(1)!, radix: 16));
}

/// The board's header pins with a GPIO line (`io.gpio.header`), as `gpiochipN:LINE` (REC-16).
Future<String?> _pickPin(BuildContext context, RemoteClient client) async {
  Map<String, dynamic> h;
  try {
    h = asMap(await client.request('io.gpio.header'));
  } catch (e) {
    if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.toString())));
    return null;
  }
  final pins = [
    for (final p in asList(h['pins']))
      if (p['gpio'] is Map && p['chip'] is String) p,
  ];
  if (!context.mounted) return null;
  if (pins.isEmpty) {
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('No GPIO header known on this board')));
    return null;
  }
  return showDialog<String>(
    context: context,
    builder: (c) => SimpleDialog(
      title: Text(h['board'] != null ? 'Header pins · ${h['board']}' : 'Header pins'),
      children: [
        for (final p in pins)
          () {
            final id = '${(p['chip'] as String).split('/').last}:${asMap(p['gpio'])['line']}';
            final used = asMap(p['state'])['used'] == true || p['held'] != null;
            return SimpleDialogOption(
              onPressed: () => Navigator.pop(c, id),
              child: Text('Pin ${p['pin']} · ${p['name']} · $id${used ? ' · in use' : ''}'),
            );
          }(),
      ],
    ),
  );
}

/// A text setting, saved on Enter or when the field loses focus; it follows the Pi's value
/// while it is not being edited.
class _TextSetting extends StatefulWidget {
  const _TextSetting({super.key, required this.label, required this.value, required this.onSave, this.hint,
                      this.number = false});
  final String label;
  final String value;
  final String? hint;
  final bool number;
  final Future<void> Function(String value) onSave;

  @override
  State<_TextSetting> createState() => _TextSettingState();
}

class _TextSettingState extends State<_TextSetting> {
  late final _c = TextEditingController(text: widget.value);
  final _focus = FocusNode();
  String? _sent;

  @override
  void initState() {
    super.initState();
    _focus.addListener(_onFocus);
  }

  void _onFocus() {
    if (!_focus.hasFocus) _save();
  }

  @override
  void didUpdateWidget(_TextSetting old) {
    super.didUpdateWidget(old);
    if (old.value != widget.value) _sent = null;
    if (!_focus.hasFocus && _c.text != widget.value) _c.text = widget.value;
  }

  @override
  void dispose() {
    _focus.removeListener(_onFocus);
    // the sheet closed while this was edited: still save it, once the sheet has gone
    final v = _c.text.trim();
    if (_focus.hasFocus && v != widget.value && v != _sent) {
      final save = widget.onSave;
      scheduleMicrotask(() => save(v));
    }
    _c.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _save() {
    final v = _c.text.trim();
    if (v == widget.value || v == _sent) return;
    _sent = v;
    widget.onSave(v);
  }

  @override
  Widget build(BuildContext context) => TextField(
        controller: _c,
        focusNode: _focus,
        keyboardType: widget.number ? TextInputType.number : TextInputType.text,
        textInputAction: TextInputAction.done,
        decoration: InputDecoration(labelText: widget.label, hintText: widget.hint, isDense: true),
        onSubmitted: (_) => _save(),
      );
}

/// Which copy sub-sections are open (view state, kept while the app runs).
final Set<String> _openCopies = {};

String _cap(Object? q) {
  final s = q is String ? q : 'high';
  return s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);
}

/// One kind of extra copy (REC-04): chevron, name, what it is (or its options when on) and its
/// switch; tapping the header shows / hides [children]. Switching it on opens it.
class _CopySection extends StatefulWidget {
  const _CopySection({
    required this.id,
    required this.title,
    required this.what,
    required this.summary,
    required this.on,
    required this.onChanged,
    required this.open,
    required this.children,
  });
  final String id, title, what, summary;
  final bool on;
  final ValueChanged<bool>? onChanged;
  final Set<String> open;
  final List<Widget> children;

  @override
  State<_CopySection> createState() => _CopySectionState();
}

class _CopySectionState extends State<_CopySection> {
  bool get _open => widget.open.contains(widget.id);

  void _flip() => setState(() => _open ? widget.open.remove(widget.id) : widget.open.add(widget.id));

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final scheme = Theme.of(context).colorScheme;
    final onChanged = widget.onChanged;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Semantics(
        button: true,
        expanded: _open,
        child: InkWell(
          key: Key('copy-${widget.id}'),
          onTap: _flip,
          borderRadius: BorderRadius.circular(10),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Row(children: [
              AnimatedRotation(
                turns: _open ? 0 : -0.25,
                duration: const Duration(milliseconds: 180),
                child: Icon(Icons.expand_more, color: scheme.onSurfaceVariant),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(widget.title, style: t.bodyLarge?.copyWith(fontWeight: FontWeight.w600)),
                  Text(widget.on ? widget.summary : widget.what,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: t.bodySmall?.copyWith(color: widget.on ? scheme.onSurface : scheme.onSurfaceVariant)),
                ]),
              ),
              Switch(
                value: widget.on,
                onChanged: onChanged == null
                    ? null
                    : (v) {
                        if (v) setState(() => widget.open.add(widget.id));
                        onChanged(v);
                      },
              ),
            ]),
          ),
        ),
      ),
      AnimatedSize(
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
        alignment: Alignment.topCenter,
        child: _open
            ? Padding(
                padding: const EdgeInsets.only(left: 32, bottom: 4),
                child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  for (var i = 0; i < widget.children.length; i++) ...[
                    if (i > 0) Divider(height: 1, color: scheme.outlineVariant.withValues(alpha: 0.4)),
                    widget.children[i],
                  ],
                ]),
              )
            : const SizedBox(width: double.infinity),
      ),
    ]);
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
  const _SwitchRow(this.title, this.sub, this.value, this.onChanged, {super.key});
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
