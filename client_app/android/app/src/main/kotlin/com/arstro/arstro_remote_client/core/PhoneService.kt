package com.arstro.arstro_remote_client.core

import android.annotation.SuppressLint
import android.app.Activity
import android.app.ActivityManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.Executors

/**
 * "Share this phone" (docs/phone/PHONE.md): the foreground service that makes the phone a device of
 * the device manager. It runs the relay agent (RelayAgent -> Router, nothing listens on a port), starts
 * the features that are on and ready, re-checks readiness, and shows "Sharing this phone · online ·
 * N viewers" with a Stop action. It is the features' PhoneContext.
 *
 * Foreground types: specialUse (+ location while `find` is ready) from the start; camera / microphone /
 * mediaProjection only while a feature holds them (`foreground(owner, types)`).
 */
class PhoneService : Service(), PhoneContext {
    companion object {
        const val TAG = "ArstroPhone"
        private const val CHANNEL = "arstro_phone"
        private const val NOTIF_ID = 4201
        const val ACTION_STOP = "com.arstro.arstro_remote_client.STOP"
        private const val CHECK_MS = 10_000L
        private const val TRAMPOLINE_WAIT_MS = 6000L

        @Volatile var instance: PhoneService? = null
            private set
        private val listeners = CopyOnWriteArraySet<() -> Unit>()

        /** Called (on the main thread) after every change of the service's state. */
        fun addListener(l: () -> Unit) = listeners.add(l)
        fun removeListener(l: () -> Unit) = listeners.remove(l)
        internal fun notifyListeners() = listeners.forEach { try { it() } catch (e: Exception) { Log.w(TAG, "listener failed", e) } }

        fun start(ctx: Context) {
            ContextCompat.startForegroundService(ctx, Intent(ctx, PhoneService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, PhoneService::class.java))
        }

        fun appVersion(ctx: Context): String = try {
            ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "?"
        } catch (e: Exception) { "?" }

        fun platform() = "Android ${Build.VERSION.RELEASE}"
    }

    lateinit var store: Store
        private set
    lateinit var agent: RelayAgent
        private set
    lateinit var router: Router
        private set
    private val bus = EventHub { level, msg -> log(level, msg) }
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor { r -> Thread(r, "phone-features") }
    private val running = HashSet<String>()           // features started (worker thread only)
    private val holds = HashMap<String, Int>()        // foreground owner -> types (main thread only)
    private var currentTypes = -1
    private var agentSig: String? = null
    private var keySig: String? = null
    private var featureSig: String? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var lastNotice: String? = null

    override val app: Context get() = applicationContext
    override val events: EventBus get() = bus

    private val gate = object : FeatureGate {
        override fun isOn(f: Feature) = store.isOn(f.id)
        override fun missing(f: Feature) = Permissions.missing(this@PhoneService, f).map { it.title }
    }

    private val prefs = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> main.post { refresh() } }

    /** Wi-Fi <-> mobile data in a pocket: the connection may sit on a network that is gone - connect
     *  again at once instead of after a timeout and the back-off. */
    private val network = object : ConnectivityManager.NetworkCallback() {
        private var current: Network? = null
        private var first = true

        override fun onAvailable(n: Network) {
            val changed = !first && n != current
            first = false
            current = n
            if (changed && instance === this@PhoneService) {
                log(LogLevel.INFO, "the network changed - connecting again")
                agent.networkChanged()
            }
        }

        override fun onLost(n: Network) {
            if (n == current) current = null
        }
    }

    private val check = object : Runnable {
        override fun run() {
            refresh()
            main.postDelayed(this, CHECK_MS)
        }
    }

    // ------------------------------------------------------------------------------ life
    override fun onCreate() {
        super.onCreate()
        store = Store(this)
        createChannel()
        if (applyTypes(baseTypes()) != null && currentTypes == -1) {   // within the few seconds Android allows
            log(LogLevel.ERROR, "Android did not let the service run in the foreground - stopping")
            stopSelf()
            return
        }
        instance = this
        router = Router(this, { Features.all }, gate, AssetSource { path ->
            try { assets.open("phone/$path") } catch (e: Exception) { null }
        }, ::info, bus) { level, msg -> log(level, msg) }
        agent = RelayAgent(router, onChange = { main.post { onAgentChange() } }) { level, msg -> log(level, msg) }
        Transports.onFallback = { msg -> log(LogLevel.INFO, msg) }
        store.listen(prefs)
        try {
            getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(network)
        } catch (e: RuntimeException) {
            log(LogLevel.WARN, "no network callback: ${e.javaClass.simpleName}")
        }
        agent.start()
        refresh()
        main.postDelayed(check, CHECK_MS)
        log(LogLevel.INFO, "sharing this phone as ${store.id}")
        notifyListeners()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            store.enabled = false
            stopSelf()
            return START_NOT_STICKY
        }
        if (!store.enabled) store.enabled = true
        refresh()
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        instance = null
        main.removeCallbacks(check)
        store.unlisten(prefs)
        try { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(network) } catch (_: RuntimeException) {}
        if (::agent.isInitialized) {
            val a = agent
            Thread({ a.stop() }, "relay-stop").start()
        }
        worker.execute {
            for (id in running.toList()) Features.byId(id)?.let { stopFeature(it) }
            running.clear()
        }
        worker.shutdown()
        holds.clear()
        releaseWake()
        log(LogLevel.INFO, "stopped sharing this phone")
        super.onDestroy()
        notifyListeners()
    }

    // ------------------------------------------------------------------------------ state
    private fun info(): JSONObject = JSONObject()
        .put("name", store.name).put("model", Build.MODEL).put("manufacturer", Build.MANUFACTURER)
        .put("android", Build.VERSION.RELEASE).put("sdk", Build.VERSION.SDK_INT)
        .put("app_version", appVersion(this)).put("id", store.id)

    private fun agentConfig(): AgentConfig = AgentConfig(
        url = store.url, token = store.token, id = store.id, name = store.name, secret = store.secret,
        keyRaw = store.keyRaw, version = appVersion(this), hostname = Build.MODEL ?: "Android", platform = platform(),
    )

    /** Apply the settings and the switches: the agent's configuration, the features that run, the
     *  foreground types, the `features` topic. Main thread; cheap when nothing changed. */
    fun refresh() {
        if (instance !== this) return
        val key = store.key
        val sig = listOf(store.url, store.token, store.name, store.id).joinToString("\u0000")
        if (sig != agentSig || key != keySig) {
            val keyChanged = keySig != null && key != keySig
            agentSig = sig
            keySig = key
            agent.configure(agentConfig(), closeChannels = keyChanged)
        }
        updateWake()
        val fs = Features.all.joinToString(",") {
            val note = try { it.note(this) } catch (e: Exception) { null }
            "${it.id}:${store.isOn(it.id)}:${isReady(it.id)}:$note"
        }
        if (fs != featureSig) {
            featureSig = fs
            syncFeatures()
            router.publishFeatures()
            updateTypes()
            notifyListeners()
        }
    }

    private fun syncFeatures() {
        val want = Features.all.filter { isReady(it.id) }.map { it.id }.toSet()
        worker.execute {
            for (f in Features.all) {
                val on = f.id in want
                if (on && f.id !in running) {
                    try { f.start(this); running.add(f.id) } catch (e: Exception) {
                        log(LogLevel.ERROR, "feature ${f.id} did not start: ${e.javaClass.simpleName}")
                    }
                } else if (!on && f.id in running) {
                    running.remove(f.id)
                    stopFeature(f)
                }
            }
        }
    }

    private fun stopFeature(f: Feature) {
        try { f.stop(this) } catch (e: Exception) { log(LogLevel.WARN, "feature ${f.id} did not stop cleanly: ${e.javaClass.simpleName}") }
        main.post { if (holds.remove(f.id) != null) updateTypes() }
    }

    private fun onAgentChange() {
        updateWake()
        updateNotification()
        notifyListeners()
    }

    // ------------------------------------------------------------------------------ PhoneContext
    override fun isOn(featureId: String): Boolean = store.isOn(featureId)

    override fun isReady(featureId: String): Boolean {
        val f = Features.byId(featureId) ?: return false
        return store.isOn(featureId) && Permissions.missing(this, f).isEmpty()
    }

    override fun foreground(owner: String, types: Int, done: (Boolean, String?) -> Unit) {
        main.post {
            if (instance !== this) { done(false, "the phone is not sharing"); return@post }
            holds[owner] = types
            val want = wanted()
            if (currentTypes != -1 && (currentTypes and want) == want) { done(true, null); return@post }
            val fresh = want and (if (currentTypes == -1) 0 else currentTypes).inv()
            if (needsVisible(fresh) && !onScreen()) {
                var ran = false
                TrampolineActivity.run(this, { act ->
                    ran = true
                    val err = applyTypes(wanted())
                    act.finish()
                    done(err == null, err)
                }, onTimeout = {
                    if (!ran) {
                        holds.remove(owner)
                        done(false, "Android needs the app on the screen for this - open Arstro Remote Client on the phone, " +
                                    "or allow it to display over other apps")
                    }
                }, timeoutMs = TRAMPOLINE_WAIT_MS)
            } else {
                val err = applyTypes(want)
                if (err != null) holds.remove(owner)
                done(err == null, err)
            }
        }
    }

    override fun releaseForeground(owner: String) {
        main.post { if (holds.remove(owner) != null) updateTypes() }
    }

    override fun withActivity(block: (Activity) -> Unit) {
        main.post { TrampolineActivity.run(this, block) }
    }

    override fun log(level: Int, message: String) {
        Log.println(level.coerceIn(Log.VERBOSE, Log.ASSERT), TAG, message)
    }

    // ------------------------------------------------------------------------------ foreground types
    private fun mask(t: Int): Int {
        var m = 0
        if (Build.VERSION.SDK_INT >= 29) m = m or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
        if (Build.VERSION.SDK_INT >= 30) m = m or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        if (Build.VERSION.SDK_INT >= 34) m = m or ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        return t and m
    }

    private fun baseTypes(): Int {
        var t = 0
        if (Build.VERSION.SDK_INT >= 34) t = t or ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        if (Build.VERSION.SDK_INT >= 29 && ::store.isInitialized && isReady("find")) t = t or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        return t
    }

    private fun wanted(): Int = mask(baseTypes() or holds.values.fold(0) { a, b -> a or b })

    /** The app has an activity on top (then Android lets it add while-in-use types). */
    private fun onScreen(): Boolean {
        val info = ActivityManager.RunningAppProcessInfo()
        ActivityManager.getMyMemoryState(info)
        return info.importance <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
    }

    private fun needsVisible(types: Int): Boolean = Build.VERSION.SDK_INT >= 30 &&
        (types and (ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)) != 0

    private fun updateTypes() {
        val want = wanted()
        if (want != currentTypes) applyTypes(want)
    }

    /** startForeground with these types -> null, or why Android refused (then the base types run). */
    private fun applyTypes(types: Int): String? {
        val t = mask(types)
        return try {
            ServiceCompat.startForeground(this, NOTIF_ID, buildNotification(), t)
            currentTypes = t
            null
        } catch (e: Exception) {
            log(LogLevel.WARN, "foreground types $t refused: ${e.javaClass.simpleName}")
            val base = mask(if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0)
            try {
                ServiceCompat.startForeground(this, NOTIF_ID, buildNotification(), base)
                currentTypes = base
            } catch (e2: Exception) {
                log(LogLevel.ERROR, "cannot run in the foreground: ${e2.javaClass.simpleName}")
            }
            "Android did not allow it now (${e.message?.take(160) ?: e.javaClass.simpleName})"
        }
    }

    // ------------------------------------------------------------------------------ notification
    private fun createChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Sharing this phone", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while the phone can be reached through your device manager"
                setShowBadge(false)
            })
        }
    }

    private fun notice(): String {
        if (!::agent.isInitialized) return "Sharing this phone · starting"
        val viewers = agent.viewers().size
        val state = when (agent.state) {
            "online" -> "online"
            "connecting" -> "connecting"
            "error" -> "offline - retrying"
            else -> "offline"
        }
        val v = if (agent.state == "online") " · $viewers viewer${if (viewers == 1) "" else "s"}" else ""
        return "Sharing this phone · $state$v"
    }

    private fun buildNotification(): Notification {
        val open = packageManager.getLaunchIntentForPackage(packageName)?.let {
            PendingIntent.getActivity(this, 1, it, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        }
        val stop = PendingIntent.getService(this, 2, Intent(this, PhoneService::class.java).setAction(ACTION_STOP),
                                            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val text = notice()
        lastNotice = text
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setContentTitle("Arstro Remote Client")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .build()
    }

    private fun updateNotification() {
        if (notice() == lastNotice) return
        try {
            getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification())
        } catch (e: SecurityException) {
            // no notification permission: the service still runs
        }
    }

    // ------------------------------------------------------------------------------ wake lock
    /** The CPU stays awake while sharing ("Stay reachable", Store.awake): asleep, the phone would stop
     *  its pings and its pipe requests, and the device manager would list it offline. With the setting
     *  off, only while someone is connected (a stream must not stall when the screen goes off). */
    private fun updateWake() {
        if (store.awake || agent.viewers().isNotEmpty()) holdWake() else releaseWake()
    }

    @SuppressLint("WakelockTimeout")      // held for as long as the phone is shared; released in onDestroy
    private fun holdWake() {
        if (wakeLock?.isHeld == true) return
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "arstro:phone-sharing").apply {
                setReferenceCounted(false)
                acquire()
            }
    }

    private fun releaseWake() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }
}
