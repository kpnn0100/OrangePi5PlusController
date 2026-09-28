import 'dart:async';

import 'package:flutter/services.dart';

/// A Bluetooth device as reported by the native bridge.
class BtDevice {
  const BtDevice({required this.address, this.name, this.bonded = false, this.rssi});

  final String address;
  final String? name;
  final bool bonded;
  final int? rssi;

  String get label => (name == null || name!.isEmpty) ? address : name!;

  /// Pis running the Arstro service advertise themselves as "Arstro-hostname".
  bool get isArstro => (name ?? '').toLowerCase().startsWith('arstro');

  factory BtDevice.fromMap(Map<dynamic, dynamic> m) => BtDevice(
        address: m['address'] as String,
        name: m['name'] as String?,
        bonded: m['bonded'] as bool? ?? false,
        rssi: m['rssi'] as int?,
      );

  BtDevice copyWith({String? name, bool? bonded, int? rssi}) => BtDevice(
        address: address,
        name: name ?? this.name,
        bonded: bonded ?? this.bonded,
        rssi: rssi ?? this.rssi,
      );

  Map<String, dynamic> toJson() => {'address': address, 'name': name};
}

class BtState {
  const BtState({
    required this.supported,
    required this.enabled,
    required this.permissions,
    required this.sdk,
    required this.model,
  });

  final bool supported;
  final bool enabled;
  final bool permissions;
  final int sdk;
  final String model;
}

/// Thin wrapper over the Kotlin BluetoothBridge (MethodChannel + 2 EventChannels).
class Bluetooth {
  Bluetooth._();
  static final Bluetooth instance = Bluetooth._();

  static const _methods = MethodChannel('arstro/bt');
  static const _events = EventChannel('arstro/bt/events');
  static const _data = EventChannel('arstro/bt/data');

  Stream<Map<dynamic, dynamic>>? _eventStream;
  Stream<Uint8List>? _dataStream;

  /// Status events: found / discovery / bond / adapter / connection.
  Stream<Map<dynamic, dynamic>> get events =>
      _eventStream ??= _events.receiveBroadcastStream().map((e) => e as Map<dynamic, dynamic>).asBroadcastStream();

  /// Raw bytes arriving on the RFCOMM socket.
  Stream<Uint8List> get data =>
      _dataStream ??= _data.receiveBroadcastStream().map((e) => e as Uint8List).asBroadcastStream();

  Future<BtState> state() async {
    final m = await _methods.invokeMapMethod<String, dynamic>('getState') ?? {};
    return BtState(
      supported: m['supported'] as bool? ?? false,
      enabled: m['enabled'] as bool? ?? false,
      permissions: m['permissions'] as bool? ?? false,
      sdk: m['sdk'] as int? ?? 0,
      model: m['model'] as String? ?? '',
    );
  }

  Future<bool> requestPermissions() async => await _methods.invokeMethod<bool>('requestPermissions') ?? false;

  Future<bool> requestEnable() async => await _methods.invokeMethod<bool>('requestEnable') ?? false;

  Future<List<BtDevice>> bondedDevices() async {
    final list = await _methods.invokeListMethod<Map<dynamic, dynamic>>('bondedDevices') ?? [];
    return list.map(BtDevice.fromMap).toList();
  }

  Future<bool> startDiscovery() async => await _methods.invokeMethod<bool>('startDiscovery') ?? false;

  Future<void> cancelDiscovery() => _methods.invokeMethod('cancelDiscovery');

  Future<bool> pair(String address) async =>
      await _methods.invokeMethod<bool>('pair', {'address': address}) ?? false;

  Future<bool> unpair(String address) async =>
      await _methods.invokeMethod<bool>('unpair', {'address': address}) ?? false;

  /// Opens the RFCOMM socket. Throws [PlatformException] on failure.
  Future<void> connect(String address, String uuid) =>
      _methods.invokeMethod('connect', {'address': address, 'uuid': uuid});

  Future<void> write(Uint8List bytes) => _methods.invokeMethod('write', bytes);

  Future<void> disconnect() => _methods.invokeMethod('disconnect');

  Future<void> keepScreenOn(bool on) => _methods.invokeMethod('keepScreenOn', on);

  /// Sends the app to the background without destroying it (keeps the link up).
  Future<void> moveToBack() => _methods.invokeMethod('moveToBack');
}
