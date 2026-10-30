package com.arstro.arstro_remote.phone.features

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.admin.DeviceAdminReceiver
import android.app.admin.DevicePolicyManager
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.media.ToneGenerator
import android.os.Build
import android.os.Bundle
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.location.LocationManagerCompat
import com.arstro.arstro_remote.R
import com.arstro.arstro_remote.phone.core.Feature
import com.arstro.arstro_remote.phone.core.OpError
import com.arstro.arstro_remote.phone.core.PhoneContext
import com.arstro.arstro_remote.phone.core.SpecialAccess
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Find my phone (PHONE.md "find", PHN-10): location now (GPS + network), a 7-day history (a fix every
 * 15 min while ready), ring loudly (alarm stream at full volume, also through Do Not Disturb, volume and
 * DND restored after), lost mode (lock now + LostModeActivity over the lock screen with the message and a
 * number to call, kept across reboots until find.lost_off). Never logs locations or the message.
 */
object FindFeature : Feature("find", "Find my phone") {
    // (the core counts ACCESS_BACKGROUND_LOCATION as granted before Android 10)
    override val permissions = listOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION,
                                      Manifest.permission.ACCESS_BACKGROUND_LOCATION)
    override val specialAccess = listOf(SpecialAccess.DND, SpecialAccess.DEVICE_ADMIN, SpecialAccess.OVERLAY)

    private const val PREFS = "arstro_phone_find"
    private const val CHANNEL = "arstro_find"
    private const val NOTIFICATION_RING = 7102
    private const val ACTION_STOP_RING = "com.arstro.arstro_remote.find.STOP_RING"
    private const val FRESH_TIMEOUT_MS = 30_000L

    private val lock = Any()
    @Volatile private var ctx: PhoneContext? = null
    private var history: LocationHistory? = null
    private var scheduler: ScheduledExecutorService? = null
    private var tick: ScheduledFuture<*>? = null

    @Volatile var ringing = false
        private set
    private var ringStarted = 0L
    private var player: MediaPlayer? = null
    private var tone: ToneGenerator? = null
    private var savedVolume = -1
    private var savedFilter = -1
    private var ringReceiver: BroadcastReceiver? = null
    private var lostReceiver: BroadcastReceiver? = null
    private val ringTimeout = Runnable { ringStop("time is up") }
    private val toneLoop = object : Runnable {
        override fun run() {
            val t = tone ?: return
            try { t.startTone(ToneGenerator.TONE_CDMA_EMERGENCY_RINGBACK, 1000) } catch (e: Exception) { }
            mainHandler.postDelayed(this, 1200)
        }
    }

    // ------------------------------------------------------------------------------------- API

    override fun ops() = mapOf(
        "find.locate" to op { c, a -> locate(c, a.bool("fresh") == true) },
        "find.history" to op { c, a -> historyJson(c, a.limit(200, 2000)) },
        "find.ring" to op { c, a -> remember(c); ring(c, (a.int("seconds") ?: 60).coerceIn(5, 600)); state() },
        "find.ring_stop" to op { c, _ -> remember(c); ringStop("stopped by the viewer"); state() },
        "find.lost" to op { c, a -> remember(c); lost(c, a.needStr("message"), a.str("contact")) },
        "find.lost_off" to op { c, _ -> remember(c); lostOff(c.app); state() },
    )

    override fun start(ctx: PhoneContext) {
        remember(ctx)
        val s = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "arstro-find").apply { isDaemon = true } }
        scheduler = s
        val h = history(ctx.app)
        val last = h.lastTime(System.currentTimeMillis())
        val wait = if (last == null) 20_000L
                   else (LocationHistory.INTERVAL_MS - (System.currentTimeMillis() - last)).coerceIn(20_000L, LocationHistory.INTERVAL_MS)
        tick = s.scheduleWithFixedDelay({ recordFix(ctx) }, wait, LocationHistory.INTERVAL_MS, TimeUnit.MILLISECONDS)
        if (isLost(ctx.app)) mainHandler.post { resumeLost(ctx) }
        publish()
    }

    override fun stop(ctx: PhoneContext) {
        tick?.cancel(false)
        scheduler?.shutdownNow()
        scheduler = null
        tick = null
        ringStop("$title was turned off")
        // turned off by the owner: lost mode ends too; the service only stopping (shutdown): it comes back at boot
        if (!ctx.isOn(id)) lostOff(ctx.app)
        ctx.app.unregisterQuietly(lostReceiver)
        lostReceiver = null
        this.ctx = null
    }

    override fun note(ctx: PhoneContext): String? = when {
        isLost(ctx.app) -> "lost mode is on"
        ringing -> "ringing"
        else -> null
    }

    private fun remember(c: PhoneContext) { if (ctx == null) ctx = c }

    private fun prefs(app: Context) = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun isLost(app: Context) = prefs(app).getBoolean("lost", false)
    fun lostMessage(app: Context): String = prefs(app).getString("message", "") ?: ""
    fun lostContact(app: Context): String? = prefs(app).getString("contact", null)

    private fun state(): JSONObject = jsonOf("ringing" to ringing, "lost" to (ctx?.app?.let { isLost(it) } ?: false))

    private fun publish() { try { ctx?.events?.publish("find", state()) } catch (e: Exception) { } }

    // ------------------------------------------------------------------------------------- location

    private fun history(app: Context): LocationHistory =
        history ?: LocationHistory(File(File(app.filesDir, "phone"), "find_history.txt")).also { history = it }

    private fun json(l: Location): JSONObject = JSONObject().put("lat", l.latitude).put("lon", l.longitude)
        .put("accuracy_m", if (l.hasAccuracy()) l.accuracy.toDouble() else JSONObject.NULL).put("time", l.time)
        .put("provider", l.provider ?: "unknown")

    private fun locate(c: PhoneContext, fresh: Boolean): JSONObject {
        val lm = c.app.getSystemService(LocationManager::class.java)
        if (!LocationManagerCompat.isLocationEnabled(lm)) throw OpError("location is turned off on the phone", 409)
        if (!fresh) lastKnown(c.app, lm)?.let { return json(it) }
        val l = currentFix(c.app, lm, FRESH_TIMEOUT_MS)
            ?: throw OpError("no location fix within 30 s - the phone may be indoors or underground", 504)
        return json(l)
    }

    @SuppressLint("MissingPermission")
    private fun lastKnown(app: Context, lm: LocationManager): Location? {
        if (!app.granted(Manifest.permission.ACCESS_COARSE_LOCATION) && !app.granted(Manifest.permission.ACCESS_FINE_LOCATION)) return null
        return lm.getProviders(true).mapNotNull { p -> try { lm.getLastKnownLocation(p) } catch (e: Exception) { null } }
            .maxByOrNull { it.time }
    }

    /** A new fix from GPS and network: the first precise one, or the best seen ~8 s after the first. */
    @SuppressLint("MissingPermission")
    private fun currentFix(app: Context, lm: LocationManager, timeoutMs: Long): Location? {
        val providers = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .filter { p -> try { lm.isProviderEnabled(p) } catch (e: Exception) { false } }
        if (providers.isEmpty()) throw OpError("location is turned off on the phone", 409)
        val done = CountDownLatch(1)
        var best: Location? = null
        var firstAt = 0L
        val listener = object : LocationListener {
            override fun onLocationChanged(l: Location) {
                synchronized(this) {
                    if (firstAt == 0L) firstAt = System.currentTimeMillis()
                    val b = best
                    if (b == null || !b.hasAccuracy() || (l.hasAccuracy() && l.accuracy < b.accuracy)) best = l
                    if (l.hasAccuracy() && l.accuracy <= 25f) done.countDown()
                }
            }
            @Suppress("OVERRIDE_DEPRECATION") override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
            override fun onProviderEnabled(provider: String) {}
            override fun onProviderDisabled(provider: String) {}
        }
        onMain {
            for (p in providers) try { lm.requestLocationUpdates(p, 1000L, 0f, listener, Looper.getMainLooper()) } catch (e: Exception) { }
        }
        try {
            val deadline = System.currentTimeMillis() + timeoutMs
            while (System.currentTimeMillis() < deadline) {
                if (done.await(500, TimeUnit.MILLISECONDS)) break
                val f = synchronized(listener) { firstAt }
                if (f != 0L && System.currentTimeMillis() - f > 8000) break
            }
        } finally {
            try { onMain { lm.removeUpdates(listener) } } catch (e: Exception) { }
        }
        return synchronized(listener) { best }
    }

    private fun recordFix(c: PhoneContext) {
        try {
            val app = c.app
            if (!c.isReady(id)) return
            val lm = app.getSystemService(LocationManager::class.java)
            if (!LocationManagerCompat.isLocationEnabled(lm)) return
            val now = System.currentTimeMillis()
            val recent = lastKnown(app, lm)?.takeIf { now - it.time < 5 * 60_000L }
            val l = recent ?: currentFix(app, lm, 60_000L) ?: return
            history(app).add(LocationHistory.Fix(l.latitude, l.longitude, if (l.hasAccuracy()) l.accuracy.toDouble() else 0.0, l.time), now)
        } catch (e: Exception) {
            c.log(Log.DEBUG, "find: no fix for the history (${e.javaClass.simpleName})")
        }
    }

    private fun historyJson(c: PhoneContext, limit: Int): JSONArray {
        val out = JSONArray()
        for (f in history(c.app).list(System.currentTimeMillis(), limit)) {
            out.put(JSONObject().put("lat", f.lat).put("lon", f.lon).put("accuracy_m", f.accuracyM).put("time", f.time))
        }
        return out
    }

    // ------------------------------------------------------------------------------------- ring

    private fun ring(c: PhoneContext, seconds: Int) {
        val app = c.app
        mainHandler.removeCallbacks(ringTimeout)
        mainHandler.postDelayed(ringTimeout, seconds * 1000L)
        synchronized(lock) {
            if (ringing) return
            ringing = true
            ringStarted = System.currentTimeMillis()
        }
        c.log(Log.INFO, "find: ringing for $seconds s")
        onMain {
            val am = app.getSystemService(AudioManager::class.java)
            val nm = app.getSystemService(NotificationManager::class.java)
            try {
                if (nm.isNotificationPolicyAccessGranted && nm.currentInterruptionFilter != NotificationManager.INTERRUPTION_FILTER_ALL) {
                    savedFilter = nm.currentInterruptionFilter
                    nm.setInterruptionFilter(NotificationManager.INTERRUPTION_FILTER_ALL)
                }
            } catch (e: Exception) { }
            try {
                savedVolume = am.getStreamVolume(AudioManager.STREAM_ALARM)
                am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
            } catch (e: Exception) { savedVolume = -1 }
            startSound(app)
            try {
                val pattern = longArrayOf(0, 1000, 500)
                val attrs = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).build()
                @Suppress("DEPRECATION")
                if (Build.VERSION.SDK_INT >= 26) CallFeature.vibrator(app)?.vibrate(VibrationEffect.createWaveform(pattern, 0), attrs)
                else CallFeature.vibrator(app)?.vibrate(pattern, 0, attrs)
            } catch (e: Exception) { }
            val r = object : BroadcastReceiver() {
                override fun onReceive(context: Context, intent: Intent) {
                    when (intent.action) {
                        ACTION_STOP_RING -> ringStop("stopped on the phone")
                        // the power button pressed while ringing: someone has the phone in hand
                        Intent.ACTION_SCREEN_OFF -> if (System.currentTimeMillis() - ringStarted > 3000) ringStop("stopped on the phone")
                    }
                }
            }
            ringReceiver = r
            app.registerLocal(r, IntentFilter().apply { addAction(ACTION_STOP_RING); addAction(Intent.ACTION_SCREEN_OFF) })
            notifyRinging(app)
        }
        showOwnActivity(c, app, Intent(app, LostModeActivity::class.java))
        publish()
    }

    private fun startSound(app: Context) {
        val attrs = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        val uris = listOfNotNull(RingtoneManager.getActualDefaultRingtoneUri(app, RingtoneManager.TYPE_ALARM),
                                 RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                                 RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE))
        for (u in uris) {
            val mp = MediaPlayer()
            try {
                mp.setAudioAttributes(attrs)
                mp.setDataSource(app, u)
                mp.isLooping = true
                mp.prepare()
                mp.start()
                player = mp
                return
            } catch (e: Exception) {
                mp.release()
            }
        }
        try {
            tone = ToneGenerator(AudioManager.STREAM_ALARM, ToneGenerator.MAX_VOLUME)
            mainHandler.post(toneLoop)
        } catch (e: Exception) { }
    }

    @SuppressLint("MissingPermission")
    private fun notifyRinging(app: Context) {
        app.channel(CHANNEL, "Find my phone", NotificationManager.IMPORTANCE_HIGH, silent = true)
        val stop = PendingIntent.getBroadcast(app, 1, Intent(ACTION_STOP_RING).setPackage(app.packageName), PI_FLAGS)
        val open = PendingIntent.getActivity(app, 2, Intent(app, LostModeActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PI_FLAGS)
        val n = NotificationCompat.Builder(app, CHANNEL)
            .setSmallIcon(R.drawable.phone_ic_find)
            .setContentTitle("Find my phone")
            .setContentText("Someone is looking for this phone - tap Stop to silence it")
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setOngoing(true)
            .setContentIntent(open)
            .setFullScreenIntent(open, true)
            .setDeleteIntent(stop)
            .addAction(0, "Stop", stop)
            .build()
        try { NotificationManagerCompat.from(app).notify(NOTIFICATION_RING, n) } catch (e: Exception) { }
    }

    @SuppressLint("WrongConstant")      // savedFilter is a value Android gave us
    fun ringStop(why: String) {
        synchronized(lock) {
            if (!ringing) return
            ringing = false
        }
        val c = ctx
        c?.log(Log.INFO, "find: ringing stopped ($why)")
        mainHandler.removeCallbacks(ringTimeout)
        mainHandler.removeCallbacks(toneLoop)
        val app = c?.app
        mainHandler.post {
            try { player?.stop() } catch (e: Exception) { }
            try { player?.release() } catch (e: Exception) { }
            player = null
            try { tone?.release() } catch (e: Exception) { }
            tone = null
            if (app != null) {
                try { CallFeature.vibrator(app)?.cancel() } catch (e: Exception) { }
                val am = app.getSystemService(AudioManager::class.java)
                if (savedVolume >= 0) try { am.setStreamVolume(AudioManager.STREAM_ALARM, savedVolume, 0) } catch (e: Exception) { }
                savedVolume = -1
                val nm = app.getSystemService(NotificationManager::class.java)
                if (savedFilter >= 0) try {
                    if (nm.isNotificationPolicyAccessGranted) nm.setInterruptionFilter(savedFilter)
                } catch (e: Exception) { }
                savedFilter = -1
                app.unregisterQuietly(ringReceiver)
                ringReceiver = null
                try { NotificationManagerCompat.from(app).cancel(NOTIFICATION_RING) } catch (e: Exception) { }
            }
            LostModeActivity.refreshAll()
        }
        publish()
    }

    // ------------------------------------------------------------------------------------- lost mode

    private fun admin(app: Context) = ComponentName(app, AdminReceiver::class.java)

    private fun lost(c: PhoneContext, message: String, contact: String?): JSONObject {
        val app = c.app
        if (message.length > 500) throw OpError("the message is too long (at most 500 characters)")
        val number = contact?.trim()?.takeIf { it.isNotEmpty() }
        if (number != null && !Regex("""^[+0-9 ()\-.]{3,30}$""").matches(number)) throw OpError("\"contact\" must be a phone number")
        val dpm = app.getSystemService(DevicePolicyManager::class.java)
        val canLock = dpm.isAdminActive(admin(app))
        prefs(app).edit().putBoolean("lost", true).putString("message", message.trim()).putString("contact", number).apply()
        c.log(Log.INFO, "find: lost mode on")
        resumeLost(c)
        if (canLock) {
            mainHandler.postDelayed({
                try { dpm.lockNow() } catch (e: Exception) { c.log(Log.WARN, "find: lock failed (${e.javaClass.simpleName})") }
                // light the screen again so the message shows over the lock screen
                mainHandler.postDelayed({ wakeBriefly(app) }, 1200)
            }, 1000)
        }
        publish()
        return jsonOf("lost" to true, "locked" to canLock, "ringing" to ringing).also {
            if (!canLock) it.put("note", "the phone was not locked - Device admin (lock) is not allowed for Arstro Remote")
        }
    }

    private fun resumeLost(c: PhoneContext) {
        val app = c.app
        if (lostReceiver == null) {
            val r = object : BroadcastReceiver() {
                override fun onReceive(context: Context, intent: Intent) {
                    if (isLost(app)) showOwnActivity(ctx, app, Intent(app, LostModeActivity::class.java))
                }
            }
            lostReceiver = r
            app.registerLocal(r, IntentFilter().apply { addAction(Intent.ACTION_SCREEN_ON); addAction(Intent.ACTION_USER_PRESENT) })
        }
        showOwnActivity(c, app, Intent(app, LostModeActivity::class.java))
        LostModeActivity.refreshAll()
    }

    private fun lostOff(app: Context) {
        val was = isLost(app)
        prefs(app).edit().clear().apply()
        app.unregisterQuietly(lostReceiver)
        lostReceiver = null
        if (was) ctx?.log(Log.INFO, "find: lost mode off")
        mainHandler.post { LostModeActivity.refreshAll() }
        publish()
    }

    @Suppress("DEPRECATION")
    private fun wakeBriefly(app: Context) {
        try {
            val pm = app.getSystemService(PowerManager::class.java)
            if (pm.isInteractive) return
            pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or PowerManager.ON_AFTER_RELEASE,
                           "arstro:lost").acquire(5000)
        } catch (e: Exception) { }
    }
}

/** Device admin for Find my phone: only "lock now" (res/xml/arstro_device_admin.xml). */
class AdminReceiver : DeviceAdminReceiver()
