import 'package:flutter/material.dart';

String fmtBytes(num? bytes, {int digits = 1}) {
  if (bytes == null) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var v = bytes.toDouble();
  var i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return '${v.toStringAsFixed(i == 0 ? 0 : digits)} ${units[i]}';
}

String fmtRate(num? bytesPerSec) => bytesPerSec == null ? '–' : '${fmtBytes(bytesPerSec)}/s';

String fmtDuration(num? seconds) {
  if (seconds == null) return '–';
  var s = seconds.toInt();
  final d = s ~/ 86400;
  s %= 86400;
  final h = s ~/ 3600;
  s %= 3600;
  final m = s ~/ 60;
  if (d > 0) return '${d}d ${h}h ${m}m';
  if (h > 0) return '${h}h ${m}m';
  if (m > 0) return '${m}m ${s % 60}s';
  return '${s}s';
}

String fmtPct(num? v, {int digits = 0}) => v == null ? '–' : '${v.toStringAsFixed(digits)}%';

String fmtTemp(num? v) => v == null ? '–' : '${v.toStringAsFixed(1)}°C';

/// Green → amber → red for temperatures (RK3588 throttles around 85°C).
Color tempColor(num? t) {
  if (t == null) return Colors.grey;
  if (t < 55) return const Color(0xFF26C6DA);
  if (t < 70) return const Color(0xFF66BB6A);
  if (t < 80) return const Color(0xFFFFB300);
  return const Color(0xFFEF5350);
}

Color loadColor(num? pct) {
  if (pct == null) return Colors.grey;
  if (pct < 50) return const Color(0xFF42A5F5);
  if (pct < 80) return const Color(0xFFFFB300);
  return const Color(0xFFEF5350);
}

IconData signalIcon(int? signal) {
  if (signal == null) return Icons.signal_wifi_off;
  if (signal >= 75) return Icons.signal_wifi_4_bar;
  if (signal >= 50) return Icons.network_wifi_3_bar;
  if (signal >= 25) return Icons.network_wifi_2_bar;
  return Icons.network_wifi_1_bar;
}
