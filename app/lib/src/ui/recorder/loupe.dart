import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import '../../media.dart';

/// The focus loupe (REC-14): a magnifying glass over the live picture. The whole picture stays
/// as it is; the circle shows what it covers, magnified from the full-resolution frame (its own
/// small stream and decoder, see [PreviewController.loupeTo]).

/// The circle's diameter on a picture of [pic] (logical pixels): 40 % of the picture's height,
/// 120..280, never more than fits.
double loupeDiameter(Size pic) {
  final d = (pic.height * 0.4).clamp(120.0, 280.0);
  return math.max(24.0, math.min(d, math.min(pic.width, pic.height) - 2 * loupeMargin));
}

/// How far the circle keeps from the picture's edges.
const loupeMargin = 4.0;

/// Where the circle's centre goes for the point [p] (the finger, in the picture [pic]), the
/// circle [d] across: above the point, clear of the finger; beside it (towards the middle) when
/// there is no room above; below it as the last choice; always inside the picture.
Offset loupeCentre(Size pic, Offset p, double d) {
  const m = loupeMargin;
  final r = d / 2;
  final lift = r + math.max(0.1 * d, 28.0);       // the rim stays >= 28 clear of the point
  double cx(double x) => x.clamp(r + m, math.max(r + m, pic.width - r - m));
  double cy(double y) => y.clamp(r + m, math.max(r + m, pic.height - r - m));
  if (p.dy - lift - r >= m) return Offset(cx(p.dx), p.dy - lift);
  final side = p.dx < pic.width / 2 ? 1.0 : -1.0;
  final sx = p.dx + side * lift;
  if (sx - r >= m && sx + r <= pic.width - m) return Offset(sx, cy(p.dy));
  if (p.dy + lift + r <= pic.height - m) return Offset(cx(p.dx), p.dy + lift);
  return Offset(cx(p.dx), cy(p.dy - lift));
}

/// A pan that wins over the page's scrolling as soon as the finger moves a little: while the
/// loupe is open, a drag on the picture moves the loupe (a still tap stays a tap).
class LoupePanRecognizer extends PanGestureRecognizer {
  LoupePanRecognizer({super.debugOwner});

  @override
  bool hasSufficientGlobalDistanceToAccept(PointerDeviceKind pointerDeviceKind, double? deviceTouchSlop) =>
      globalDistanceMoved.abs() > 4;
}

/// The circle, its "3×" label and × button, and a dot on the exact point - laid over the
/// picture (a [pic]-sized box). Only the × takes touches: the rest lets them through to the
/// picture underneath, which moves the loupe.
class LoupeOverlay extends StatelessWidget {
  const LoupeOverlay({super.key, required this.preview, required this.pic, required this.ratio});
  final PreviewController preview;
  final Size pic;
  final int ratio;

  @override
  Widget build(BuildContext context) {
    final at = preview.loupeAt;
    if (at == null || pic.isEmpty) return const SizedBox.shrink();
    final p = Offset(at.x * pic.width, at.y * pic.height);
    final d = loupeDiameter(pic);
    final r = d / 2;
    final c = loupeCentre(pic, p, d);
    final texture = preview.loupeTexture;
    final diag = r * math.sqrt1_2;                 // the rim at 45°
    return Stack(clipBehavior: Clip.none, children: [
      // the exact point under the finger
      Positioned(
        left: p.dx - 5,
        top: p.dy - 5,
        child: IgnorePointer(
          child: Container(
            key: const Key('loupe-dot'),
            width: 10,
            height: 10,
            decoration: BoxDecoration(
              color: Colors.white,
              shape: BoxShape.circle,
              border: Border.all(color: Colors.black54, width: 1.5),
            ),
          ),
        ),
      ),
      Positioned(
        left: c.dx - r,
        top: c.dy - r,
        width: d,
        height: d,
        child: IgnorePointer(
          child: Container(
            key: const Key('loupe'),
            decoration: const BoxDecoration(
              shape: BoxShape.circle,
              color: Colors.black,
              boxShadow: [BoxShadow(color: Colors.black54, blurRadius: 14, offset: Offset(0, 4))],
            ),
            foregroundDecoration: BoxDecoration(
              shape: BoxShape.circle,
              border: Border.all(color: Colors.white, width: 3),
            ),
            child: ClipOval(
              child: Stack(fit: StackFit.expand, children: [
                if (texture != null) Texture(key: const Key('loupe-texture'), textureId: texture),
                // before its first picture: a quiet sign that it is coming
                if (texture == null || !preview.loupePlaying)
                  const ColoredBox(
                    key: Key('loupe-waiting'),
                    color: Color(0x99000000),
                    child: Center(
                      child: Text('…', style: TextStyle(color: Colors.white70, fontSize: 26, fontWeight: FontWeight.w600)),
                    ),
                  ),
              ]),
            ),
          ),
        ),
      ),
      // the magnification, on the rim at the lower left
      Positioned(
        left: c.dx - diag - 17,
        top: c.dy + diag - 10,
        child: IgnorePointer(
          child: Container(
            width: 34,
            height: 20,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: Colors.black.withValues(alpha: 0.75),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: Colors.white70, width: 1),
            ),
            child: Text('$ratio×',
                style: const TextStyle(fontSize: 11.5, fontWeight: FontWeight.w700, color: Colors.white)),
          ),
        ),
      ),
      // close, on the rim at the upper right (a 40 px target round a 28 px button)
      Positioned(
        left: c.dx + diag - 20,
        top: c.dy - diag - 20,
        width: 40,
        height: 40,
        child: Tooltip(
          message: 'Close the loupe',
          child: GestureDetector(
            key: const Key('loupe-close'),
            behavior: HitTestBehavior.opaque,
            onTap: preview.loupeOff,
            child: Center(
              child: Container(
                width: 28,
                height: 28,
                decoration: BoxDecoration(
                  color: Colors.black.withValues(alpha: 0.8),
                  shape: BoxShape.circle,
                  border: Border.all(color: Colors.white70),
                ),
                child: const Icon(Icons.close_rounded, size: 17, color: Colors.white),
              ),
            ),
          ),
        ),
      ),
    ]);
  }
}
