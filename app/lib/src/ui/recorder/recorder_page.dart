import 'package:flutter/material.dart';

import '../../app_scope.dart';
import 'common.dart';
import 'gallery_view.dart';
import 'live_view.dart';

/// The Recorder tab: HDMI RX live view + record button, and the gallery.
class RecorderPage extends StatefulWidget {
  const RecorderPage({super.key, required this.active});
  final bool active;

  @override
  State<RecorderPage> createState() => _RecorderPageState();
}

class _RecorderPageState extends State<RecorderPage> {
  int _view = 0;

  @override
  Widget build(BuildContext context) {
    final client = AppScope.of(context).client;
    return Column(children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 8),
        child: ListenableBuilder(
          listenable: Listenable.merge([client.topic('jobs'), client.topic('recorder')]),
          builder: (context, _) {
            final running = asList(client.topic('jobs').value).where((j) => j['state'] == 'running').length;
            final recording = asMap(asMap(client.topic('recorder').value)['recording'])['active'] == true;
            return _Switch(
              index: _view,
              onChanged: (i) => setState(() => _view = i),
              labels: [
                _Label('Live', dot: recording ? kRec : null),
                _Label('Gallery', badge: running > 0 ? '$running' : null),
              ],
            );
          },
        ),
      ),
      Expanded(
        child: IndexedStack(index: _view, children: [
          LiveView(client: client, active: widget.active && _view == 0, onOpenGallery: () => setState(() => _view = 1)),
          GalleryView(client: client, active: widget.active && _view == 1),
        ]),
      ),
    ]);
  }
}

class _Label {
  const _Label(this.text, {this.dot, this.badge});
  final String text;
  final Color? dot;
  final String? badge;
}

/// Pill switch with a sliding thumb.
class _Switch extends StatelessWidget {
  const _Switch({required this.index, required this.onChanged, required this.labels});
  final int index;
  final ValueChanged<int> onChanged;
  final List<_Label> labels;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 420),
        child: Container(
          height: 42,
          padding: const EdgeInsets.all(3),
          decoration: BoxDecoration(
            color: scheme.surfaceContainerLow,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: scheme.outlineVariant.withValues(alpha: 0.5)),
          ),
          child: LayoutBuilder(builder: (context, c) {
            final w = c.maxWidth / labels.length;
            return Stack(children: [
              AnimatedPositioned(
                duration: const Duration(milliseconds: 260),
                curve: Curves.easeOutCubic,
                left: w * index,
                top: 0,
                bottom: 0,
                width: w,
                child: Container(
                  decoration: BoxDecoration(color: scheme.surfaceContainerHighest, borderRadius: BorderRadius.circular(11)),
                ),
              ),
              Row(children: [
                for (var i = 0; i < labels.length; i++)
                  Expanded(
                    child: InkWell(
                      borderRadius: BorderRadius.circular(11),
                      onTap: () => onChanged(i),
                      child: Center(
                        child: Row(mainAxisSize: MainAxisSize.min, children: [
                          Text(labels[i].text,
                              style: TextStyle(
                                fontWeight: FontWeight.w600,
                                color: i == index ? scheme.onSurface : scheme.onSurfaceVariant,
                              )),
                          if (labels[i].dot != null) ...[
                            const SizedBox(width: 6),
                            Container(width: 7, height: 7, decoration: BoxDecoration(color: labels[i].dot, shape: BoxShape.circle)),
                          ],
                          if (labels[i].badge != null) ...[
                            const SizedBox(width: 6),
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
                              decoration: BoxDecoration(color: scheme.primary, borderRadius: BorderRadius.circular(8)),
                              child: Text(labels[i].badge!,
                                  style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: scheme.onPrimary)),
                            ),
                          ],
                        ]),
                      ),
                    ),
                  ),
              ]),
            ]);
          }),
        ),
      ),
    );
  }
}
