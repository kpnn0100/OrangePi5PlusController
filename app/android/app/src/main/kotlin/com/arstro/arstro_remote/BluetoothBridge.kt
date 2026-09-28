package com.arstro.arstro_remote

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothSocket
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.WindowManager
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * Classic Bluetooth (RFCOMM) bridge for Flutter.
 *
 *  MethodChannel "arstro/bt"         commands (scan, pair, connect, write, ...)
 *  EventChannel  "arstro/bt/events"  status events (maps)
 *  EventChannel  "arstro/bt/data"    raw bytes received from the socket
 */
@SuppressLint("MissingPermission")
class BluetoothBridge(private val activity: Activity, messenger: BinaryMessenger) :
    MethodChannel.MethodCallHandler {

    companion object {
        private const val TAG = "ArstroBT"
        private const val REQ_PERMISSIONS = 4101
        private const val REQ_ENABLE = 4102
    }

    private val main = Handler(Looper.getMainLooper())
    private val methods = MethodChannel(messenger, "arstro/bt")
    private var eventSink: EventChannel.EventSink? = null
    private var dataSink: EventChannel.EventSink? = null

    private val adapter: BluetoothAdapter? =
        (activity.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager?)?.adapter

    // One thread writes so frames keep their order; another connects.
    private val writer = Executors.newSingleThreadExecutor { r -> Thread(r, "bt-writer") }
    private val connector = Executors.newSingleThreadExecutor { r -> Thread(r, "bt-connect") }

    @Volatile private var socket: BluetoothSocket? = null
    @Volatile private var output: OutputStream? = null
    private val generation = AtomicInteger(0)
    @Volatile private var connectedAddress: String? = null

    private var pendingPermission: MethodChannel.Result? = null
    private var pendingEnable: MethodChannel.Result? = null

    init {
        methods.setMethodCallHandler(this)
        EventChannel(messenger, "arstro/bt/events").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(arguments: Any?, events: EventChannel.EventSink?) { eventSink = events }
            override fun onCancel(arguments: Any?) { eventSink = null }
        })
        EventChannel(messenger, "arstro/bt/data").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(arguments: Any?, events: EventChannel.EventSink?) { dataSink = events }
            override fun onCancel(arguments: Any?) { dataSink = null }
        })
    }

    fun dispose() {
        try { activity.unregisterReceiver(receiver) } catch (_: Exception) {}
        closeSocket("app closed")
        writer.shutdownNow()
        connector.shutdownNow()
    }

    // ------------------------------------------------------------------ events
    private fun emit(event: Map<String, Any?>) {
        main.post { eventSink?.success(event) }
    }

    private fun deviceMap(d: BluetoothDevice, rssi: Int? = null): Map<String, Any?> {
        val name = try { d.name } catch (_: SecurityException) { null }
        val bonded = try { d.bondState == BluetoothDevice.BOND_BONDED } catch (_: SecurityException) { false }
        val type = try { d.type } catch (_: SecurityException) { 0 }
        return mapOf("name" to name, "address" to d.address, "bonded" to bonded, "rssi" to rssi, "type" to type)
    }

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            intent ?: return
            val device: BluetoothDevice? = if (Build.VERSION.SDK_INT >= 33)
                intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
            else @Suppress("DEPRECATION") intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
            when (intent.action) {
                BluetoothDevice.ACTION_FOUND, BluetoothDevice.ACTION_NAME_CHANGED -> device?.let {
                    val rssi = intent.getShortExtra(BluetoothDevice.EXTRA_RSSI, Short.MIN_VALUE)
                    emit(mapOf("event" to "found",
                        "device" to deviceMap(it, if (rssi == Short.MIN_VALUE) null else rssi.toInt())))
                }
                BluetoothAdapter.ACTION_DISCOVERY_STARTED -> emit(mapOf("event" to "discovery", "state" to "started"))
                BluetoothAdapter.ACTION_DISCOVERY_FINISHED -> emit(mapOf("event" to "discovery", "state" to "finished"))
                BluetoothDevice.ACTION_BOND_STATE_CHANGED -> device?.let {
                    val state = when (intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1)) {
                        BluetoothDevice.BOND_BONDED -> "bonded"
                        BluetoothDevice.BOND_BONDING -> "bonding"
                        else -> "none"
                    }
                    emit(mapOf("event" to "bond", "address" to it.address, "state" to state, "device" to deviceMap(it)))
                }
                BluetoothAdapter.ACTION_STATE_CHANGED -> {
                    val s = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1)
                    val state = when (s) {
                        BluetoothAdapter.STATE_ON -> "on"
                        BluetoothAdapter.STATE_OFF -> "off"
                        BluetoothAdapter.STATE_TURNING_ON -> "turning_on"
                        else -> "turning_off"
                    }
                    emit(mapOf("event" to "adapter", "state" to state))
                    if (s == BluetoothAdapter.STATE_TURNING_OFF || s == BluetoothAdapter.STATE_OFF) {
                        closeSocket("Bluetooth turned off")
                    }
                }
                BluetoothDevice.ACTION_ACL_DISCONNECTED -> device?.let {
                    emit(mapOf("event" to "acl_disconnected", "address" to it.address))
                }
            }
        }
    }

    // Must come after `receiver` is initialised (property/init order).
    init {
        val filter = IntentFilter().apply {
            addAction(BluetoothDevice.ACTION_FOUND)
            addAction(BluetoothDevice.ACTION_NAME_CHANGED)
            addAction(BluetoothAdapter.ACTION_DISCOVERY_STARTED)
            addAction(BluetoothAdapter.ACTION_DISCOVERY_FINISHED)
            addAction(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
            addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
            addAction(BluetoothDevice.ACTION_ACL_DISCONNECTED)
        }
        if (Build.VERSION.SDK_INT >= 33) {
            activity.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            activity.registerReceiver(receiver, filter)
        }
    }

    // ------------------------------------------------------------- permissions
    private fun requiredPermissions(): Array<String> =
        if (Build.VERSION.SDK_INT >= 31)
            arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        else arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)

    private fun hasPermissions(): Boolean = requiredPermissions().all {
        activity.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
    }

    fun onRequestPermissionsResult(requestCode: Int, grantResults: IntArray): Boolean {
        if (requestCode != REQ_PERMISSIONS) return false
        pendingPermission?.success(grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED })
        pendingPermission = null
        return true
    }

    fun onActivityResult(requestCode: Int, resultCode: Int): Boolean {
        if (requestCode != REQ_ENABLE) return false
        pendingEnable?.success(resultCode == Activity.RESULT_OK || adapter?.isEnabled == true)
        pendingEnable = null
        return true
    }

    // ----------------------------------------------------------------- methods
    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        try {
            when (call.method) {
                "getState" -> result.success(mapOf(
                    "supported" to (adapter != null),
                    "enabled" to (adapter?.isEnabled == true),
                    "permissions" to hasPermissions(),
                    "connected" to (socket?.isConnected == true),
                    "connectedAddress" to connectedAddress,
                    "sdk" to Build.VERSION.SDK_INT,
                    "model" to "${Build.MANUFACTURER} ${Build.MODEL}",
                ))
                "requestPermissions" -> {
                    if (hasPermissions()) { result.success(true); return }
                    pendingPermission?.success(false)
                    pendingPermission = result
                    activity.requestPermissions(requiredPermissions(), REQ_PERMISSIONS)
                }
                "requestEnable" -> {
                    val a = adapter ?: return result.error("unsupported", "No Bluetooth adapter", null)
                    if (a.isEnabled) { result.success(true); return }
                    pendingEnable?.success(false)
                    pendingEnable = result
                    @Suppress("DEPRECATION")
                    activity.startActivityForResult(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE), REQ_ENABLE)
                }
                "bondedDevices" -> {
                    val a = adapter ?: return result.success(emptyList<Any>())
                    result.success(a.bondedDevices.map { deviceMap(it) })
                }
                "startDiscovery" -> {
                    val a = adapter ?: return result.success(false)
                    if (a.isDiscovering) a.cancelDiscovery()
                    result.success(a.startDiscovery())
                }
                "cancelDiscovery" -> result.success(adapter?.cancelDiscovery() ?: false)
                "pair" -> {
                    val d = adapter?.getRemoteDevice(call.argument<String>("address"))
                        ?: return result.error("unsupported", "No Bluetooth adapter", null)
                    adapter.cancelDiscovery()
                    result.success(d.createBond())
                }
                "unpair" -> {
                    val d = adapter?.getRemoteDevice(call.argument<String>("address"))
                        ?: return result.error("unsupported", "No Bluetooth adapter", null)
                    val ok = try {
                        d.javaClass.getMethod("removeBond").invoke(d) as Boolean
                    } catch (e: Exception) { false }
                    result.success(ok)
                }
                "connect" -> connect(call.argument<String>("address")!!, call.argument<String>("uuid")!!, result)
                "write" -> write(call.arguments as ByteArray, result)
                "disconnect" -> { closeSocket("closed by user"); result.success(true) }
                "moveToBack" -> result.success(activity.moveTaskToBack(true))
                "keepScreenOn" -> {
                    val on = call.arguments as Boolean
                    if (on) activity.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    else activity.window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    result.success(true)
                }
                else -> result.notImplemented()
            }
        } catch (e: SecurityException) {
            result.error("permission", "Bluetooth permission missing: ${e.message}", null)
        } catch (e: Exception) {
            result.error("error", e.message ?: e.toString(), null)
        }
    }

    // -------------------------------------------------------------- connection
    private fun connect(address: String, uuid: String, result: MethodChannel.Result) {
        val a = adapter ?: return result.error("unsupported", "No Bluetooth adapter", null)
        if (!a.isEnabled) return result.error("disabled", "Bluetooth is off", null)
        closeSocket(null)
        val gen = generation.incrementAndGet()
        emit(mapOf("event" to "connection", "state" to "connecting", "address" to address))
        connector.execute {
            var lastError: Exception? = null
            val device = a.getRemoteDevice(address)
            try { a.cancelDiscovery() } catch (_: SecurityException) {}
            val cancelled = { main.post { result.error("cancelled", "connection cancelled", null) } }
            for (attempt in 1..2) {
                if (generation.get() != gen) { cancelled(); return@execute }
                var s: BluetoothSocket? = null
                try {
                    // Secure socket: bonds automatically (system pairing dialog) if needed.
                    s = device.createRfcommSocketToServiceRecord(UUID.fromString(uuid))
                    socket = s
                    s.connect()
                    if (generation.get() != gen) { s.close(); cancelled(); return@execute }
                    output = s.outputStream
                    connectedAddress = address
                    Log.i(TAG, "connected to $address (attempt $attempt)")
                    val input = s.inputStream
                    main.post { result.success(true) }
                    emit(mapOf("event" to "connection", "state" to "connected", "address" to address,
                        "device" to deviceMap(device)))
                    readLoop(input, gen, address)
                    return@execute
                } catch (e: Exception) {
                    lastError = e
                    Log.w(TAG, "connect attempt $attempt to $address failed: ${e.message}")
                    try { s?.close() } catch (_: IOException) {}
                    if (socket === s) socket = null
                    if (attempt == 1) Thread.sleep(700)
                }
            }
            if (generation.get() == gen) {
                val msg = lastError?.message ?: "connection failed"
                main.post { result.error("connect_failed", msg, null) }
                emit(mapOf("event" to "connection", "state" to "failed", "address" to address, "reason" to msg))
            } else {
                cancelled()
            }
        }
    }

    private fun readLoop(input: InputStream, gen: Int, address: String) {
        val buf = ByteArray(16384)
        var reason = "connection closed by the Pi"
        try {
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                if (n == 0) continue
                val chunk = buf.copyOf(n)
                main.post { dataSink?.success(chunk) }
            }
        } catch (e: IOException) {
            reason = e.message ?: "connection lost"
        }
        if (generation.get() == gen) {
            Log.i(TAG, "disconnected from $address: $reason")
            closeSocket(null)
            emit(mapOf("event" to "connection", "state" to "disconnected", "address" to address, "reason" to reason))
        }
    }

    private fun write(data: ByteArray, result: MethodChannel.Result) {
        val gen = generation.get()
        writer.execute {
            val out = output
            if (out == null || generation.get() != gen) {
                main.post { result.error("not_connected", "Not connected", null) }
                return@execute
            }
            try {
                out.write(data)
                out.flush()
                main.post { result.success(true) }
            } catch (e: IOException) {
                main.post { result.error("write_failed", e.message, null) }
            }
        }
    }

    private fun closeSocket(reason: String?) {
        val s = socket
        val addr = connectedAddress
        generation.incrementAndGet()
        socket = null
        output = null
        connectedAddress = null
        try { s?.close() } catch (_: IOException) {}
        if (s != null && reason != null) {
            emit(mapOf("event" to "connection", "state" to "disconnected", "address" to addr, "reason" to reason))
        }
    }
}
