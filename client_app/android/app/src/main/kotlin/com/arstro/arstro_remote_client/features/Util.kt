package com.arstro.arstro_remote_client.features

import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.PixelFormat
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.view.View
import android.view.WindowManager
import androidx.core.content.ContextCompat
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/* Shared helpers of the features (Android side). Never log texts, locations or keys. */

internal const val TAG = "ArstroPhone"

internal val mainHandler: Handler by lazy { Handler(Looper.getMainLooper()) }

internal fun isMainThread() = Looper.myLooper() == Looper.getMainLooper()

/** Runs `block` on the main thread and waits for its result (directly when already there). */
internal fun <T> onMain(timeoutMs: Long = 10_000, block: () -> T): T {
    if (isMainThread()) return block()
    val latch = CountDownLatch(1)
    var result: Result<T>? = null
    mainHandler.post {
        result = runCatching(block)
        latch.countDown()
    }
    if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) throw OpError("the phone did not answer in time - try again", 504)
    return result!!.getOrThrow()
}

internal fun log(level: Int, message: String) = Log.println(level, TAG, message)

// ------------------------------------------------------------------------------------- op arguments

internal fun JSONObject.str(name: String): String? {
    val v = opt(name)
    return if (v == null || v == JSONObject.NULL) null else v.toString()
}

internal fun JSONObject.needStr(name: String): String =
    str(name)?.takeIf { it.isNotEmpty() } ?: throw OpError("\"$name\" is missing")

internal fun JSONObject.num(name: String): Double? = when (val v = opt(name)) {
    is Number -> v.toDouble()
    is String -> v.trim().toDoubleOrNull()
    else -> null
}

internal fun JSONObject.needNum(name: String): Double {
    val v = num(name) ?: throw OpError("\"$name\" is missing (a number)")
    if (v.isNaN() || v.isInfinite()) throw OpError("\"$name\" must be a number")
    return v
}

internal fun JSONObject.int(name: String): Int? = num(name)?.takeIf { !it.isNaN() && !it.isInfinite() }?.toInt()

internal fun JSONObject.bool(name: String): Boolean? = when (val v = opt(name)) {
    is Boolean -> v
    is Number -> v.toInt() != 0
    is String -> when (v.trim().lowercase()) {
        "1", "true", "on", "yes" -> true
        "0", "false", "off", "no", "" -> false
        else -> null
    }
    else -> null
}

/** A 0..1 coordinate. */
internal fun JSONObject.unit(name: String): Double {
    val v = needNum(name)
    if (v < 0 || v > 1) throw OpError("\"$name\" must be between 0 and 1")
    return v
}

internal fun JSONObject.limit(default: Int, max: Int): Int = (int("limit") ?: default).coerceIn(1, max)

internal fun jsonOf(vararg pairs: Pair<String, Any?>): JSONObject {
    val o = JSONObject()
    for ((k, v) in pairs) o.put(k, v ?: JSONObject.NULL)
    return o
}

// ------------------------------------------------------------------------------------- Android

internal fun Context.granted(permission: String) =
    ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

/** A receiver for this app only (system broadcasts and our own PendingIntents still arrive). */
internal fun Context.registerLocal(receiver: BroadcastReceiver, filter: IntentFilter) {
    ContextCompat.registerReceiver(this, receiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
}

internal fun Context.unregisterQuietly(receiver: BroadcastReceiver?) {
    if (receiver == null) return
    try { unregisterReceiver(receiver) } catch (e: Exception) { }
}

internal fun Context.channel(id: String, name: String, importance: Int, silent: Boolean = false) {
    if (Build.VERSION.SDK_INT < 26) return
    val nm = getSystemService(NotificationManager::class.java) ?: return
    if (nm.getNotificationChannel(id) != null) return
    val ch = NotificationChannel(id, name, importance)
    if (silent) {
        ch.setSound(null, null)
        ch.enableVibration(false)
    }
    nm.createNotificationChannel(ch)
}

/** PendingIntent flags: immutable (Android 12+ requires one of the two). */
internal const val PI_FLAGS = android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE

/** Blocks until the service runs with the foreground types (PhoneContext.foreground). Not on the main thread. */
internal fun PhoneContext.foregroundBlocking(owner: String, types: Int, what: String, timeoutMs: Long = 15_000) {
    val latch = CountDownLatch(1)
    var ok = false
    var why: String? = null
    foreground(owner, types) { o, r ->
        ok = o
        why = r
        latch.countDown()
    }
    if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) throw OpError("Android did not let the app use the $what in time - try again", 504)
    if (!ok) throw OpError(why ?: "Android did not let the app use the $what from the background", 409)
}

/**
 * Starts an activity of another app (or a URL) from the background: through the core's visible
 * TrampolineActivity, so Android allows it.
 */
internal fun PhoneContext.startFromBackground(intent: Intent) {
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    withActivity { act ->
        try {
            act.startActivity(intent)
        } catch (e: Exception) {
            log(Log.WARN, "start activity: ${e.javaClass.simpleName}")
        } finally {
            act.finish()
        }
    }
}

/**
 * Shows one of our own activities over the lock screen from the background (lost mode, ringing, a call):
 * with "display over other apps" the app may start activities from the background; Android 15+ also wants
 * a visible overlay window at that moment, so a 1-pixel invisible one is shown for a second around it.
 * Falls back to the core's TrampolineActivity.
 */
@SuppressLint("InlinedApi")
internal fun showOwnActivity(ctx: PhoneContext?, app: Context, intent: Intent) {
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
    if (!Settings.canDrawOverlays(app)) {
        if (ctx != null) ctx.startFromBackground(intent)
        else try { app.startActivity(intent) } catch (e: Exception) { log(Log.WARN, "start activity: ${e.javaClass.simpleName}") }
        return
    }
    mainHandler.post {
        val wm = app.getSystemService(WindowManager::class.java)
        val view = View(app)
        val type = if (Build.VERSION.SDK_INT >= 26) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                   else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        val lp = WindowManager.LayoutParams(1, 1, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL, PixelFormat.TRANSLUCENT)
        val added = try { wm?.addView(view, lp); wm != null } catch (e: Exception) { false }
        mainHandler.postDelayed({
            try { app.startActivity(intent) } catch (e: Exception) { log(Log.WARN, "start activity: ${e.javaClass.simpleName}") }
            if (added) mainHandler.postDelayed({ try { wm?.removeView(view) } catch (e: Exception) { } }, 1500)
        }, if (added) 150 else 0)
    }
}

/**
 * startActivityForResult from any activity we are given (the core's TrampolineActivity): a headless
 * platform fragment receives the result, so the activity needs no code for it. The activity must stay
 * alive (not finished, not noHistory) until the result arrives.
 */
@Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
class ResultFragment : android.app.Fragment() {
    companion object {
        private val pending = ConcurrentHashMap<Int, (Int, Intent?) -> Unit>()
        private val ids = AtomicInteger(0x100)

        fun launch(activity: Activity, intent: Intent, callback: (resultCode: Int, data: Intent?) -> Unit): Int {
            val req = ids.incrementAndGet() and 0x7FFF
            pending[req] = callback
            val f = ResultFragment()
            f.arguments = Bundle().apply {
                putInt("req", req)
                putParcelable("intent", intent)
            }
            activity.fragmentManager.beginTransaction().add(f, "arstro-result-$req").commitAllowingStateLoss()
            return req
        }

        /** Gives up on a request (no answer): the callback gets RESULT_CANCELED. */
        fun cancel(req: Int) { pending.remove(req)?.invoke(Activity.RESULT_CANCELED, null) }
    }

    private var started = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        started = savedInstanceState?.getBoolean("started") ?: false
        val args = arguments ?: return
        if (!started) {
            started = true
            val intent: Intent? = if (Build.VERSION.SDK_INT >= 33) args.getParcelable("intent", Intent::class.java)
                                  else args.getParcelable("intent")
            try {
                startActivityForResult(intent, args.getInt("req"))
            } catch (e: Exception) {
                pending.remove(args.getInt("req"))?.invoke(Activity.RESULT_CANCELED, null)
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("started", started)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        pending.remove(requestCode)?.invoke(resultCode, data)
        try { fragmentManager?.beginTransaction()?.remove(this)?.commitAllowingStateLoss() } catch (e: Exception) { }
    }
}

// ------------------------------------------------------------------------------------- typed lambdas

internal fun op(f: (PhoneContext, JSONObject) -> Any?) = f
internal fun route(f: (PhoneContext, com.arstro.arstro_remote_client.core.Request) -> com.arstro.arstro_remote_client.core.Response) = f
internal fun socket(f: (PhoneContext, com.arstro.arstro_remote_client.core.WsSession) -> com.arstro.arstro_remote_client.core.WsHandler) = f
