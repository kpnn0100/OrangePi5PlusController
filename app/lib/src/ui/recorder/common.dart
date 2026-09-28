import 'package:flutter/material.dart';

import '../../remote_client.dart';

const kRec = Color(0xFFFF4D5E);
const kOk = Color(0xFF3DDC97);
const kWarn = Color(0xFFF5B841);

/// Runs an op; a failure (or `ok`) becomes a snack bar. Returns the data or null.
Future<dynamic> runOp(BuildContext context, RemoteClient client, String op,
    [Map<String, dynamic> args = const {}, String? ok, Duration? timeout]) async {
  final messenger = ScaffoldMessenger.maybeOf(context);
  try {
    final r = await client.request(op, args, timeout ?? const Duration(seconds: 60));
    if (ok != null) messenger?.showSnackBar(SnackBar(content: Text(ok), duration: const Duration(seconds: 2)));
    return r ?? const {};
  } catch (e) {
    messenger?.showSnackBar(SnackBar(content: Text(e.toString()), backgroundColor: const Color(0xFF5A2330)));
    return null;
  }
}

Future<bool> confirm(BuildContext context, String title, String text, {String ok = 'Delete', bool danger = true}) async {
  final r = await showDialog<bool>(
    context: context,
    builder: (c) => AlertDialog(
      title: Text(title),
      content: Text(text),
      actions: [
        TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
        FilledButton(
          style: danger ? FilledButton.styleFrom(backgroundColor: kRec, foregroundColor: Colors.white) : null,
          onPressed: () => Navigator.pop(c, true),
          child: Text(ok),
        ),
      ],
    ),
  );
  return r ?? false;
}

/// 83 -> "1:23", 3723 -> "1:02:03".
String clock(num? seconds) {
  if (seconds == null) return '–';
  final s = seconds.floor().clamp(0, 1 << 31);
  final h = s ~/ 3600, m = (s ~/ 60) % 60, ss = s % 60;
  String two(int v) => v.toString().padLeft(2, '0');
  return h > 0 ? '$h:${two(m)}:${two(ss)}' : '$m:${two(ss)}';
}

/// Decimal sizes like the Pi's gallery (1 GB = 10^9 bytes).
String size(num? bytes) {
  if (bytes == null) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  var v = bytes.toDouble();
  var i = 0;
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000;
    i++;
  }
  return '${i == 0 ? v.toStringAsFixed(0) : v.toStringAsFixed(v >= 100 ? 0 : 1)} ${u[i]}';
}

String mbit(num? bytesPerSec) {
  if (bytesPerSec == null || bytesPerSec == 0) return '0 Mb/s';
  final v = bytesPerSec * 8 / 1e6;
  return '${v >= 100 ? v.toStringAsFixed(0) : v.toStringAsFixed(1)} Mb/s';
}

Map<String, dynamic> asMap(dynamic v) => v is Map ? Map<String, dynamic>.from(v) : <String, dynamic>{};
List<Map<String, dynamic>> asList(dynamic v) =>
    v is List ? [for (final e in v) if (e is Map) Map<String, dynamic>.from(e)] : const [];

/// Small rounded label ("H.265", "REC", ...).
class Tag extends StatelessWidget {
  const Tag(this.text, {super.key, this.color, this.dot = false});
  final String text;
  final Color? color;
  final bool dot;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final c = color ?? scheme.onSurfaceVariant;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
      decoration: BoxDecoration(
        color: color == null ? scheme.surfaceContainerHighest : c.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (dot) ...[
          Container(width: 6, height: 6, decoration: BoxDecoration(color: c, shape: BoxShape.circle)),
          const SizedBox(width: 5),
        ],
        Text(text, style: TextStyle(color: c, fontSize: 11.5, fontWeight: FontWeight.w700, letterSpacing: 0.2)),
      ]),
    );
  }
}

Color? kindColor(String kind) => switch (kind) {
      'H.265' => const Color(0xFF9DB4FF),
      'H.264' => kOk,
      'FFV1' => kWarn,
      _ => null,
    };

String kindLabel(String kind) => kind == 'VIDEO' ? 'Video' : kind;

/// Label + value pair used in the status grids.
class Metric extends StatelessWidget {
  const Metric(this.label, this.value, {super.key, this.sub});
  final String label;
  final String value;
  final String? sub;

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context).textTheme;
    final muted = Theme.of(context).colorScheme.onSurfaceVariant;
    return Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
      Text(label, style: t.labelMedium?.copyWith(color: muted)),
      const SizedBox(height: 2),
      Text.rich(
        TextSpan(text: value, children: [
          if (sub != null) TextSpan(text: '  $sub', style: t.bodySmall?.copyWith(color: muted)),
        ]),
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: t.titleMedium?.copyWith(fontWeight: FontWeight.w600, fontFeatures: const [FontFeature.tabularFigures()]),
      ),
    ]);
  }
}

/// Grid of [Metric]s that wraps to the width.
class MetricGrid extends StatelessWidget {
  const MetricGrid(this.items, {super.key});
  final List<Widget> items;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, c) {
      final cols = c.maxWidth > 420 ? 4 : 2;
      final w = (c.maxWidth - (cols - 1) * 14) / cols;
      return Wrap(spacing: 14, runSpacing: 14, children: [for (final i in items) SizedBox(width: w, child: i)]);
    });
  }
}
