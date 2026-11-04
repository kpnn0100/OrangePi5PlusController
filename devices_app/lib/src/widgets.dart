import 'package:flutter/material.dart';

import 'theme.dart';

// The parts of the Arstro look (docs/design.md) the pages are made of: flat cards with a 1 px border
// and an overline title, lists as hairline-separated rows in one bordered container, status dots and
// pills - the same pieces as the device manager's web page (relay/arstro_relay/static/style.css).

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

/// A light error banner: err-soft background, err text, an action on the right.
class ErrorBanner extends StatelessWidget {
  const ErrorBanner({super.key, required this.icon, required this.text, this.action});
  final IconData icon;
  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: EdgeInsets.fromLTRB(14, 10, action == null ? 14 : 6, 10),
      decoration: BoxDecoration(
        color: Arstro.errSoft,
        border: Border.all(color: const Color(0x2EEC7E76)),
        borderRadius: BorderRadius.circular(Arstro.rButton),
      ),
      child: Row(children: [
        Icon(icon, size: 18, color: Arstro.err),
        const SizedBox(width: 10),
        Expanded(child: Text(text, style: const TextStyle(color: Arstro.err, fontSize: 13.5, height: 1.4))),
        ?action,
      ]),
    );
  }
}

/// "now", "5 min ago", "3 h ago", "2 d ago".
String ago(DateTime t, {DateTime? now}) {
  final d = (now ?? DateTime.now()).difference(t);
  if (d.inSeconds < 60) return 'just now';
  if (d.inMinutes < 60) return '${d.inMinutes} min ago';
  if (d.inHours < 24) return '${d.inHours} h ago';
  return '${d.inDays} d ago';
}

/// "5 min", "3 h", "2 d" (how long something has been so).
String span(DateTime t, {DateTime? now}) {
  final d = (now ?? DateTime.now()).difference(t);
  if (d.inMinutes < 1) return 'a moment';
  if (d.inMinutes < 60) return '${d.inMinutes} min';
  if (d.inHours < 24) return '${d.inHours} h';
  return '${d.inDays} d';
}

/// 7-8 px status dot: online `ok` with a 3 px `ok-soft` ring, offline `muted`.
class StatusDot extends StatelessWidget {
  const StatusDot({super.key, required this.online, this.size = 7});
  final bool online;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: online ? Arstro.ok : Arstro.muted,
        boxShadow: online ? const [BoxShadow(color: Arstro.okSoft, spreadRadius: 3)] : null,
      ),
    );
  }
}

/// Round status pill with a dot: "online" (ok on ok-soft) or "offline" (muted on surface-3); a machine
/// with only some of its devices online: "1 of 2 online" (warn on warn-soft).
class StatusPill extends StatelessWidget {
  const StatusPill({super.key, required this.online, this.of});
  final bool online;
  final (int, int)? of;                                  // (online, all) of a machine's devices

  /// The pill of a machine: online, offline, or how many of its devices are online.
  factory StatusPill.machine(int online, int all, {Key? key}) =>
      StatusPill(key: key, online: online > 0, of: online > 0 && online < all ? (online, all) : null);

  @override
  Widget build(BuildContext context) {
    final part = of != null;
    final color = part ? Arstro.warn : (online ? Arstro.ok : Arstro.muted);
    return Container(
      padding: const EdgeInsets.fromLTRB(8, 3, 9, 3),
      decoration: BoxDecoration(
        color: part ? Arstro.warnSoft : (online ? Arstro.okSoft : Arstro.surface3),
        borderRadius: BorderRadius.circular(99),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Container(width: 7, height: 7, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
        const SizedBox(width: 7),
        Text(part ? '${of!.$1} of ${of!.$2} online' : (online ? 'online' : 'offline'),
            style: TextStyle(fontSize: 12, fontWeight: FontWeight.w500, color: color, height: 1.3)),
      ]),
    );
  }
}

/// A small label next to a name ("slot A"): surface-3, 6 px radius, text-2.
class Tag extends StatelessWidget {
  const Tag(this.text, {super.key});
  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(color: Arstro.surface3, borderRadius: BorderRadius.circular(Arstro.rChip)),
      child: Text(text, style: const TextStyle(fontSize: 11.5, fontWeight: FontWeight.w500, color: Arstro.text2, height: 1.4)),
    );
  }
}

/// A device's kind icon in a small bordered box (the web list's `.kind`); dimmer while offline.
class KindBox extends StatelessWidget {
  const KindBox({super.key, required this.icon, this.online = true, this.size = 36});
  final IconData icon;
  final bool online;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: online ? Arstro.surface2 : Colors.transparent,
        border: Border.all(color: Arstro.line),
        borderRadius: BorderRadius.circular(Arstro.rButton),
      ),
      child: Icon(icon, size: size * 0.5, color: online ? Arstro.text2 : Arstro.muted),
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
