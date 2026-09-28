import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_scope.dart';
import '../remote_client.dart';
import 'format.dart';
import 'widgets.dart';

class DashboardPage extends StatelessWidget {
  const DashboardPage({super.key});

  @override
  Widget build(BuildContext context) {
    final client = AppScope.of(context).client;
    return ValueListenableBuilder<Map<String, dynamic>?>(
      valueListenable: client.stats,
      builder: (context, s, _) {
        if (s == null) {
          return const EmptyState(icon: Icons.hourglass_empty, text: 'Waiting for data from the Pi…');
        }
        final cards = <Widget>[
          _OverviewCard(s: s),
          _GaugesCard(s: s),
          _HistoryCard(client: client),
          _CpuCard(s: s),
          _TempsCard(s: s),
          _NetworkCard(s: s),
          _AcceleratorsCard(s: s),
          _StorageCard(s: s),
          _ProcessesCard(s: s),
        ];
        return LayoutBuilder(builder: (context, box) {
          final cols = box.maxWidth >= 1280 ? 3 : box.maxWidth >= 760 ? 2 : 1;
          final columns = List.generate(cols, (_) => <Widget>[]);
          for (var i = 0; i < cards.length; i++) {
            columns[i % cols].add(Padding(padding: const EdgeInsets.only(bottom: 12), child: cards[i]));
          }
          return RefreshIndicator(
            onRefresh: () async {
              try {
                client.stats.value = Map<String, dynamic>.from(await client.request('stats.get') as Map);
              } catch (_) {}
            },
            child: SingleChildScrollView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(12),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  for (var c = 0; c < cols; c++) ...[
                    if (c > 0) const SizedBox(width: 12),
                    Expanded(child: Column(children: columns[c])),
                  ],
                ],
              ),
            ),
          );
        });
      },
    );
  }
}

Map<String, dynamic> _m(dynamic v) => v is Map ? Map<String, dynamic>.from(v) : <String, dynamic>{};
List<dynamic> _l(dynamic v) => v is List ? v : const [];
double? _d(dynamic v) => v is num ? v.toDouble() : null;

class _OverviewCard extends StatelessWidget {
  const _OverviewCard({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final net = _m(s['network']);
    final wifi = _m(s['wifi']);
    final ip = net['ip'] as String?;
    return SectionCard(
      title: 'Overview',
      icon: Icons.developer_board,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('IP address', style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
              SelectableText(ip ?? 'No network',
                  style: theme.textTheme.headlineMedium?.copyWith(
                      fontWeight: FontWeight.w700, color: ip == null ? theme.colorScheme.error : null)),
            ]),
          ),
          if (ip != null)
            IconButton.filledTonal(
              tooltip: 'Copy IP',
              icon: const Icon(Icons.copy),
              onPressed: () {
                Clipboard.setData(ClipboardData(text: ip));
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Copied $ip')));
              },
            ),
        ]),
        const SizedBox(height: 12),
        KeyValueRow('Hostname', s['hostname']?.toString() ?? '–'),
        KeyValueRow('Uptime', fmtDuration(s['uptime'] as num?)),
        KeyValueRow('Wi-Fi', wifi['ssid'] == null ? 'Not connected' : '${wifi['ssid']}  (${wifi['signal']}%)'),
        KeyValueRow('Gateway', net['gateway'] == null ? '–' : '${net['gateway']} via ${net['gateway_iface']}'),
        KeyValueRow('OS', s['os']?.toString() ?? '–'),
        KeyValueRow('Kernel', '${s['kernel']} (${s['arch']})'),
      ]),
    );
  }
}

class _GaugesCard extends StatelessWidget {
  const _GaugesCard({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context) {
    final cpu = _d(_m(s['cpu'])['percent']) ?? 0;
    final temp = _d(s['cpu_temp']);
    final mem = _m(s['memory']);
    final disks = _l(s['disks']);
    final root = disks.isEmpty ? <String, dynamic>{} : _m(disks.first);
    return SectionCard(
      title: 'Live',
      icon: Icons.monitor_heart_outlined,
      child: Wrap(
        alignment: WrapAlignment.spaceAround,
        spacing: 12,
        runSpacing: 16,
        children: [
          RingGauge(value: cpu / 100, label: 'CPU', center: fmtPct(cpu), color: loadColor(cpu)),
          RingGauge(
              value: (temp ?? 0) / 100, label: 'Temperature', center: temp == null ? '–' : '${temp.round()}°',
              sublabel: 'CPU', color: tempColor(temp)),
          RingGauge(
              value: (_d(mem['percent']) ?? 0) / 100,
              label: 'Memory',
              center: fmtPct(mem['percent'] as num?),
              sublabel: '${fmtBytes(mem['used'] as num?)} / ${fmtBytes(mem['total'] as num?, digits: 0)}',
              color: loadColor(mem['percent'] as num?)),
          RingGauge(
              value: (_d(root['percent']) ?? 0) / 100,
              label: 'Disk ${root['mount'] ?? ''}',
              center: fmtPct(root['percent'] as num?),
              sublabel: '${fmtBytes(root['free'] as num?, digits: 0)} free',
              color: loadColor(root['percent'] as num?)),
        ],
      ),
    );
  }
}

class _HistoryCard extends StatelessWidget {
  const _HistoryCard({required this.client});
  final RemoteClient client;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cpu = client.cpuHistory;
    final temp = client.tempHistory;
    return SectionCard(
      title: 'History (3 min)',
      icon: Icons.show_chart,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('CPU  ${cpu.isEmpty ? '' : fmtPct(cpu.last)}', style: theme.textTheme.labelLarge),
        const SizedBox(height: 6),
        Sparkline(values: cpu, color: const Color(0xFF42A5F5)),
        const SizedBox(height: 14),
        Text('Temperature  ${temp.isEmpty ? '' : fmtTemp(temp.last)}', style: theme.textTheme.labelLarge),
        const SizedBox(height: 6),
        Sparkline(values: temp, color: tempColor(temp.isEmpty ? null : temp.last), min: 30, max: 90),
      ]),
    );
  }
}

class _CpuCard extends StatelessWidget {
  const _CpuCard({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context) {
    final cpu = _m(s['cpu']);
    final cores = _l(cpu['per_core']);
    final freqs = _l(cpu['freq_mhz']);
    final load = _l(cpu['load']);
    return SectionCard(
      title: 'CPU cores',
      icon: Icons.memory,
      trailing: Text(load.length == 3 ? 'load ${load.join('  ')}' : '', style: Theme.of(context).textTheme.bodySmall),
      child: Column(children: [
        for (var i = 0; i < cores.length; i++)
          LabeledBar(
            label: 'Core $i${i < freqs.length ? ' · ${(freqs[i] as num) >= 1000 ? '${((freqs[i] as num) / 1000).toStringAsFixed(1)}G' : '${freqs[i]}M'}' : ''}',
            value: (_d(cores[i]) ?? 0) / 100,
            text: fmtPct(cores[i] as num?),
            color: loadColor(cores[i] as num?),
          ),
      ]),
    );
  }
}

class _TempsCard extends StatelessWidget {
  const _TempsCard({required this.s});
  final Map<String, dynamic> s;

  static const _names = {
    'soc': 'SoC',
    'bigcore0': 'Big core 0',
    'bigcore1': 'Big core 1',
    'littlecore': 'Little cores',
    'center': 'Center',
    'gpu': 'GPU',
    'npu': 'NPU',
    'iwlwifi_1': 'Wi-Fi card',
  };

  @override
  Widget build(BuildContext context) {
    final temps = _m(s['temps']);
    return SectionCard(
      title: 'Temperatures',
      icon: Icons.thermostat,
      child: Column(children: [
        for (final e in temps.entries)
          LabeledBar(
            label: _names[e.key] ?? e.key,
            value: (_d(e.value) ?? 0) / 100,
            text: fmtTemp(e.value as num?),
            color: tempColor(e.value as num?),
          ),
      ]),
    );
  }
}

class _NetworkCard extends StatelessWidget {
  const _NetworkCard({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final ifaces = _l(_m(s['network'])['interfaces']).map(_m).toList();
    return SectionCard(
      title: 'Network',
      icon: Icons.lan_outlined,
      child: Column(children: [
        for (final i in ifaces)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Icon(
                (i['name'] as String).startsWith('wl') ? Icons.wifi : Icons.settings_ethernet,
                color: i['up'] == true && _l(i['ipv4']).isNotEmpty
                    ? theme.colorScheme.primary
                    : theme.colorScheme.onSurfaceVariant.withValues(alpha: 0.5),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(i['name'] as String, style: theme.textTheme.titleSmall),
                  SelectableText(
                    _l(i['ipv4']).isEmpty ? (i['up'] == true ? 'up, no IPv4' : 'down') : _l(i['ipv4']).join(', '),
                    style: theme.textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w600),
                  ),
                  Text('${i['mac'] ?? ''}', style: theme.textTheme.bodySmall),
                ]),
              ),
              Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
                Text('↓ ${fmtRate(i['rx_rate'] as num?)}', style: theme.textTheme.bodySmall),
                Text('↑ ${fmtRate(i['tx_rate'] as num?)}', style: theme.textTheme.bodySmall),
              ]),
            ]),
          ),
      ]),
    );
  }
}

class _AcceleratorsCard extends StatelessWidget {
  const _AcceleratorsCard({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context) {
    final dev = _m(s['devfreq']);
    final fan = s['fan_percent'] as num?;
    final swap = _m(s['swap']);
    Widget bar(String key, String label) {
      final d = _m(dev[key]);
      if (d.isEmpty) return const SizedBox.shrink();
      return LabeledBar(
        label: '$label · ${d['mhz']}M',
        value: (_d(d['load']) ?? 0) / 100,
        text: fmtPct(d['load'] as num?),
        color: loadColor(d['load'] as num?),
      );
    }

    return SectionCard(
      title: 'GPU · NPU · RAM bus · Fan',
      icon: Icons.bolt_outlined,
      child: Column(children: [
        bar('gpu', 'GPU'),
        bar('npu', 'NPU'),
        bar('dmc', 'DDR'),
        if (fan != null) LabeledBar(label: 'Fan', value: fan / 100, text: fmtPct(fan), color: const Color(0xFF26A69A)),
        if ((swap['total'] as num? ?? 0) > 0)
          LabeledBar(
            label: 'Swap',
            value: (_d(swap['percent']) ?? 0) / 100,
            text: fmtPct(swap['percent'] as num?),
            color: loadColor(swap['percent'] as num?),
          ),
      ]),
    );
  }
}

class _StorageCard extends StatelessWidget {
  const _StorageCard({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context) {
    final disks = _l(s['disks']).map(_m).toList();
    return SectionCard(
      title: 'Storage',
      icon: Icons.storage,
      child: Column(children: [
        for (final d in disks)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              LabeledBar(
                label: d['mount'] as String,
                value: (_d(d['percent']) ?? 0) / 100,
                text: fmtPct(d['percent'] as num?),
                color: loadColor(d['percent'] as num?),
              ),
              Text('${d['device']} · ${d['fstype']} · ${fmtBytes(d['used'] as num?)} used of ${fmtBytes(d['total'] as num?)}',
                  style: Theme.of(context).textTheme.bodySmall),
            ]),
          ),
      ]),
    );
  }
}

class _ProcessesCard extends StatelessWidget {
  const _ProcessesCard({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final procs = _l(s['processes']).map(_m).toList();
    final mono = theme.textTheme.bodySmall?.copyWith(fontFeatures: const [FontFeature.tabularFigures()]);
    return SectionCard(
      title: 'Top processes',
      icon: Icons.view_list_outlined,
      child: Column(children: [
        Row(children: [
          Expanded(child: Text('Name', style: theme.textTheme.labelSmall)),
          SizedBox(width: 60, child: Text('PID', style: theme.textTheme.labelSmall, textAlign: TextAlign.right)),
          SizedBox(width: 56, child: Text('CPU', style: theme.textTheme.labelSmall, textAlign: TextAlign.right)),
          SizedBox(width: 56, child: Text('MEM', style: theme.textTheme.labelSmall, textAlign: TextAlign.right)),
        ]),
        const Divider(height: 12),
        for (final p in procs)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 2),
            child: Row(children: [
              Expanded(child: Text('${p['name']}', overflow: TextOverflow.ellipsis, style: theme.textTheme.bodyMedium)),
              SizedBox(width: 60, child: Text('${p['pid']}', style: mono, textAlign: TextAlign.right)),
              SizedBox(width: 56, child: Text(fmtPct(p['cpu'] as num?, digits: 1), style: mono, textAlign: TextAlign.right)),
              SizedBox(width: 56, child: Text(fmtPct(p['mem'] as num?, digits: 1), style: mono, textAlign: TextAlign.right)),
            ]),
          ),
      ]),
    );
  }
}
