import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'common.dart';

/// The record button (UX-02): a red dot that morphs into a rounded stop square while
/// recording, with a soft pulse ring.
class RecordButton extends StatefulWidget {
  const RecordButton({super.key, required this.recording, required this.busy, this.onPressed});

  final bool recording;
  final bool busy;
  final VoidCallback? onPressed;

  @override
  State<RecordButton> createState() => _RecordButtonState();
}

class _RecordButtonState extends State<RecordButton> with SingleTickerProviderStateMixin {
  late final AnimationController _pulse =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 1800));

  @override
  void initState() {
    super.initState();
    if (widget.recording) _pulse.repeat();
  }

  @override
  void didUpdateWidget(RecordButton old) {
    super.didUpdateWidget(old);
    if (widget.recording && !_pulse.isAnimating) _pulse.repeat();
    if (!widget.recording && _pulse.isAnimating) _pulse.stop();
  }

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final on = widget.recording;
    final enabled = widget.onPressed != null && !widget.busy;
    const curve = Curves.easeOutCubic;
    const t = Duration(milliseconds: 280);
    return Semantics(
      button: true,
      label: on ? 'Stop recording' : 'Start recording',
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 200),
        opacity: enabled || widget.busy ? 1 : 0.35,
        child: GestureDetector(
          onTap: enabled
              ? () {
                  HapticFeedback.mediumImpact();
                  widget.onPressed!();
                }
              : null,
          child: SizedBox(
            width: 104,
            height: 104,
            child: Stack(alignment: Alignment.center, children: [
              if (on)
                AnimatedBuilder(
                  animation: _pulse,
                  builder: (context, _) {
                    final v = Curves.easeOut.transform(_pulse.value);
                    return Transform.scale(
                      scale: 0.92 + 0.26 * v,
                      child: Container(
                        width: 86,
                        height: 86,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          border: Border.all(color: kRec.withValues(alpha: 0.7 * (1 - v)), width: 2),
                        ),
                      ),
                    );
                  },
                ),
              AnimatedContainer(
                duration: t,
                curve: curve,
                width: 84,
                height: 84,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  border: Border.all(color: on ? kRec : Colors.white, width: 3),
                ),
              ),
              AnimatedContainer(
                duration: t,
                curve: curve,
                width: on ? 30 : 62,
                height: on ? 30 : 62,
                decoration: BoxDecoration(
                  color: widget.busy ? Colors.grey : kRec,
                  borderRadius: BorderRadius.circular(on ? 8 : 31),
                ),
              ),
              if (widget.busy)
                const SizedBox(width: 28, height: 28, child: CircularProgressIndicator(strokeWidth: 2.5, color: Colors.white)),
            ]),
          ),
        ),
      ),
    );
  }
}
