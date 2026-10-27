import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../media.dart';
import 'common.dart';

/// On-set monitoring over the live picture (REC-10, REC-13, REC-14): frame guides, safe areas,
/// centre cross, zebras and false colour over the picture, the audio meters, a histogram or
/// waveform, and the focus zoom (tap the picture). Everything is drawn from what the Pi sends:
/// the settings (`recorder.settings` › monitor, shared by every viewer), the false-colour bands
/// of the `recorder` status, and the preview socket's `levels` and `analysis`.
class MonitorLayer extends StatelessWidget {
  const MonitorLayer({super.key, required this.preview, required this.monitor, required this.bands,
                      this.canZoom = true});
  final PreviewController preview;

  /// `recorder.settings` › monitor.
  final Map<String, dynamic> monitor;

  /// `recorder` › monitor › bands.
  final List<Map<String, dynamic>> bands;
  final bool canZoom;

  @override
  Widget build(BuildContext context) {
    final zoom = preview.zoom;
    final scope = monitor['scope'] as String? ?? 'off';
    return LayoutBuilder(builder: (context, c) {
      final sw = math.min(220.0, c.maxWidth * 0.34), sh = math.min(110.0, c.maxHeight * 0.3);
      return Stack(fit: StackFit.expand, children: [
        Center(
          child: AspectRatio(
            aspectRatio: preview.aspect,
            child: LayoutBuilder(
              builder: (context, box) => GestureDetector(
                key: const Key('live-picture'),
                behavior: HitTestBehavior.opaque,
                // REC-14: x, y of the tap within the video itself (never the black bars around it)
                onTapUp: canZoom
                    ? (d) => preview.zoomAt(d.localPosition.dx / box.maxWidth, d.localPosition.dy / box.maxHeight)
                    : null,
                child: Stack(fit: StackFit.expand, children: [
                  if (preview.textureId != null) Texture(textureId: preview.textureId!),
                  // the overlays belong to the whole picture: hidden while zoomed
                  if (zoom == null)
                    ValueListenableBuilder<Analysis?>(
                      valueListenable: preview.analysis,
                      builder: (_, a, _) => IgnorePointer(
                        child: CustomPaint(
                          key: const Key('monitor-overlay'),
                          painter: MonitorPainter(monitor: monitor, bands: bands, analysis: a),
                        ),
                      ),
                    ),
                ]),
              ),
            ),
          ),
        ),
        if (monitor['meters'] != false)
          Positioned(
            left: 8,
            top: 44,
            bottom: 50,
            child: ValueListenableBuilder<AudioLevels?>(
              valueListenable: preview.levels,
              builder: (_, l, _) => l == null || l.peak.isEmpty ? const SizedBox() : AudioMeters(levels: l),
            ),
          ),
        if (zoom == null && scope != 'off')
          Positioned(
            right: 8,
            bottom: 56,
            width: sw,
            height: sh,
            child: ValueListenableBuilder<Analysis?>(
              valueListenable: preview.analysis,
              builder: (_, a, _) => a == null
                  ? const SizedBox()
                  : IgnorePointer(
                      child: CustomPaint(key: const Key('scope'), painter: ScopePainter(analysis: a, kind: scope)),
                    ),
            ),
          ),
        if (zoom != null) ...[
          Positioned(right: 10, top: 10, child: GlassLabel(zoomBadge(zoom))),
          Positioned(right: 10, top: 42, child: ZoomLocator(zoom: zoom)),
          Positioned(
            left: 0,
            right: 0,
            bottom: 46,                        // above the REC badge and the full-screen button
            child: Center(
              child: FilledButton.tonalIcon(
                style: FilledButton.styleFrom(
                  visualDensity: VisualDensity.compact,
                  backgroundColor: Colors.black.withValues(alpha: 0.65),
                  foregroundColor: Colors.white,
                ),
                onPressed: preview.zoomOff,
                icon: const Icon(Icons.zoom_out_map_rounded, size: 18),
                label: const Text('Whole picture'),
              ),
            ),
          ),
        ],
      ]);
    });
  }
}

/// "Zoom 3× · 1:1 pixels" from the server's zoom reply (`ratio`, `pixels` = screen pixels per
/// source pixel of the stream).
String zoomBadge(Map<String, dynamic> z) {
  final ratio = (z['ratio'] as num?) ?? 0;
  final px = (z['pixels'] as num?)?.toDouble() ?? 1;
  String one(double v) => v == v.roundToDouble() ? v.toStringAsFixed(0) : v.toStringAsFixed(1);
  final pixels = (px - 1).abs() < 0.02
      ? '1:1 pixels'
      : px < 1
          ? '1:${one(1 / px)} pixels'
          : '${one(px)}:1 pixels';
  return 'Zoom ${ratio.toInt()}× · $pixels';
}

/// Small white-on-dark label over the picture.
class GlassLabel extends StatelessWidget {
  const GlassLabel(this.text, {super.key, this.color});
  final String text;
  final Color? color;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(
            color: color ?? Colors.black.withValues(alpha: 0.6), borderRadius: BorderRadius.circular(20)),
        child: Text(text, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: Colors.white)),
      );
}

/// Where the zoomed crop lies in the whole picture (REC-14).
class ZoomLocator extends StatelessWidget {
  const ZoomLocator({super.key, required this.zoom, this.width = 72});
  final Map<String, dynamic> zoom;
  final double width;

  @override
  Widget build(BuildContext context) {
    final sw = (zoom['src_width'] as num?)?.toDouble() ?? 16, sh = (zoom['src_height'] as num?)?.toDouble() ?? 9;
    return SizedBox(
      key: const Key('zoom-locator'),
      width: width,
      height: width * sh / math.max(1, sw),
      child: CustomPaint(painter: _LocatorPainter(zoom)),
    );
  }
}

class _LocatorPainter extends CustomPainter {
  _LocatorPainter(this.zoom);
  final Map<String, dynamic> zoom;

  @override
  void paint(Canvas canvas, Size size) {
    final r = Offset.zero & size;
    canvas.drawRect(r, Paint()..color = Colors.black.withValues(alpha: 0.55));
    final crop = [for (final v in (zoom['crop'] as List? ?? const [])) (v as num).toDouble()];
    final sw = (zoom['src_width'] as num?)?.toDouble() ?? 0, sh = (zoom['src_height'] as num?)?.toDouble() ?? 0;
    if (crop.length == 4 && sw > 0 && sh > 0) {
      final c = Rect.fromLTWH(crop[0] / sw * size.width, crop[1] / sh * size.height, crop[2] / sw * size.width,
          crop[3] / sh * size.height);
      canvas.drawRect(c, Paint()..color = Colors.white.withValues(alpha: 0.35));
      canvas.drawRect(c, Paint()
        ..color = Colors.white
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.2);
    }
    canvas.drawRect(r.deflate(0.5), Paint()
      ..color = Colors.white70
      ..style = PaintingStyle.stroke);
  }

  @override
  bool shouldRepaint(_LocatorPainter old) => old.zoom != zoom;
}

const guideRatios = {'2.39': 2.39, '1.85': 1.85, '16:9': 16 / 9, '4:3': 4 / 3, '1:1': 1.0, '9:16': 9 / 16};

Color? _hex(Object? s) {
  final m = RegExp(r'^#?([0-9a-fA-F]{6})$').firstMatch('${s ?? ''}');
  return m == null ? null : Color(0xFF000000 | int.parse(m.group(1)!, radix: 16));
}

/// Guides, safe areas, centre cross, zebras and false colour over the picture (REC-13).
class MonitorPainter extends CustomPainter {
  MonitorPainter({required this.monitor, required this.bands, this.analysis});
  final Map<String, dynamic> monitor;
  final List<Map<String, dynamic>> bands;
  final Analysis? analysis;

  /// The band (its colour) of each map value (IRE x 2), or null where the picture shows through.
  static List<Color?> bandTable(List<Map<String, dynamic>> bands) => [
        for (var v = 0; v < 256; v++)
          () {
            final ire = v / 2;
            for (final b in bands) {
              final from = (b['from'] as num?)?.toDouble() ?? 0, to = (b['to'] as num?)?.toDouble() ?? 0;
              if (ire >= from && ire < to) return _hex(b['colour'])?.withValues(alpha: 0.8);
            }
            return null;
          }(),
      ];

  @override
  void paint(Canvas canvas, Size size) {
    final a = analysis;
    if (a != null && a.gw > 0 && a.gh > 0 && a.map.length >= a.gw * a.gh) {
      if (monitor['false_color'] == true) _falseColour(canvas, size, a);
      if (monitor['zebra'] == true) _zebras(canvas, size, a, ((monitor['zebra_level'] as num?) ?? 95).toDouble());
    }
    final ratio = guideRatios[monitor['guides']];
    if (ratio != null) _guide(canvas, size, ratio);
    if (monitor['safe'] == true) _safe(canvas, size);
    if (monitor['center'] == true) _centre(canvas, size);
  }

  /// Runs of cells along each row: (row, first column, end column, value).
  void _runs(Analysis a, bool Function(int v) take, Object? Function(int v) key,
      void Function(int row, int c0, int c1, int v) out) {
    for (var r = 0; r < a.gh; r++) {
      var c = 0;
      while (c < a.gw) {
        final v = a.map[r * a.gw + c];
        if (!take(v)) {
          c++;
          continue;
        }
        final k = key(v);
        var e = c + 1;
        while (e < a.gw && take(a.map[r * a.gw + e]) && key(a.map[r * a.gw + e]) == k) {
          e++;
        }
        out(r, c, e, v);
        c = e;
      }
    }
  }

  void _falseColour(Canvas canvas, Size size, Analysis a) {
    final table = bandTable(bands);
    final cw = size.width / a.gw, ch = size.height / a.gh;
    final paint = Paint()..isAntiAlias = false;
    _runs(a, (v) => table[v] != null, (v) => table[v], (r, c0, c1, v) {
      paint.color = table[v]!;
      canvas.drawRect(Rect.fromLTRB(c0 * cw, r * ch, c1 * cw, (r + 1) * ch), paint);
    });
  }

  void _zebras(Canvas canvas, Size size, Analysis a, double level) {
    final min = (level * 2).ceil();
    final cw = size.width / a.gw, ch = size.height / a.gh;
    final path = Path();
    var any = false;
    _runs(a, (v) => v >= min, (_) => true, (r, c0, c1, _) {
      path.addRect(Rect.fromLTRB(c0 * cw, r * ch, c1 * cw, (r + 1) * ch));
      any = true;
    });
    if (!any) return;
    canvas.save();
    canvas.clipPath(path);
    final stripe = Paint()
      ..color = Colors.white.withValues(alpha: 0.85)
      ..strokeWidth = 2.5;
    for (var x = -size.height; x < size.width; x += 8) {
      canvas.drawLine(Offset(x, size.height), Offset(x + size.height, 0), stripe);
    }
    canvas.restore();
  }

  void _guide(Canvas canvas, Size size, double ratio) {
    final aspect = size.width / size.height;
    final shade = Paint()..color = Colors.black.withValues(alpha: 0.45);
    final line = Paint()
      ..color = Colors.white.withValues(alpha: 0.8)
      ..strokeWidth = 1;
    if ((ratio - aspect).abs() < 0.01) return;
    if (ratio > aspect) {
      final h = size.width / ratio, top = (size.height - h) / 2;
      canvas.drawRect(Rect.fromLTWH(0, 0, size.width, top), shade);
      canvas.drawRect(Rect.fromLTWH(0, top + h, size.width, top), shade);
      canvas.drawLine(Offset(0, top), Offset(size.width, top), line);
      canvas.drawLine(Offset(0, top + h), Offset(size.width, top + h), line);
    } else {
      final w = size.height * ratio, left = (size.width - w) / 2;
      canvas.drawRect(Rect.fromLTWH(0, 0, left, size.height), shade);
      canvas.drawRect(Rect.fromLTWH(left + w, 0, left, size.height), shade);
      canvas.drawLine(Offset(left, 0), Offset(left, size.height), line);
      canvas.drawLine(Offset(left + w, 0), Offset(left + w, size.height), line);
    }
  }

  void _safe(Canvas canvas, Size size) {
    final r = Offset.zero & size;
    final p = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1;
    // action safe 93 %, title safe 90 %
    canvas.drawRect(Rect.fromCenter(center: r.center, width: size.width * 0.93, height: size.height * 0.93),
        p..color = Colors.white.withValues(alpha: 0.55));
    canvas.drawRect(Rect.fromCenter(center: r.center, width: size.width * 0.90, height: size.height * 0.90),
        p..color = Colors.white.withValues(alpha: 0.8));
  }

  void _centre(Canvas canvas, Size size) {
    final c = (Offset.zero & size).center;
    final l = math.min(size.width, size.height) * 0.05;
    for (final (colour, w) in [(Colors.black54, 3.0), (Colors.white, 1.3)]) {
      final p = Paint()
        ..color = colour
        ..strokeWidth = w;
      canvas.drawLine(c - Offset(l, 0), c + Offset(l, 0), p);
      canvas.drawLine(c - Offset(0, l), c + Offset(0, l), p);
    }
  }

  @override
  bool shouldRepaint(MonitorPainter old) =>
      old.analysis != analysis || old.monitor.toString() != monitor.toString() || old.bands != bands;
}

/// Histogram or waveform of the full captured frame (REC-13); waveform row 0 = 0 % at the bottom.
class ScopePainter extends CustomPainter {
  ScopePainter({required this.analysis, required this.kind});
  final Analysis analysis;
  final String kind;

  @override
  void paint(Canvas canvas, Size size) {
    final a = analysis;
    final r = Offset.zero & size;
    canvas.drawRRect(RRect.fromRectAndRadius(r, const Radius.circular(8)), Paint()..color = Colors.black.withValues(alpha: 0.6));
    final inner = r.deflate(6);
    final grid = Paint()
      ..color = Colors.white24
      ..strokeWidth = 1;
    if (kind == 'waveform') {
      if (a.ww <= 0 || a.wl <= 0 || a.wave.length < a.ww * a.wl) return;
      double y(double ire) => inner.bottom - ire / a.ireMax * inner.height;
      for (final ire in [0.0, 50.0, 100.0]) {
        canvas.drawLine(Offset(inner.left, y(ire)), Offset(inner.right, y(ire)), grid);
      }
      final cw = inner.width / a.ww, ch = inner.height / a.wl;
      final p = Paint()..isAntiAlias = false;
      for (var row = 0; row < a.wl; row++) {
        final top = inner.bottom - (row + 1) * ch;             // row 0 = 0 % = the bottom
        for (var col = 0; col < a.ww; col++) {
          final v = a.wave[row * a.ww + col];
          if (v == 0) continue;
          p.color = const Color(0xFF7CFFB0).withValues(alpha: (v / 255).clamp(0.15, 1.0));
          canvas.drawRect(Rect.fromLTWH(inner.left + col * cw, top, cw + 0.5, ch + 0.5), p);
        }
      }
    } else {
      // histogram: 0..127 % IRE left to right, lines at 0 and 100 %
      final bw = inner.width / 128;
      canvas.drawLine(Offset(inner.left + 100 * bw, inner.top), Offset(inner.left + 100 * bw, inner.bottom), grid);
      final p = Paint()..color = Colors.white.withValues(alpha: 0.85);
      for (var i = 0; i < 128 && i < a.hist.length; i++) {
        final h = a.hist[i] / 255 * inner.height;
        if (h <= 0) continue;
        canvas.drawRect(Rect.fromLTWH(inner.left + i * bw, inner.bottom - h, bw + 0.3, h), p);
      }
    }
  }

  @override
  bool shouldRepaint(ScopePainter old) => old.analysis != analysis || old.kind != kind;
}

/// Audio meters (REC-10): one bar per channel, RMS filled, the peak as a tick; -60..0 dBFS;
/// a clipped peak (>= -1 dBFS) is red.
class AudioMeters extends StatelessWidget {
  const AudioMeters({super.key, required this.levels});
  final AudioLevels levels;

  static const floor = -60.0;

  @override
  Widget build(BuildContext context) {
    final n = math.max(levels.peak.length, levels.rms.length);
    return Container(
      key: const Key('audio-meters'),
      width: 10.0 + n * 9,
      padding: const EdgeInsets.all(5),
      decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.55), borderRadius: BorderRadius.circular(6)),
      child: Semantics(
        label: 'Audio ${[for (final p in levels.peak) '${p.round()} dB'].join(', ')}',
        child: CustomPaint(painter: _MetersPainter(levels)),
      ),
    );
  }
}

class _MetersPainter extends CustomPainter {
  _MetersPainter(this.l);
  final AudioLevels l;

  double _f(double db) => ((db - AudioMeters.floor) / -AudioMeters.floor).clamp(0.0, 1.0);

  @override
  void paint(Canvas canvas, Size size) {
    final n = math.max(l.peak.length, l.rms.length);
    if (n == 0) return;
    const gap = 3.0;
    final w = (size.width - gap * (n - 1)) / n;
    for (var i = 0; i < n; i++) {
      final x = i * (w + gap);
      final peak = i < l.peak.length ? l.peak[i] : AudioMeters.floor;
      final rms = i < l.rms.length ? l.rms[i] : AudioMeters.floor;
      final clip = peak >= -1;
      canvas.drawRect(Rect.fromLTWH(x, 0, w, size.height), Paint()..color = Colors.white12);
      final h = _f(rms) * size.height;
      final colour = clip ? kRec : rms > -12 ? kWarn : kOk;
      canvas.drawRect(Rect.fromLTWH(x, size.height - h, w, h), Paint()..color = colour);
      final py = size.height - _f(peak) * size.height;
      canvas.drawRect(Rect.fromLTWH(x, py - 1, w, 2), Paint()..color = clip ? kRec : Colors.white);
    }
  }

  @override
  bool shouldRepaint(_MetersPainter old) => old.l != l;
}
