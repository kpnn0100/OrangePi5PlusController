package com.arstro.arstro_remote_client.features

import android.annotation.SuppressLint
import android.app.NotificationManager
import android.app.WallpaperManager
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.Intent
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.media.AudioManager
import android.os.Build
import android.os.Environment
import android.os.SystemClock
import android.provider.AlarmClock
import android.provider.Settings
import android.util.Log
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import com.arstro.arstro_remote_client.core.SpecialAccess
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Device controls (PHONE.md "device", PHN-24): what Android lets an ordinary app change on the phone -
 * the ringer mode and Do Not Disturb, Bluetooth (Android 12 only: later versions only open the panel),
 * an alarm or a timer in the clock app, the screen timeout, the wallpaper from a file, the sensors, and
 * facts about the phone. No root, no device owner.
 */
object DeviceFeature : Feature("device", "Device controls") {
    // Bluetooth on / off needs "Nearby devices" on Android 12 (the core counts it as granted elsewhere)
    override val permissions = listOf("android.permission.BLUETOOTH_CONNECT")
    override val specialAccess = listOf(SpecialAccess.DND)

    override fun ops() = mapOf(
        "device.info" to op { c, _ -> info(c.app) },
        "device.ringer" to op { c, a -> ringer(c, a.str("mode")) },
        "device.dnd" to op { c, a -> dnd(c, a.str("mode")) },
        "device.bluetooth" to op { c, a -> bluetooth(c, a.bool("on")) },
        "device.alarm" to op { c, a -> alarm(c, a) },
        "device.timer" to op { c, a -> timer(c, a) },
        "device.screen_timeout" to op { c, a -> screenTimeout(c.app, a.int("seconds")) },
        "device.wallpaper" to op { c, a -> wallpaper(c, a.needStr("path"), a.str("which")) },
        "device.sensors" to op { c, _ -> sensors(c.app) },
        "device.sensor" to op { c, a -> readSensor(c.app, a.int("type") ?: throw OpError("\"type\" is missing (a sensor type number from device.sensors)")) },
    )

    private fun audio(app: Context) = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private fun nm(app: Context) = app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    // ------------------------------------------------------------------------------------- facts
    private fun info(app: Context): JSONObject {
        val dm = app.resources.displayMetrics
        val bt = btAdapter(app)
        return jsonOf(
            "manufacturer" to Build.MANUFACTURER, "brand" to Build.BRAND, "model" to Build.MODEL, "device" to Build.DEVICE,
            "android" to Build.VERSION.RELEASE, "sdk" to Build.VERSION.SDK_INT,
            "security_patch" to (if (Build.VERSION.SDK_INT >= 23) Build.VERSION.SECURITY_PATCH else null),
            "build" to Build.DISPLAY, "abis" to JSONArray(Build.SUPPORTED_ABIS.toList()),
            "screen" to jsonOf("width" to dm.widthPixels, "height" to dm.heightPixels, "dpi" to dm.densityDpi),
            "uptime_s" to SystemClock.elapsedRealtime() / 1000,
            "ringer" to ringerName(audio(app).ringerMode),
            "dnd" to dndName(nm(app).currentInterruptionFilter),
            "bluetooth" to (try { bt?.isEnabled } catch (e: SecurityException) { null }),
            "screen_timeout_s" to (try { Settings.System.getInt(app.contentResolver, Settings.System.SCREEN_OFF_TIMEOUT) / 1000 } catch (e: Exception) { null }),
            "can" to jsonOf(
                "dnd" to nm(app).isNotificationPolicyAccessGranted,
                "write_settings" to Settings.System.canWrite(app),
                "bluetooth" to (Build.VERSION.SDK_INT < 33),
            ),
        )
    }

    // ------------------------------------------------------------------------------------- sound
    private fun ringerName(m: Int) = when (m) {
        AudioManager.RINGER_MODE_SILENT -> "silent"
        AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
        else -> "normal"
    }

    private fun ringer(c: PhoneContext, mode: String?): JSONObject {
        val am = audio(c.app)
        if (mode != null) {
            val m = when (mode.lowercase()) {
                "normal", "sound" -> AudioManager.RINGER_MODE_NORMAL
                "vibrate" -> AudioManager.RINGER_MODE_VIBRATE
                "silent" -> AudioManager.RINGER_MODE_SILENT
                else -> throw OpError("mode is normal, vibrate or silent")
            }
            needDnd(c.app)
            am.ringerMode = m
            c.log(Log.INFO, "device: ringer ${ringerName(m)}")
        }
        return jsonOf("mode" to ringerName(am.ringerMode))
    }

    private fun dndName(f: Int) = when (f) {
        NotificationManager.INTERRUPTION_FILTER_PRIORITY -> "priority"
        NotificationManager.INTERRUPTION_FILTER_ALARMS -> "alarms"
        NotificationManager.INTERRUPTION_FILTER_NONE -> "none"
        else -> "off"
    }

    private fun needDnd(app: Context) {
        if (!nm(app).isNotificationPolicyAccessGranted) throw OpError("Do Not Disturb access is needed - allow it in the app", 409)
    }

    private fun dnd(c: PhoneContext, mode: String?): JSONObject {
        val n = nm(c.app)
        if (mode != null) {
            val f = when (mode.lowercase()) {
                "off", "all" -> NotificationManager.INTERRUPTION_FILTER_ALL
                "priority" -> NotificationManager.INTERRUPTION_FILTER_PRIORITY
                "alarms" -> NotificationManager.INTERRUPTION_FILTER_ALARMS
                "none", "total" -> NotificationManager.INTERRUPTION_FILTER_NONE
                else -> throw OpError("mode is off, priority, alarms or none")
            }
            needDnd(c.app)
            n.setInterruptionFilter(f)
            // Android applies it a moment later
            val until = SystemClock.elapsedRealtime() + 1500
            while (n.currentInterruptionFilter != f && SystemClock.elapsedRealtime() < until) Thread.sleep(50)
            c.log(Log.INFO, "device: do not disturb ${dndName(f)}")
        }
        return jsonOf("mode" to dndName(n.currentInterruptionFilter))
    }

    // ------------------------------------------------------------------------------------- bluetooth
    private fun btAdapter(app: Context) = (app.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    @SuppressLint("MissingPermission")
    @Suppress("DEPRECATION")
    private fun bluetooth(c: PhoneContext, on: Boolean?): JSONObject {
        val bt = btAdapter(c.app) ?: throw OpError("this phone has no Bluetooth", 404)
        if (on != null) {
            if (Build.VERSION.SDK_INT >= 33) {
                // Android 13+: apps may not switch Bluetooth; open the panel for someone at the phone
                c.startFromBackground(Intent(Settings.ACTION_BLUETOOTH_SETTINGS))
                throw OpError("Android ${Build.VERSION.RELEASE} lets only the user switch Bluetooth - its settings are open on the phone", 409)
            }
            val ok = try { if (on) bt.enable() else bt.disable() } catch (e: SecurityException) {
                throw OpError("Nearby devices permission is needed - allow it in the app", 409)
            }
            if (!ok && bt.isEnabled != on) throw OpError("Android refused to turn Bluetooth ${if (on) "on" else "off"}", 409)
            c.log(Log.INFO, "device: bluetooth ${if (on) "on" else "off"}")
            // it takes a moment
            val until = SystemClock.elapsedRealtime() + 4000
            while (bt.isEnabled != on && SystemClock.elapsedRealtime() < until) Thread.sleep(100)
        }
        return jsonOf("on" to try { bt.isEnabled } catch (e: SecurityException) { null }, "name" to (try { bt.name } catch (e: SecurityException) { null }))
    }

    // ------------------------------------------------------------------------------------- clock
    private fun alarm(c: PhoneContext, a: JSONObject): JSONObject {
        val hour = a.int("hour") ?: throw OpError("\"hour\" is missing (0-23)")
        val minute = a.int("minute") ?: 0
        if (hour !in 0..23 || minute !in 0..59) throw OpError("the time must be 0-23 : 0-59")
        val label = a.str("label")?.take(60)
        val i = Intent(AlarmClock.ACTION_SET_ALARM).putExtra(AlarmClock.EXTRA_HOUR, hour).putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
        if (label != null) i.putExtra(AlarmClock.EXTRA_MESSAGE, label)
        if (i.resolveActivity(c.app.packageManager) == null) throw OpError("no clock app on the phone takes alarms", 404)
        c.startFromBackground(i)
        c.log(Log.INFO, "device: alarm set")
        return jsonOf("hour" to hour, "minute" to minute)
    }

    private fun timer(c: PhoneContext, a: JSONObject): JSONObject {
        val s = a.int("seconds") ?: throw OpError("\"seconds\" is missing")
        if (s !in 1..86_400) throw OpError("a timer is 1 second to 24 hours")
        val i = Intent(AlarmClock.ACTION_SET_TIMER).putExtra(AlarmClock.EXTRA_LENGTH, s).putExtra(AlarmClock.EXTRA_SKIP_UI, true)
        a.str("label")?.let { i.putExtra(AlarmClock.EXTRA_MESSAGE, it.take(60)) }
        if (i.resolveActivity(c.app.packageManager) == null) throw OpError("no clock app on the phone takes timers", 404)
        c.startFromBackground(i)
        c.log(Log.INFO, "device: timer set")
        return jsonOf("seconds" to s)
    }

    // ------------------------------------------------------------------------------------- settings
    private fun screenTimeout(app: Context, seconds: Int?): JSONObject {
        if (seconds != null) {
            if (seconds !in 15..1800) throw OpError("the screen timeout is 15 seconds to 30 minutes")
            if (!Settings.System.canWrite(app)) throw OpError("\"Modify system settings\" is needed - allow it in the app (Audio & tools)", 409)
            Settings.System.putInt(app.contentResolver, Settings.System.SCREEN_OFF_TIMEOUT, seconds * 1000)
        }
        return jsonOf("seconds" to Settings.System.getInt(app.contentResolver, Settings.System.SCREEN_OFF_TIMEOUT, 30_000) / 1000)
    }

    private fun wallpaper(c: PhoneContext, path: String, which: String?): JSONObject {
        val root = Environment.getExternalStorageDirectory().absolutePath
        val abs = StoragePaths.resolve(root, path) ?: throw OpError("no such file: $path", 404)
        val f = File(abs)
        if (!f.isFile) throw OpError("no such file: $path", 404)
        if (f.length() > 40L shl 20) throw OpError("the picture is too big (40 MB at most)", 413)
        val flags = when (which?.lowercase()) {
            null, "", "both" -> WallpaperManager.FLAG_SYSTEM or WallpaperManager.FLAG_LOCK
            "home" -> WallpaperManager.FLAG_SYSTEM
            "lock" -> WallpaperManager.FLAG_LOCK
            else -> throw OpError("which is home, lock or both")
        }
        try {
            f.inputStream().use { WallpaperManager.getInstance(c.app).setStream(it, null, true, flags) }
        } catch (e: SecurityException) {
            throw OpError("Android did not let the app read $path - turn on Files (all files access)", 409)
        } catch (e: java.io.IOException) {
            throw OpError("that file is not a picture Android can use", 400)
        }
        c.log(Log.INFO, "device: wallpaper changed")
        return jsonOf("set" to true)
    }

    // ------------------------------------------------------------------------------------- sensors
    private fun sm(app: Context) = app.getSystemService(Context.SENSOR_SERVICE) as SensorManager

    private fun sensors(app: Context): JSONArray = JSONArray().also { arr ->
        for (s in sm(app).getSensorList(Sensor.TYPE_ALL)) {
            arr.put(jsonOf("type" to s.type, "name" to s.name, "vendor" to s.vendor, "kind" to s.stringType.substringAfterLast('.'),
                           "unit" to unitOf(s.type), "wakeup" to s.isWakeUpSensor))
        }
    }

    private fun unitOf(type: Int): String? = when (type) {
        Sensor.TYPE_ACCELEROMETER, Sensor.TYPE_GRAVITY, Sensor.TYPE_LINEAR_ACCELERATION -> "m/s²"
        Sensor.TYPE_GYROSCOPE -> "rad/s"
        Sensor.TYPE_MAGNETIC_FIELD -> "µT"
        Sensor.TYPE_LIGHT -> "lx"
        Sensor.TYPE_PRESSURE -> "hPa"
        Sensor.TYPE_PROXIMITY -> "cm"
        Sensor.TYPE_AMBIENT_TEMPERATURE -> "°C"
        Sensor.TYPE_RELATIVE_HUMIDITY -> "%"
        Sensor.TYPE_STEP_COUNTER -> "steps"
        else -> null
    }

    private fun readSensor(app: Context, type: Int): JSONObject {
        val m = sm(app)
        val s = m.getDefaultSensor(type) ?: throw OpError("no sensor of type $type on this phone", 404)
        var values: FloatArray? = null
        var acc = 0
        val latch = CountDownLatch(1)
        val l = object : SensorEventListener {
            override fun onSensorChanged(e: SensorEvent) { if (values == null) { values = e.values.clone(); acc = e.accuracy; latch.countDown() } }
            override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
        }
        m.registerListener(l, s, SensorManager.SENSOR_DELAY_NORMAL, mainHandler)
        try { latch.await(3, TimeUnit.SECONDS) } finally { m.unregisterListener(l) }
        val v = values ?: throw OpError("${s.name} sent no value in 3 s (some sensors report only on a change)", 504)
        return jsonOf("type" to type, "name" to s.name, "values" to JSONArray(v.map { it.toDouble() }), "unit" to unitOf(type), "accuracy" to acc)
    }
}
