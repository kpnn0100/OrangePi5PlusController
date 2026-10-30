package com.arstro.arstro_remote_client.features

import android.accessibilityservice.AccessibilityService
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.graphics.Point
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.PowerManager
import android.util.DisplayMetrics
import android.util.Log
import android.view.Display
import android.view.ViewConfiguration
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import com.arstro.arstro_remote_client.core.Response
import com.arstro.arstro_remote_client.core.SpecialAccess
import com.arstro.arstro_remote_client.core.TrampolineActivity
import com.arstro.arstro_remote_client.core.WsHandler
import com.arstro.arstro_remote_client.core.WsSession
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Screen view & control (PHONE.md "screen", PHN-04). One MediaProjection and its single VirtualDisplay are
 * kept while the service runs (Android 14+: one display per projection, consent per session); with nobody
 * watching the display's surface is null, with viewers it feeds an H.264 encoder sized to the viewers'
 * `quality.max` (default 720 on the long side), rebuilt on rotation / quality changes through
 * VirtualDisplay.resize + setSurface. Control goes through ControlService (accessibility).
 *
 * The video always shows the screen upright in its current rotation, so the config's `rotation` is 0 and
 * control coordinates are 0..1 of the picture as shown.
 *
 * Android captures nothing while the phone's screen is off: opening the screen view turns it on, and
 * while someone watches it stays on (dimmed after the phone's own timeout). The power button still
 * turns it off; `screen_on` in the state says so and the viewer's Wake key turns it on again.
 */
object ScreenFeature : Feature("screen", "Screen view & control") {
    override val specialAccess = listOf(SpecialAccess.SCREEN_CAPTURE, SpecialAccess.ACCESSIBILITY)

    private const val FG = "screen"
    private const val DEFAULT_MAX = 720
    private const val CONSENT_TIMEOUT_MS = 120_000L

    private val lock = Any()
    @Volatile private var ctx: PhoneContext? = null
    @Volatile private var app: Context? = null
    @Volatile private var projection: MediaProjection? = null
    @Volatile private var display: VirtualDisplay? = null
    @Volatile private var encoder: AvcEncoder? = null
    private var encoderKey: String? = null
    private var displaySize: Pair<Int, Int>? = null
    @Volatile private var waiting = false
    @Volatile private var lastError: String? = null
    private var shooting = false
    private var thread: HandlerThread? = null
    @Volatile private var handler: Handler? = null
    private var displayListener: DisplayManager.DisplayListener? = null
    private var screenReceiver: BroadcastReceiver? = null
    private var keepOn: PowerManager.WakeLock? = null
    private val controls = Executors.newSingleThreadExecutor { r -> Thread(r, "arstro-control").apply { isDaemon = true } }
    private val hub = VideoHub { encoder?.requestKeyFrame() }

    // ------------------------------------------------------------------------------------- API

    override fun ops() = mapOf(
        "screen.status" to op { c, _ -> remember(c); status(c.app) },
        "screen.start" to op { c, _ -> remember(c); startConsent(c) },
        "screen.stop" to op { c, _ -> remember(c); stopProjection(c, "screen capture stopped"); status(c.app) },
        "screen.tap" to op { c, a -> control(c, "tap", a, true) },
        "screen.long" to op { c, a -> control(c, "long", a, true) },
        "screen.swipe" to op { c, a -> control(c, "swipe", a, true) },
        "screen.scroll" to op { c, a -> control(c, "scroll", a, true) },
        "screen.text" to op { c, a -> control(c, "text", a, true) },
        "screen.key" to op { c, a -> control(c, "key", a, true) },
    )

    override fun routes() = mapOf(
        "GET /api/screen/shot" to route { c, _ ->
            remember(c)
            Response.bytes(shot(c.app), "image/png", 200, listOf("Cache-Control" to "no-store"))
        },
    )

    override fun sockets() = mapOf("/ws/screen" to socket { c, s -> openViewer(c, s) })

    override fun start(ctx: PhoneContext) {
        remember(ctx)
        val dm = ctx.app.getSystemService(DisplayManager::class.java)
        val l = object : DisplayManager.DisplayListener {
            override fun onDisplayAdded(displayId: Int) {}
            override fun onDisplayRemoved(displayId: Int) {}
            override fun onDisplayChanged(displayId: Int) {
                if (displayId != Display.DEFAULT_DISPLAY) return
                val d = realDisplay(ctx.app)
                val changed = synchronized(lock) {
                    val c = displaySize != (d.w to d.h)
                    displaySize = d.w to d.h
                    c
                }
                if (changed) {
                    reconfigure()
                    publish()
                }
            }
        }
        displayListener = l
        dm?.registerDisplayListener(l, handler)
        val r = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                if (intent.action == Intent.ACTION_SCREEN_ON) encoder?.requestKeyFrame()   // the picture is back
                publish()
            }
        }
        screenReceiver = r
        ctx.app.registerLocal(r, IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_SCREEN_OFF)
        })
        publish()
    }

    override fun stop(ctx: PhoneContext) {
        stopProjection(ctx, "${title} was turned off on the phone")
        displayListener?.let { ctx.app.getSystemService(DisplayManager::class.java)?.unregisterDisplayListener(it) }
        displayListener = null
        ctx.app.unregisterQuietly(screenReceiver)
        screenReceiver = null
        hub.closeAll(1001, "${title} was turned off on the phone")
        this.ctx = null
        updateKeepOn(ctx.app)        // nobody watches any more: the phone's own screen timeout
    }

    override fun note(ctx: PhoneContext): String? = when {
        projection != null -> if (ControlService.instance == null) "capturing; remote control (Accessibility) is not connected" else null
        waiting -> "waiting for the screen capture consent on the phone"
        lastError != null -> lastError
        ControlService.instance == null -> "remote control (Accessibility) is not connected"
        else -> "screen capture starts when someone opens the screen"
    }

    /** ControlService connected / disconnected. */
    fun controlChanged() = publish()

    // ------------------------------------------------------------------------------------- state

    private fun remember(c: PhoneContext) {
        if (ctx == null) ctx = c
        if (app == null) app = c.app
        synchronized(lock) {
            if (thread == null) {
                val t = HandlerThread("arstro-screen").apply { start() }
                thread = t
                handler = Handler(t.looper)
            }
        }
    }

    data class Disp(val w: Int, val h: Int, val dpi: Int, val rotation: Int)

    @Suppress("DEPRECATION")
    fun realDisplay(app: Context): Disp {
        val d = app.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
        val p = Point()
        d.getRealSize(p)
        val m = DisplayMetrics()
        d.getRealMetrics(m)
        return Disp(maxOf(2, p.x), maxOf(2, p.y), m.densityDpi, d.rotation * 90)
    }

    private fun status(app: Context): JSONObject {
        val d = realDisplay(app)
        return jsonOf("consent" to (projection != null), "waiting" to waiting, "watching" to hub.count,
                      "screen_on" to app.getSystemService(PowerManager::class.java).isInteractive,
                      "width" to d.w, "height" to d.h, "rotation" to d.rotation,
                      "control" to (ControlService.instance != null), "error" to lastError)
    }

    private fun stateMessage(app: Context, reason: String? = null): String =
        status(app).put("t", "state").also { if (reason != null) it.put("reason", reason) }.toString()

    private fun publish() {
        val c = ctx ?: return
        try { c.events.publish("screen", status(c.app)) } catch (e: Exception) { }
    }

    // ------------------------------------------------------------------------------------- consent + projection

    private fun startConsent(c: PhoneContext): JSONObject {
        if (projection == null) requestConsent(c)
        return jsonOf("consent" to (projection != null), "waiting" to waiting)
    }

    private fun requestConsent(c: PhoneContext) {
        synchronized(lock) {
            if (projection != null || waiting) return
            waiting = true
            lastError = null
        }
        publish()
        c.log(Log.INFO, "screen: asking for the capture consent")
        val asked = java.util.concurrent.atomic.AtomicBoolean(false)
        mainHandler.postDelayed({
            if (!asked.get() && waiting && projection == null) {
                consentFailed(c, "Android did not let the app ask for screen capture - open Arstro Remote Client on the phone and try again")
            }
        }, 20_000)
        c.withActivity { act ->
            asked.set(true)
            try {
                val mpm = act.getSystemService(MediaProjectionManager::class.java)
                val intent = if (Build.VERSION.SDK_INT >= 34)
                    mpm.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
                else mpm.createScreenCaptureIntent()
                var timeout: Runnable? = null
                var answered = false
                val onResult = { code: Int, data: android.content.Intent? ->
                    timeout?.let { mainHandler.removeCallbacks(it) }
                    if (answered) {
                        // already answered (or given up)
                    } else if (code == Activity.RESULT_OK && data != null) {
                        answered = true
                        // Android 14+: the mediaProjection service type only after the consent, before getMediaProjection
                        c.foreground(FG, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION) { ok, why ->
                            mainHandler.post {
                                if (ok) startProjection(c, code, data)
                                else consentFailed(c, why ?: "Android did not allow screen capture now - try again")
                                act.finish()
                            }
                        }
                    } else {
                        answered = true
                        consentFailed(c, "screen capture was not allowed on the phone")
                        act.finish()
                    }
                }
                // the core's TrampolineActivity has its own result hook; any other activity gets a headless fragment
                val req = if (act is TrampolineActivity) { act.startForResult(intent, onResult); -1 }
                          else ResultFragment.launch(act, intent, onResult)
                timeout = Runnable { if (req >= 0) ResultFragment.cancel(req) else onResult(Activity.RESULT_CANCELED, null) }
                mainHandler.postDelayed(timeout, CONSENT_TIMEOUT_MS)
            } catch (e: Exception) {
                consentFailed(c, "the phone could not ask for screen capture (${e.javaClass.simpleName})")
                act.finish()
            }
        }
    }

    private fun consentFailed(c: PhoneContext, why: String) {
        synchronized(lock) {
            waiting = false
            lastError = why
        }
        c.log(Log.INFO, "screen: $why")
        hub.text(stateMessage(c.app, why))
        hub.closeAll(1000, why)
        publish()
    }

    private fun startProjection(c: PhoneContext, code: Int, data: android.content.Intent) {
        if (ctx == null) {                 // the feature was turned off while the consent was open
            c.releaseForeground(FG)
            return
        }
        try {
            val mpm = c.app.getSystemService(MediaProjectionManager::class.java)
            val p = mpm.getMediaProjection(code, data) ?: throw IllegalStateException("no projection")
            val h = handler
            p.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() { projectionStopped(p) }
            }, h)
            val d = realDisplay(c.app)
            val vd = p.createVirtualDisplay("Arstro screen", d.w, d.h, d.dpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, null, null, h)
            synchronized(lock) {
                projection = p
                display = vd
                displaySize = d.w to d.h
                waiting = false
                lastError = null
            }
            c.log(Log.INFO, "screen: capture running")
            hub.text(stateMessage(c.app))
            reconfigure()
        } catch (e: Exception) {
            c.releaseForeground(FG)
            consentFailed(c, "screen capture could not start (${e.javaClass.simpleName})")
            return
        }
        publish()
    }

    /** Android or the user stopped the projection (status bar chip, ...). */
    private fun projectionStopped(p: MediaProjection) {
        val c = ctx
        synchronized(lock) {
            if (projection !== p) return
            projection = null
            releaseDisplayLocked()
        }
        c?.releaseForeground(FG)
        c?.log(Log.INFO, "screen: capture stopped on the phone")
        val why = "screen capture was stopped on the phone"
        app?.let { hub.text(stateMessage(it, why)) }
        hub.closeAll(1000, why)
        publish()
    }

    private fun stopProjection(c: PhoneContext, why: String) {
        val p: MediaProjection?
        synchronized(lock) {
            p = projection
            projection = null
            waiting = false
            releaseDisplayLocked()
        }
        if (p != null) {
            try { p.stop() } catch (e: Exception) { }
            hub.text(stateMessage(c.app, why))
            hub.closeAll(1000, why)
        }
        c.releaseForeground(FG)
        publish()
    }

    private fun releaseDisplayLocked() {
        try { display?.release() } catch (e: Exception) { }
        display = null
        val e = encoder
        encoder = null
        encoderKey = null
        hub.clearConfig()
        e?.let { enc -> Thread { enc.release() }.start() }
    }

    // ------------------------------------------------------------------------------------- encoding

    private val encListener = object : AvcEncoder.Listener {
        override fun onConfig(encoder: AvcEncoder, codec: String) {
            if (encoder === this@ScreenFeature.encoder) hub.config(codec, encoder.width, encoder.height, 0)
        }

        override fun onFrame(encoder: AvcEncoder, packet: ByteArray, key: Boolean) {
            if (encoder === this@ScreenFeature.encoder) hub.frame(packet, key)
        }

        override fun onError(encoder: AvcEncoder, error: Exception) {
            if (encoder !== this@ScreenFeature.encoder) return
            lastError = "the video encoder stopped (${error.javaClass.simpleName})"
            hub.text(JSONObject().put("t", "error").put("error", lastError).toString())
        }
    }

    /** Matches the encoder to the viewers (none: surface null, no encoder). Runs on the screen thread. */
    private fun reconfigure() {
        val h = handler ?: return
        if (Thread.currentThread() !== h.looper.thread) {
            h.post { reconfigure() }
            return
        }
        val a = app ?: return
        var old: AvcEncoder? = null
        synchronized(lock) {
            val vd = display ?: return
            if (shooting) return
            if (hub.count == 0) {
                vd.surface = null
                old = encoder
                encoder = null
                encoderKey = null
                hub.clearConfig()
            } else {
                val d = realDisplay(a)
                val (qmax, qfps, qkbps) = hub.maxQuality()
                val fps = (if (qfps > 0) qfps else 30).coerceIn(5, 60)
                val (w, hgt) = VideoSizes.fit(d.w, d.h, if (qmax > 0) qmax.coerceIn(144, 4096) else DEFAULT_MAX)
                val bitrate = if (qkbps > 0) (qkbps * 1000).coerceIn(100_000, 20_000_000) else VideoSizes.bitrate(w, hgt, fps)
                val key = "$w x $hgt @ $fps $bitrate"
                if (key == encoderKey && encoder != null) return
                val enc = try {
                    AvcEncoder.create(w, hgt, fps, bitrate, 100_000, encListener)
                } catch (e: Exception) {
                    lastError = "the phone's video encoder refused ${w}x$hgt (${e.javaClass.simpleName})"
                    hub.text(JSONObject().put("t", "error").put("error", lastError).toString())
                    return
                }
                old = encoder
                hub.clearConfig()
                encoder = enc
                encoderKey = key
                vd.resize(enc.width, enc.height, d.dpi)
                vd.surface = enc.surface
            }
        }
        old?.release()
    }

    // ------------------------------------------------------------------------------------- viewers

    private fun openViewer(c: PhoneContext, s: WsSession): WsHandler {
        remember(c)
        val v = hub.add(s)
        updateKeepOn(c.app)          // turns the screen on: a dark screen has no picture
        if (projection == null) {
            s.sendText(stateMessage(c.app))
            requestConsent(c)
        } else {
            reconfigure()
            hub.greet(v)
        }
        publish()
        return object : WsHandler {
            override fun onText(session: WsSession, text: String) {
                val m = try { JSONObject(text) } catch (e: Exception) { return }
                when (val t = m.optString("t")) {
                    "keyframe" -> { v.needKey = true; encoder?.requestKeyFrame() }
                    "quality" -> {
                        v.max = m.int("max") ?: 0
                        v.fps = m.int("fps") ?: 0
                        v.kbps = m.int("kbps") ?: 0
                        reconfigure()
                    }
                    "tap", "long", "swipe", "scroll", "text", "key" -> controls.execute {
                        try {
                            control(c, t, m, false)
                        } catch (e: OpError) {
                            try { session.sendText(JSONObject().put("t", "error").put("error", e.message).toString()) } catch (x: Exception) { }
                        } catch (e: Exception) {
                            c.log(Log.WARN, "screen: control failed: ${e.javaClass.simpleName}")
                        }
                    }
                }
            }

            override fun onClose(session: WsSession, code: Int, reason: String) {
                hub.remove(session)
                updateKeepOn(c.app)
                reconfigure()
                publish()
            }
        }
    }

    /** While someone watches: the screen on (it was off: turned on), dimmed after the phone's timeout.
     *  Nobody: the phone's own timeout again. */
    @Suppress("DEPRECATION")
    @android.annotation.SuppressLint("WakelockTimeout")    // released when the last viewer leaves / in stop
    private fun updateKeepOn(app: Context) {
        synchronized(lock) {
            val held = keepOn
            if (hub.count > 0 && ctx != null) {
                if (held?.isHeld == true) return
                keepOn = app.getSystemService(PowerManager::class.java)
                    .newWakeLock(PowerManager.SCREEN_DIM_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "arstro:screen-viewers")
                    .apply { setReferenceCounted(false); acquire() }
            } else {
                held?.let { if (it.isHeld) it.release() }
                keepOn = null
            }
        }
    }

    // ------------------------------------------------------------------------------------- control

    private fun control(c: PhoneContext, type: String, a: JSONObject, wait: Boolean): Any? {
        if (type == "key") {
            val key = a.needStr("key")
            if (key == "wake") return wake(c.app, wait)
            // the web UI sends these typing keys as keys: they are text edits of the focused field
            if (key == "enter") { service().type("\n"); return null }
            if (key == "backspace") { service().type("\b"); return null }
            val action = when (key) {
                "back" -> AccessibilityService.GLOBAL_ACTION_BACK
                "home" -> AccessibilityService.GLOBAL_ACTION_HOME
                "recents" -> AccessibilityService.GLOBAL_ACTION_RECENTS
                "notifications" -> AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS
                "quick_settings" -> AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS
                "power_dialog" -> AccessibilityService.GLOBAL_ACTION_POWER_DIALOG
                "lock" -> if (Build.VERSION.SDK_INT >= 28) AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN
                          else throw OpError("locking from here needs Android 9 or newer", 409)
                else -> throw OpError("unknown key \"$key\" - use back, home, recents, notifications, quick_settings, lock, power_dialog, wake, enter or backspace")
            }
            service().global(action)
            return null
        }
        val svc = service()
        val d = realDisplay(c.app)
        when (type) {
            "tap" -> svc.gesture(Gestures.tap(a.unit("x"), a.unit("y"), d.w, d.h), wait)
            "long" -> svc.gesture(Gestures.longPress(a.unit("x"), a.unit("y"), d.w, d.h,
                                                     ViewConfiguration.getLongPressTimeout().toLong()), wait)
            "swipe" -> svc.gesture(Gestures.swipe(a.unit("x1"), a.unit("y1"), a.unit("x2"), a.unit("y2"),
                                                  a.num("ms")?.toLong(), d.w, d.h), wait)
            "scroll" -> svc.gesture(Gestures.scroll(a.unit("x"), a.unit("y"), a.needNum("dy"), d.w, d.h)
                                        ?: throw OpError("\"dy\" must not be 0"), wait)
            "text" -> svc.type(a.str("text")?.takeIf { it.isNotEmpty() } ?: throw OpError("\"text\" is missing"))
        }
        return null
    }

    private fun service(): ControlService = ControlService.instance
        ?: throw OpError("remote control is off - turn on Accessibility for Arstro Remote Client on the phone", 409)

    @Suppress("DEPRECATION")
    private fun wake(app: Context, wait: Boolean): Any? {
        val pm = app.getSystemService(PowerManager::class.java)
        if (pm.isInteractive) return null
        val wl = pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or
                                PowerManager.ON_AFTER_RELEASE, "arstro:wake")
        wl.acquire(3000)
        if (wait && !isMainThread()) {
            for (i in 0 until 10) {
                if (pm.isInteractive) return null
                Thread.sleep(100)
            }
            throw OpError("Android did not let the app turn the screen on - press the power button", 409)
        }
        return null
    }

    // ------------------------------------------------------------------------------------- screenshot

    private fun shot(app: Context): ByteArray {
        val vd: VirtualDisplay
        synchronized(lock) {
            vd = display ?: throw OpError("screen capture not running - open the screen once", 409)
            if (shooting) throw OpError("a screenshot is being taken - try again", 409)
            shooting = true
        }
        val d = realDisplay(app)
        var bmp: Bitmap? = null
        val latch = CountDownLatch(1)
        val reader = ImageReader.newInstance(d.w, d.h, PixelFormat.RGBA_8888, 2)
        try {
            reader.setOnImageAvailableListener({ r ->
                val img = try { r.acquireLatestImage() } catch (e: Exception) { null }
                if (img != null) {
                    try {
                        if (bmp == null) {
                            bmp = toBitmap(img)
                            latch.countDown()
                        }
                    } catch (e: Exception) {
                        latch.countDown()
                    } finally {
                        img.close()
                    }
                }
            }, handler)
            synchronized(lock) {
                vd.resize(d.w, d.h, d.dpi)
                vd.surface = reader.surface
            }
            latch.await(3, TimeUnit.SECONDS)
        } finally {
            synchronized(lock) {
                shooting = false
                if (display === vd) {
                    val enc = encoder
                    try {
                        if (enc != null) {
                            vd.resize(enc.width, enc.height, d.dpi)
                            vd.surface = enc.surface
                        } else {
                            vd.surface = null
                        }
                    } catch (e: Exception) { }
                }
            }
            encoder?.requestKeyFrame()
            try { reader.close() } catch (e: Exception) { }
            reconfigure()
        }
        val b = bmp ?: throw OpError("the screen sent no picture - try again", 504)
        val out = ByteArrayOutputStream()
        b.compress(Bitmap.CompressFormat.PNG, 100, out)
        b.recycle()
        return out.toByteArray()
    }

    private fun toBitmap(img: Image): Bitmap {
        val plane = img.planes[0]
        val ps = plane.pixelStride
        val rs = plane.rowStride
        val bw = rs / ps
        val full = Bitmap.createBitmap(bw, img.height, Bitmap.Config.ARGB_8888)
        full.copyPixelsFromBuffer(plane.buffer)
        if (bw == img.width) return full
        val crop = Bitmap.createBitmap(full, 0, 0, img.width, img.height)
        full.recycle()
        return crop
    }
}
