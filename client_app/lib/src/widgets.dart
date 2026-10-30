import 'package:flutter/material.dart';

// The cards and rows of Arstro Remote (app/lib/src/ui/widgets.dart), so both apps look the same.

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
