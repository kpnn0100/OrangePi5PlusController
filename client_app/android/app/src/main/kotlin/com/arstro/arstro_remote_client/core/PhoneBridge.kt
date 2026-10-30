package com.arstro.arstro_remote_client.core

import android.Manifest
import android.app.Activity
import android.content.ActivityNotFoundException
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.net.URI

/**
 * The app's page and first-run wizard <-> the phone service (PHONE.md "The app's
 * MethodChannel"):
 *
 *   MethodChannel "arstro/phone"         state, setFeature, request, setRelay, key, rotateKey, start,
 *                                        stop, finishFirstRun (+ openAppInfo)
 *   EventChannel  "arstro/phone/events"  the state map after every change
 *
 * `request` asks for what a feature (or one permission / special access) still needs, step by step:
 * the runtime permissions in one system dialog, background location after foreground location, then
 * each special access on its settings page; it answers with the state when the user is back.
 */
class PhoneBridge(private val activity: Activity, messenger: BinaryMessenger) : MethodChannel.MethodCallHandler {
    companion object {
        private const val REQ_PERMS = 4201
        private const val REQ_SPECIAL = 4202
        private const val BG = "android.permission.ACCESS_BACKGROUND_LOCATION"
    }

    private val ctx = activity.applicationContext
    private val store = Store(ctx)
    private val main = Handler(Looper.getMainLooper())
    private val methods = MethodChannel(messenger, "arstro/phone")
    private var sink: EventChannel.EventSink? = null
    private val onService: () -> Unit = { main.post { push() } }

    private var queue: ArrayDeque<Permissions.Need>? = null
    private var pending: MethodChannel.Result? = null
    private var waitingSpecial = false
    private var pausedSinceSpecial = false

    init {
        methods.setMethodCallHandler(this)
        EventChannel(messenger, "arstro/phone/events").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(arguments: Any?, events: EventChannel.EventSink?) { sink = events; push() }
            override fun onCancel(arguments: Any?) { sink = null }
        })
        PhoneService.addListener(onService)
    }

    fun dispose() {
        PhoneService.removeListener(onService)
        methods.setMethodCallHandler(null)
        try { pending?.success(state()) } catch (_: Exception) {}
        pending = null
        queue = null
    }

    private fun push() {
        try { sink?.success(state()) } catch (e: Exception) { Log.w(PhoneService.TAG, "phone state push failed", e) }
    }

    // ------------------------------------------------------------------------------ state
    fun state(): Map<String, Any?> {
        val svc = PhoneService.instance
        val agent = if (svc != null) svc.agent else null
        val features = Features.all.map { f ->
            val on = store.isOn(f.id)
            val missing = Permissions.missing(ctx, f)
            val note = if (svc != null) try { f.note(svc) } catch (e: Exception) { null } else null
            mapOf("id" to f.id, "title" to f.title, "on" to on, "ready" to (on && missing.isEmpty()),
                  "missing" to missing.map { it.toMap() }, "note" to note)
        }
        return mapOf(
            "running" to (svc != null),
            "enabled" to store.enabled,
            "relay" to mapOf(
                "state" to (agent?.state ?: "offline"),
                "error" to agent?.error,
                "url" to store.url,
                "name" to store.name,
                "id" to store.id,
                "transport" to agent?.transport,
                "viewers" to (agent?.viewers()?.map { it.toMap() } ?: emptyList<Any>()),
                "token_set" to store.token.isNotEmpty(),
                "relay_version" to agent?.relayVersion,
            ),
            "key_id" to store.keyId,
            "first_run" to store.firstRun,
            "features" to features,
            "service" to mapOf("missing" to Permissions.serviceMissing(ctx).map { it.toMap() }),
            "sdk" to Build.VERSION.SDK_INT,
            "default_url" to Store.DEFAULT_URL,
        )
    }

    private fun refreshService() {
        PhoneService.instance?.refresh()
    }

    // ------------------------------------------------------------------------------ methods
    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        try {
            when (call.method) {
                "state" -> result.success(state())
                "setFeature" -> {
                    val id = call.argument<String>("id")
                    if (id == null || Features.byId(id) == null) return result.error("bad_feature", "No feature $id", null)
                    val on = call.argument<Boolean>("on") == true
                    store.setOn(id, on)
                    // only Find my phone uses the device admin (lock now): give it back when that is
                    // turned off - an active device admin also blocks uninstalling the app
                    if (id == "find" && !on) releaseDeviceAdmin()
                    refreshService()
                    result.success(state())
                    push()
                }
                "request" -> request(call, result)
                "setRelay" -> setRelay(call, result)
                "key" -> result.success(mapOf("key" to store.key, "key_id" to store.keyId))
                "rotateKey" -> {
                    store.rotateKey()
                    refreshService()               // a new key drops the viewers with the old one
                    result.success(mapOf("key" to store.key, "key_id" to store.keyId))
                    push()
                }
                "start" -> {
                    store.enabled = true
                    try { PhoneService.start(ctx) } catch (e: Exception) {
                        return result.error("start_failed", "Android did not let the service start: ${e.message}", null)
                    }
                    result.success(state())
                }
                "stop" -> {
                    store.enabled = false
                    PhoneService.stop(ctx)
                    result.success(state())
                }
                "finishFirstRun" -> {
                    store.firstRun = false
                    result.success(state())
                }
                "openAppInfo" -> {
                    try { activity.startActivity(Permissions.appInfoIntent(ctx)) } catch (_: ActivityNotFoundException) {}
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        } catch (e: Exception) {
            Log.w(PhoneService.TAG, "phone ${call.method} failed: ${e.javaClass.simpleName}")
            result.error("failed", e.message ?: e.javaClass.simpleName, null)
        }
    }

    private fun setRelay(call: MethodCall, result: MethodChannel.Result) {
        val url = call.argument<String>("url")?.trim()
        if (url != null) {
            val ok = try {
                val u = URI(url)
                (u.scheme == "https" || u.scheme == "http") && !u.host.isNullOrEmpty()
            } catch (e: Exception) { false }
            if (!ok) return result.error("bad_url", "The address must look like https://server/path", null)
            store.url = url
        }
        call.argument<String>("token")?.let { if (it.isNotBlank()) store.token = it }
        call.argument<String>("name")?.let { if (it.isNotBlank()) store.name = it }
        refreshService()
        result.success(state())
        push()
    }

    // ------------------------------------------------------------------------------ asking
    private fun request(call: MethodCall, result: MethodChannel.Result) {
        if (pending != null) return result.error("busy", "Another request is still open", null)
        val id = call.argument<String>("id")
        val name = call.argument<String>("name")
        val needs: List<Permissions.Need> = when {
            id != null -> {
                val f = Features.byId(id) ?: return result.error("bad_feature", "No feature $id", null)
                Permissions.missing(ctx, f)
            }
            name != null -> {
                val special = runCatching { SpecialAccess.valueOf(name) }.getOrNull()
                when {
                    special != null -> listOf(Permissions.Need(name, "special", special.title))
                    name.startsWith("android.permission.") -> listOf(Permissions.Need(name, "permission", Permissions.title(name)))
                    else -> return result.error("bad_name", "Unknown permission $name", null)
                }
            }
            else -> return result.error("bad_args", "request needs id or name", null)
        }
        pending = result
        queue = ArrayDeque(needs)
        next()
    }

    private fun next() {
        val q = queue ?: return
        // 1. runtime permissions, in one dialog (background location comes after)
        val perms = q.filter { it.kind == "permission" && it.name != BG }
        if (perms.isNotEmpty()) {
            q.removeAll(perms)
            val ask = perms.map { it.name }.filter { !Permissions.granted(ctx, it) }
            if (ask.isNotEmpty() && Build.VERSION.SDK_INT >= 23) {
                activity.requestPermissions(ask.toTypedArray(), REQ_PERMS)
                return
            }
        }
        // 2. background location: only after (precise) location is allowed
        q.firstOrNull { it.name == BG }?.let { bg ->
            q.remove(bg)
            val fg = Permissions.granted(ctx, Manifest.permission.ACCESS_FINE_LOCATION) ||
                     Permissions.granted(ctx, Manifest.permission.ACCESS_COARSE_LOCATION)
            if (fg && !Permissions.granted(ctx, BG)) {
                activity.requestPermissions(arrayOf(BG), REQ_PERMS)
                return
            }
        }
        // 3. special accesses, one settings page at a time
        while (true) {
            val sp = q.removeFirstOrNull() ?: break
            if (sp.kind != "special") continue
            val access = runCatching { SpecialAccess.valueOf(sp.name) }.getOrNull() ?: continue
            if (Permissions.granted(ctx, access)) continue
            val intent = Permissions.intentFor(ctx, access) ?: continue
            waitingSpecial = true
            pausedSinceSpecial = false
            try {
                activity.startActivityForResult(intent, REQ_SPECIAL)
            } catch (e: ActivityNotFoundException) {
                try { activity.startActivityForResult(Permissions.fallbackIntent(ctx, access), REQ_SPECIAL) } catch (e2: Exception) {
                    waitingSpecial = false
                    continue
                }
            } catch (e: SecurityException) {
                waitingSpecial = false
                continue
            }
            return
        }
        // done
        queue = null
        val r = pending
        pending = null
        refreshService()
        r?.success(state())
        push()
    }

    fun onRequestPermissionsResult(requestCode: Int): Boolean {
        if (requestCode != REQ_PERMS) return false
        main.post { next() }
        return true
    }

    fun onActivityResult(requestCode: Int): Boolean {
        if (requestCode != REQ_SPECIAL) return false
        waitingSpecial = false
        main.post { next() }
        return true
    }

    fun onPause() {
        if (waitingSpecial) pausedSinceSpecial = true
    }

    /** Some settings pages do not answer with a result: going on when the app is back. */
    fun onResume() {
        if (waitingSpecial && pausedSinceSpecial) {
            waitingSpecial = false
            main.postDelayed({ next() }, 300)
        }
        refreshService()
        push()
    }

    private fun releaseDeviceAdmin() {
        val dpm = activity.getSystemService(android.app.admin.DevicePolicyManager::class.java) ?: return
        val admin = android.content.ComponentName(activity, com.arstro.arstro_remote_client.features.AdminReceiver::class.java)
        if (dpm.isAdminActive(admin)) dpm.removeActiveAdmin(admin)
    }
}
