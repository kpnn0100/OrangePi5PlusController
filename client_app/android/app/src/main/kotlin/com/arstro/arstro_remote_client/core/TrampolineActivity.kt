package com.arstro.arstro_remote_client.core

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log

/**
 * An invisible activity that brings the app to the foreground for a moment (PHONE.md "Background
 * rules"): Android lets a service add while-in-use foreground types (camera, microphone) and ask the
 * screen-capture consent only while the app has an activity on top. Allowed from the background
 * because the app holds "display over other apps".
 *
 * `run(ctx, block)` starts it (or uses the one on screen) and calls `block(activity)` on the main
 * thread once it is resumed. The block calls `activity.finish()` when done; for a result use
 * `(activity as TrampolineActivity).startForResult(intent) { resultCode, data -> ... }`. With nothing
 * left to do it finishes itself after a moment.
 */
class TrampolineActivity : Activity() {
    companion object {
        private const val TAG = "ArstroPhone"
        private const val IDLE_MS = 1500L
        private val main = Handler(Looper.getMainLooper())
        private val pending = ArrayDeque<(Activity) -> Unit>()
        private var current: TrampolineActivity? = null
        private var launchedAt = 0L

        /** Run `block` with a resumed TrampolineActivity. `onTimeout` runs if none came up in time
         *  (Android did not let the app start an activity from the background). Main thread. */
        fun run(ctx: Context, block: (Activity) -> Unit, onTimeout: (() -> Unit)? = null, timeoutMs: Long = 10_000) {
            main.post {
                val act = current
                if (act != null && act.resumed && !act.isFinishing) {
                    act.runBlock(block)
                    return@post
                }
                pending.addLast(block)
                val now = System.currentTimeMillis()
                if (current == null || current?.isFinishing == true || now - launchedAt > 2000) {
                    launchedAt = now
                    try {
                        ctx.startActivity(Intent(ctx, TrampolineActivity::class.java).addFlags(
                            Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION or
                            Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS))
                    } catch (e: Exception) {
                        Log.w(TAG, "cannot bring the app to the foreground: ${e.javaClass.simpleName}")
                    }
                }
                main.postDelayed({
                    if (pending.remove(block)) {
                        Log.w(TAG, "the app could not come to the foreground in time")
                        onTimeout?.invoke()
                    }
                }, timeoutMs)
            }
        }
    }

    private var resumed = false
    private var nextCode = 7300
    private val results = HashMap<Int, (Int, Intent?) -> Unit>()

    /** startActivityForResult with a callback (e.g. the screen-capture consent). */
    fun startForResult(intent: Intent, callback: (resultCode: Int, data: Intent?) -> Unit) {
        val code = nextCode++
        results[code] = callback
        try {
            startActivityForResult(intent, code)
        } catch (e: Exception) {
            results.remove(code)
            callback(RESULT_CANCELED, null)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)      // on top also when the phone is locked
            setTurnScreenOn(true)
        }
        current = this
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        current = this
        while (pending.isNotEmpty() && !isFinishing) runBlock(pending.removeFirst())
        scheduleIdle()
    }

    override fun onPause() {
        resumed = false
        super.onPause()
    }

    private fun runBlock(block: (Activity) -> Unit) {
        try { block(this) } catch (e: Exception) { Log.w(TAG, "trampoline task failed: ${e.javaClass.simpleName}") }
    }

    private fun scheduleIdle() {
        main.postDelayed({
            if (resumed && results.isEmpty() && pending.isEmpty() && !isFinishing) finish()
        }, IDLE_MS)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION") super.onActivityResult(requestCode, resultCode, data)
        val cb = results.remove(requestCode) ?: return
        try { cb(resultCode, data) } catch (e: Exception) { Log.w(TAG, "result callback failed: ${e.javaClass.simpleName}") }
        scheduleIdle()
    }

    override fun onDestroy() {
        if (current === this) current = null
        val left = results.values.toList()
        results.clear()
        left.forEach { try { it(RESULT_CANCELED, null) } catch (_: Exception) {} }
        super.onDestroy()
    }

    override fun finish() {
        super.finish()
        if (Build.VERSION.SDK_INT >= 34) overrideActivityTransition(OVERRIDE_TRANSITION_CLOSE, 0, 0)
        else @Suppress("DEPRECATION") overridePendingTransition(0, 0)
    }
}
