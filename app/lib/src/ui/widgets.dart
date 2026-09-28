import 'dart:math';

import 'package:flutter/material.dart';

/// Card with a small uppercase title row.
class SectionCard extends StatelessWidget {
  const SectionCard({super.key, required this.title, required this.child, this.icon, this.trailing, this.padding});

  final String title;
  final IconData? icon;
  final Widget child;
  final Widget? trailing;
  final EdgeInsetsGeometry? padding;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: padding ?? const EdgeInsets.fromLTRB(16, 14, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(children: [
              if (icon != null) ...[Icon(icon, size: 18, color: scheme.primary), const SizedBox(width: 8)],
              Expanded(
                child: Text(title.toUpperCase(),
                    style: Theme.of(context).textTheme.labelMedium?.copyWith(
                          letterSpacing: 1.1,
                          color: scheme.onSurfaceVariant,
                          fontWeight: FontWeight.w600,
                        )),
              ),
              ?trailing,
            ]),
            const SizedBox(height: 12),
            child,
          ],
        ),
      ),
    );
  }
}

/// Ring gauge with a value in the middle.
class RingGauge extends StatelessWidget {
  const RingGauge({
    super.key,
    required this.value,
    required this.label,
    required this.center,
    required this.color,
    this.size = 108,
    this.sublabel,
  });

  final double value; // 0..1
  final String label;
  final String center;
  final String? sublabel;
  final Color color;
  final double size;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Semantics(
      label: '$label $center',
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        SizedBox(
          width: size,
          height: size,
          child: CustomPaint(
            painter: _RingPainter(value.clamp(0, 1).toDouble(), color, theme.colorScheme.surfaceContainerHighest),
            child: Padding(
              // keep the text inside the ring
              padding: EdgeInsets.symmetric(horizontal: size * 0.17),
              child: Center(
                child: FittedBox(
                  fit: BoxFit.scaleDown,
                  child: Column(mainAxisSize: MainAxisSize.min, children: [
                    Text(center,
                        style:
                            theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w700, fontSize: size * 0.19)),
                    if (sublabel != null)
                      Text(sublabel!,
                          style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
                  ]),
                ),
              ),
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(label, style: theme.textTheme.labelLarge),
      ]),
    );
  }
}

class _RingPainter extends CustomPainter {
  _RingPainter(this.value, this.color, this.track);
  final double value;
  final Color color;
  final Color track;

  @override
  void paint(Canvas canvas, Size size) {
    final stroke = size.width * 0.09;
    final rect = Offset(stroke / 2, stroke / 2) & Size(size.width - stroke, size.height - stroke);
    const start = pi * 0.75;
    const sweep = pi * 1.5;
    final bg = Paint()
      ..color = track
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke
      ..strokeCap = StrokeCap.round;
    final fg = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke
      ..strokeCap = StrokeCap.round;
    canvas.drawArc(rect, start, sweep, false, bg);
    if (value > 0) canvas.drawArc(rect, start, sweep * value, false, fg);
  }

  @override
  bool shouldRepaint(_RingPainter old) => old.value != value || old.color != color || old.track != track;
}

/// Simple line chart for the CPU / temperature history.
class Sparkline extends StatelessWidget {
  const Sparkline({super.key, required this.values, required this.color, this.min = 0, this.max = 100, this.height = 64});

  final List<double> values;
  final Color color;
  final double min;
  final double max;
  final double height;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: height,
      width: double.infinity,
      child: CustomPaint(painter: _SparkPainter(List.of(values), color, min, max,
          Theme.of(context).colorScheme.outlineVariant.withValues(alpha: 0.4))),
    );
  }
}

class _SparkPainter extends CustomPainter {
  _SparkPainter(this.values, this.color, this.min, this.max, this.grid);
  final List<double> values;
  final Color color;
  final double min, max;
  final Color grid;

  @override
  void paint(Canvas canvas, Size size) {
    final gp = Paint()
      ..color = grid
      ..strokeWidth = 1;
    for (var i = 0; i <= 2; i++) {
      final y = size.height * i / 2;
      canvas.drawLine(Offset(0, y), Offset(size.width, y), gp);
    }
    if (values.length < 2) return;
    const n = 90;
    final dx = size.width / (n - 1);
    final offset = n - values.length;
    Offset pt(int i) {
      final v = ((values[i] - min) / (max - min)).clamp(0.0, 1.0);
      return Offset((i + offset) * dx, size.height - v * size.height);
    }

    final path = Path()..moveTo(pt(0).dx, pt(0).dy);
    for (var i = 1; i < values.length; i++) {
      path.lineTo(pt(i).dx, pt(i).dy);
    }
    final fill = Path.from(path)
      ..lineTo(pt(values.length - 1).dx, size.height)
      ..lineTo(pt(0).dx, size.height)
      ..close();
    canvas.drawPath(
        fill,
        Paint()
          ..shader = LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [color.withValues(alpha: 0.35), color.withValues(alpha: 0.02)],
          ).createShader(Offset.zero & size));
    canvas.drawPath(
        path,
        Paint()
          ..color = color
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2
          ..strokeJoin = StrokeJoin.round);
  }

  @override
  bool shouldRepaint(_SparkPainter old) => true;
}

/// Thin horizontal bar with label and value.
class LabeledBar extends StatelessWidget {
  const LabeledBar({super.key, required this.label, required this.value, required this.text, required this.color});

  final String label;
  final double value;
  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(children: [
        SizedBox(width: 92, child: Text(label, style: theme.textTheme.bodyMedium, overflow: TextOverflow.ellipsis)),
        Expanded(
          child: ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: LinearProgressIndicator(
              value: value.clamp(0, 1).toDouble(),
              minHeight: 8,
              color: color,
              backgroundColor: theme.colorScheme.surfaceContainerHighest,
            ),
          ),
        ),
        const SizedBox(width: 10),
        SizedBox(
            width: 64,
            child: Text(text,
                textAlign: TextAlign.right,
                style: theme.textTheme.bodyMedium?.copyWith(fontFeatures: const [FontFeature.tabularFigures()]))),
      ]),
    );
  }
}

class KeyValueRow extends StatelessWidget {
  const KeyValueRow(this.k, this.v, {super.key, this.mono = false, this.selectable = false});
  final String k;
  final String v;
  final bool mono;
  final bool selectable;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final style = theme.textTheme.bodyMedium?.copyWith(
      fontFamily: mono ? 'monospace' : null,
      fontWeight: FontWeight.w500,
    );
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(
            width: 110,
            child: Text(k, style: theme.textTheme.bodyMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant))),
        Expanded(child: selectable ? SelectableText(v, style: style) : Text(v, style: style)),
      ]),
    );
  }
}

/// Placeholder shown while there is no data yet.
class EmptyState extends StatelessWidget {
  const EmptyState({super.key, required this.icon, required this.text, this.action});
  final IconData icon;
  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 48, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(height: 12),
          Text(text, textAlign: TextAlign.center, style: theme.textTheme.bodyLarge),
          if (action != null) ...[const SizedBox(height: 16), action!],
        ]),
      ),
    );
  }
}
