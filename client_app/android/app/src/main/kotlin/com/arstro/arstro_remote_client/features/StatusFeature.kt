package com.arstro.arstro_remote_client.features

import android.app.ActivityManager
import android.app.KeyguardManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import android.os.StatFs
import android.os.SystemClock
import android.telephony.CellSignalStrengthGsm
import android.telephony.CellSignalStrengthLte
import android.telephony.CellSignalStrengthNr
import android.telephony.CellSignalStrengthWcdma
import android.telephony.TelephonyManager
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.PhoneContext
import org.json.JSONObject
import java.net.Inet4Address
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * Phone status (PHONE.md "status", PHN-03): battery, storage, memory, network, signal, uptime, screen.
 * Published on topic `status` every 30 s and on battery / network / screen changes while running.
 * The Wi-Fi name only when Android gives it (location permission and location on).
 */
object StatusFeature : Feature("status", "Phone status") {
    @Volatile private var ctx: PhoneContext? = null
    private var scheduler: ScheduledExecutorService? = null
    private var receiver: BroadcastReceiver? = null
    private var netCallback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var lastBattery = ""
    private val publishSoon = Runnable { publishNow() }

    override fun ops() = mapOf("status.get" to op { c, _ -> status(c.app) })

    override fun start(ctx: PhoneContext) {
        this.ctx = ctx
        val app = ctx.app
        val s = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "arstro-status").apply { isDaemon = true } }
        scheduler = s
        s.scheduleWithFixedDelay({ publishNow() }, 1, 30, TimeUnit.SECONDS)
        val r = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                if (intent.action == Intent.ACTION_BATTERY_CHANGED) {
                    // the battery broadcast comes often (temperature, voltage): only real changes
                    val key = "${intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)}/${intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1)}/" +
                        "${intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)}"
                    if (key == lastBattery) return
                    lastBattery = key
                }
                soon()
            }
        }
        receiver = r
        app.registerLocal(r, IntentFilter().apply {
            addAction(Intent.ACTION_BATTERY_CHANGED)
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
            addAction(Intent.ACTION_USER_PRESENT)
        })
        try {
            val cm = app.getSystemService(ConnectivityManager::class.java)
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = soon()
                override fun onLost(network: Network) = soon()
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = soon()
                override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) = soon()
            }
            cm.registerDefaultNetworkCallback(cb)
            netCallback = cb
        } catch (e: Exception) { }
    }

    override fun stop(ctx: PhoneContext) {
        scheduler?.shutdownNow()
        scheduler = null
        ctx.app.unregisterQuietly(receiver)
        receiver = null
        netCallback?.let { cb -> try { ctx.app.getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(cb) } catch (e: Exception) { } }
        netCallback = null
        mainHandler.removeCallbacks(publishSoon)
        this.ctx = null
    }

    private fun soon() {
        mainHandler.removeCallbacks(publishSoon)
        mainHandler.postDelayed(publishSoon, 1000)
    }

    private fun publishNow() {
        val c = ctx ?: return
        if (isMainThread()) {
            scheduler?.execute { publishNow() }
            return
        }
        try { c.events.publish("status", status(c.app)) } catch (e: Exception) { }
    }

    fun status(app: Context): JSONObject {
        val o = JSONObject()
        o.put("battery", battery(app))
        try {
            val st = StatFs(Environment.getDataDirectory().absolutePath)
            o.put("storage", JSONObject().put("total", st.totalBytes).put("free", st.availableBytes))
        } catch (e: Exception) { }
        val mi = ActivityManager.MemoryInfo()
        app.getSystemService(ActivityManager::class.java).getMemoryInfo(mi)
        o.put("memory", JSONObject().put("total", mi.totalMem).put("available", mi.availMem))
        o.put("network", network(app))
        signal(app)?.let { o.put("signal", it) }
        o.put("uptime_s", SystemClock.elapsedRealtime() / 1000)
        o.put("screen_on", app.getSystemService(PowerManager::class.java).isInteractive)
        o.put("locked", app.getSystemService(KeyguardManager::class.java).isKeyguardLocked)
        return o
    }

    private fun battery(app: Context): JSONObject {
        val i = app.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return JSONObject()
        val level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
        val status = i.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val plugged = when (i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0)) {
            BatteryManager.BATTERY_PLUGGED_AC -> "ac"
            BatteryManager.BATTERY_PLUGGED_USB -> "usb"
            BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
            8 -> "dock"
            0 -> "none"
            else -> "other"
        }
        val health = when (i.getIntExtra(BatteryManager.EXTRA_HEALTH, 0)) {
            BatteryManager.BATTERY_HEALTH_GOOD -> "good"
            BatteryManager.BATTERY_HEALTH_OVERHEAT -> "overheat"
            BatteryManager.BATTERY_HEALTH_DEAD -> "dead"
            BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE -> "over_voltage"
            BatteryManager.BATTERY_HEALTH_COLD -> "cold"
            BatteryManager.BATTERY_HEALTH_UNSPECIFIED_FAILURE -> "failure"
            else -> "unknown"
        }
        return JSONObject()
            .put("level", if (level >= 0 && scale > 0) Math.round(level * 100f / scale) else JSONObject.NULL)
            .put("charging", status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL)
            .put("plugged", plugged)
            .put("temperature_c", i.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0)
            .put("health", health)
    }

    @Suppress("DEPRECATION")
    private fun network(app: Context): JSONObject {
        val o = JSONObject().put("type", "none").put("connected", false)
        val cm = app.getSystemService(ConnectivityManager::class.java) ?: return o
        val n = cm.activeNetwork ?: return o
        val caps = cm.getNetworkCapabilities(n) ?: return o
        val type = when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_BLUETOOTH) -> "bluetooth"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
            else -> "other"
        }
        o.put("type", type).put("connected", caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET))
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) o.put("vpn", true)
        cm.getLinkProperties(n)?.linkAddresses?.map { it.address }?.firstOrNull { it is Inet4Address && !it.isLoopbackAddress }
            ?.let { o.put("ip", it.hostAddress) }
        if (type == "wifi") {
            try {
                val wm = app.applicationContext.getSystemService(WifiManager::class.java)
                val info = wm?.connectionInfo
                val ssid = info?.ssid?.trim('"')
                if (!ssid.isNullOrEmpty() && ssid != "<unknown ssid>" && ssid != WifiManager.UNKNOWN_SSID.trim('"')) o.put("wifi_ssid", ssid)
                if (info != null && info.rssi > -127) o.put("wifi_rssi", info.rssi)
            } catch (e: Exception) { }
        }
        return o
    }

    @Suppress("DEPRECATION")
    private fun signal(app: Context): JSONObject? {
        if (!app.packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY)) return wifiSignal(app)
        if (Build.VERSION.SDK_INT >= 28) {
            try {
                val tm = app.getSystemService(TelephonyManager::class.java)
                val ss = tm?.signalStrength
                if (ss != null) {
                    if (Build.VERSION.SDK_INT >= 29) {
                        val cell = ss.cellSignalStrengths.maxByOrNull { it.level }
                        if (cell != null) {
                            val type = when {
                                cell is CellSignalStrengthNr -> "nr"
                                cell is CellSignalStrengthLte -> "lte"
                                cell is CellSignalStrengthWcdma -> "wcdma"
                                cell is CellSignalStrengthGsm -> "gsm"
                                else -> cell.javaClass.simpleName.removePrefix("CellSignalStrength").lowercase()
                            }
                            return JSONObject().put("type", type).put("level", cell.level).put("dbm", cell.dbm)
                        }
                    }
                    return JSONObject().put("type", "cellular").put("level", ss.level)
                }
            } catch (e: Exception) { }
        }
        return wifiSignal(app)
    }

    @Suppress("DEPRECATION")
    private fun wifiSignal(app: Context): JSONObject? = try {
        val wm = app.applicationContext.getSystemService(WifiManager::class.java)
        val info = wm?.connectionInfo
        if (info == null || info.rssi <= -127 || info.networkId == -1) null
        else JSONObject().put("type", "wifi").put("level", WifiManager.calculateSignalLevel(info.rssi, 5)).put("dbm", info.rssi)
    } catch (e: Exception) { null }
}
