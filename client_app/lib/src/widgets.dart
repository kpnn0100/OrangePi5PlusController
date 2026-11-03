import 'package:flutter/material.dart';

import 'theme.dart';

// The parts of the Arstro look (docs/design.md) the pages are made of: flat cards with a 1 px border
// and an overline title, lists as hairline-separated rows - the same pieces as Arstro Devices
// (devices_app/lib/src/widgets.dart) and the device manager's web page.

/// Small uppercase section label (11.5 px, 500, letter-spacing .06em, muted).
class Overline extends StatelessWidget {
  const Overline(this.text, {super.key, this.padding = EdgeInsets.zero});
  final String text;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) =>
      Padding(padding: padding, child: Text(text.toUpperCase(), style: Arstro.overline));
}

/// Flat card: `surface`, 1 px `line`, radius 10, an overline title (with an optional action on its
/// right) and the content under it.
class SectionCard extends StatelessWidget {
  const SectionCard({super.key, required this.title, required this.child, this.trailing, this.padding});

  final String title;
  final Widget child;
  final Widget? trailing;
  final EdgeInsetsGeometry? padding;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: Arstro.surface,
        border: Border.all(color: Arstro.line),
        borderRadius: BorderRadius.circular(Arstro.rCard),
      ),
      padding: padding ?? const EdgeInsets.all(16),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
        SizedBox(
          height: trailing == null ? null : 28,
          child: Row(children: [
            Expanded(child: Text(title.toUpperCase(), style: Arstro.overline)),
            ?trailing,
          ]),
        ),
        const SizedBox(height: 12),
        child,
      ]),
    );
  }
}

/// Rows separated by hairlines inside one bordered container (a list, not a stack of cards).
class RowGroup extends StatelessWidget {
  const RowGroup({super.key, required this.children, this.radius = Arstro.rCard, this.color = Arstro.surface});
  final List<Widget> children;
  final double radius;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: color,
        border: Border.all(color: Arstro.line),
        borderRadius: BorderRadius.circular(radius),
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(radius - 1),
        child: Material(
          type: MaterialType.transparency,
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            for (var i = 0; i < children.length; i++) ...[if (i > 0) const Divider(), children[i]],
          ]),
        ),
      ),
    );
  }
}

/// A label and its value on one line (a list of facts: label muted, value on the right).
class KeyValueRow extends StatelessWidget {
  const KeyValueRow(this.k, this.v, {super.key, this.mono = false, this.selectable = false});
  final String k;
  final String v;
  final bool mono;
  final bool selectable;

  @override
  Widget build(BuildContext context) {
    final style = mono ? Arstro.monoStyle(size: 13) : const TextStyle(fontSize: 14, color: Arstro.text, height: 1.4);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 9),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(
          width: 104,
          child: Text(k, style: const TextStyle(fontSize: 13.5, color: Arstro.muted, height: 1.45)),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Align(
            alignment: Alignment.centerRight,
            child: selectable
                ? SelectableText(v, style: style, textAlign: TextAlign.right)
                : Text(v, style: style, textAlign: TextAlign.right),
          ),
        ),
      ]),
    );
  }
}

/// A label above an input (13 px, 500, text-2), like the web forms.
class FieldLabel extends StatelessWidget {
  const FieldLabel(this.text, {super.key});
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(text, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500, color: Arstro.text2)),
      );
}

/// Placeholder shown while there is no data yet: a quiet bordered box, the first line in text-2,
/// the rest muted.
class EmptyState extends StatelessWidget {
  const EmptyState({super.key, required this.icon, required this.text, this.action});
  final IconData icon;
  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final nl = text.indexOf('\n');
    final first = nl < 0 ? text : text.substring(0, nl);
    final rest = nl < 0 ? '' : text.substring(nl);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 36),
      decoration: BoxDecoration(
        border: Border.all(color: Arstro.line2),
        borderRadius: BorderRadius.circular(Arstro.rCard),
      ),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        Icon(icon, size: 24, color: Arstro.muted),
        const SizedBox(height: 12),
        Text.rich(
          TextSpan(children: [
            TextSpan(text: first, style: const TextStyle(color: Arstro.text2, fontWeight: FontWeight.w500)),
            if (rest.isNotEmpty) TextSpan(text: rest, style: const TextStyle(color: Arstro.muted, fontSize: 13.5)),
          ]),
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 14.5, height: 1.55),
        ),
        if (action != null) ...[const SizedBox(height: 16), action!],
      ]),
    );
  }
}

/// A row with a title (and a line under it) and a switch on the right; tapping the row toggles it.
class SwitchRow extends StatelessWidget {
  const SwitchRow({super.key, required this.title, this.subtitle, required this.value, required this.onChanged});
  final Widget title;
  final Widget? subtitle;
  final bool value;
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onChanged == null ? null : () => onChanged!(!value),
      borderRadius: BorderRadius.circular(Arstro.rButton),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              DefaultTextStyle.merge(
                  style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w500, color: Arstro.text, height: 1.35),
                  child: title),
              if (subtitle != null) ...[
                const SizedBox(height: 2),
                DefaultTextStyle.merge(style: const TextStyle(fontSize: 13, color: Arstro.muted, height: 1.45), child: subtitle!),
              ],
            ]),
          ),
          const SizedBox(width: 16),
          Switch(value: value, onChanged: onChanged),
        ]),
      ),
    );
  }
}

/// The Arstro mark: an "A" in `on-primary` on a `primary` rounded square (no gradient).
class ArstroMark extends StatelessWidget {
  const ArstroMark({super.key, this.size = 26});
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(color: Arstro.primary, borderRadius: BorderRadius.circular(size * 0.27)),
      child: CustomPaint(painter: _MarkPainter()),
    );
  }
}

class _MarkPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    // the web logo's path on a 32 x 32 grid: M9 22 16 8l7 14 M11.5 17h9
    final k = size.width / 32;
    final p = Paint()
      ..color = Arstro.onPrimary
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.4 * k
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    canvas.drawPath(
        Path()
          ..moveTo(9 * k, 22 * k)
          ..lineTo(16 * k, 8 * k)
          ..lineTo(23 * k, 22 * k)
          ..moveTo(11.5 * k, 17 * k)
          ..lineTo(20.5 * k, 17 * k),
        p);
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
