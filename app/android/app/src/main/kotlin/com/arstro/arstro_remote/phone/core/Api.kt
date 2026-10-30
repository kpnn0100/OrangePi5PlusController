package com.arstro.arstro_remote.phone.core

import android.content.Context
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.InputStream

/*
 * The contract between the phone service's core (relay agent, router, service) and its features
 * (docs/phone/PHONE.md). Features only use what is here; the core implements PhoneContext and
 * calls Feature's hooks. Keep both sides and PHONE.md in sync.
 */

/** An HTTP request of a viewer (a decrypted `http` stream of the relay channel). */
class Request(
    val method: String,                       // "GET", "POST", ...
    val path: String,                         // "/api/files/list" (no query)
    val query: Map<String, List<String>>,
    val headers: Map<String, String>,         // names in lower case
    val body: InputStream?,                   // null without a body
    val contentLength: Long,                  // -1 when unknown
) {
    fun param(name: String): String? = query[name]?.firstOrNull()
}

/** The answer. `body` is streamed to the viewer in chunks; `length` -1 = unknown. */
class Response(
    val status: Int,
    val headers: List<Pair<String, String>> = emptyList(),
    val body: InputStream? = null,
    val length: Long = -1,
) {
    companion object {
        fun json(obj: Any?, status: Int = 200): Response {
            // strings, numbers, booleans and null serialised through a one-element array ("[x]" -> "x")
            val text = if (obj is JSONObject || obj is org.json.JSONArray) obj.toString()
                       else org.json.JSONArray().put(JSONObject.wrap(obj) ?: JSONObject.NULL).toString().let { it.substring(1, it.length - 1) }
            val bytes = text.toByteArray()
            return Response(status, listOf("Content-Type" to "application/json", "Cache-Control" to "no-store"),
                            ByteArrayInputStream(bytes), bytes.size.toLong())
        }

        fun bytes(data: ByteArray, contentType: String, status: Int = 200, extra: List<Pair<String, String>> = emptyList()) =
            Response(status, listOf("Content-Type" to contentType) + extra, ByteArrayInputStream(data), data.size.toLong())

        fun error(status: Int, message: String) =
            json(JSONObject().put("ok", false).put("error", message), status)
    }
}

/** A viewer's WebSocket (a `ws` stream of the relay channel). Sends are thread-safe and ordered. */
interface WsSession {
    val path: String
    val query: Map<String, List<String>>
    val isOpen: Boolean
    fun sendText(text: String)
    fun sendBinary(data: ByteArray)
    fun close(code: Int = 1000, reason: String = "")
    /** Bytes queued for the viewer and not yet sent (back off above ~1 MB: drop video frames). */
    val queued: Long
}

/** What a feature does with one WebSocket. Called on the session's reader thread. */
interface WsHandler {
    fun onText(session: WsSession, text: String) {}
    fun onBinary(session: WsSession, data: ByteArray) {}
    fun onClose(session: WsSession, code: Int, reason: String) {}
}

/** A refusal with a sentence for the viewer; becomes {"ok": false, "error": message} with `status`. */
class OpError(message: String, val status: Int = 400) : Exception(message)

/** Special accesses a feature can need (granted in system settings, not by a runtime dialog). */
enum class SpecialAccess(val title: String) {
    SCREEN_CAPTURE("Screen capture"),
    ACCESSIBILITY("Accessibility (remote control)"),
    NOTIFICATION_LISTENER("Notification access"),
    ALL_FILES("All files access"),
    OVERLAY("Display over other apps"),
    DND("Do Not Disturb access"),
    DEVICE_ADMIN("Device admin (lock)"),
    WRITE_SETTINGS("Modify system settings"),
    FULL_SCREEN_INTENT("Full-screen notifications"),
    BATTERY_UNRESTRICTED("Unrestricted battery use"),
}

/** Retained state topics and one-off events, pushed to every `/ws` viewer. */
interface EventBus {
    fun publish(topic: String, data: Any?)        // retained: sent to new viewers too
    fun event(name: String, data: JSONObject)      // {"ev": name, ...data}
}

/** What the core gives a feature. */
interface PhoneContext {
    val app: Context
    val events: EventBus
    fun isOn(featureId: String): Boolean
    fun isReady(featureId: String): Boolean
    /** Add foreground-service types (ServiceInfo.FOREGROUND_SERVICE_TYPE_*) for as long as `owner`
     *  holds them; brings the app to the foreground for a moment when Android requires it.
     *  Calls `done(true)` when the service runs with them, `done(false, reason)` if not possible. */
    fun foreground(owner: String, types: Int, done: (Boolean, String?) -> Unit)
    fun releaseForeground(owner: String)
    /** Run `block` with a visible activity (TrampolineActivity), e.g. to ask the screen-capture consent:
     *  `block(activity)` runs on the main thread; call `finish` on the activity when done. */
    fun withActivity(block: (android.app.Activity) -> Unit)
    fun log(level: Int, message: String)          // android.util.Log levels; never keys or message texts
}

/** One feature of the phone (PHONE.md "Features"). Registered in Features.kt. */
abstract class Feature(val id: String, val title: String) {
    /** Runtime permissions (android.permission.*) it needs to be ready. */
    open val permissions: List<String> = emptyList()
    /** Special accesses it needs to be ready. */
    open val specialAccess: List<SpecialAccess> = emptyList()
    /** Ops "<id>.<name>" -> handler (args JSON) -> result (JSONObject, JSONArray, String, Number, Boolean or null). */
    open fun ops(): Map<String, (PhoneContext, JSONObject) -> Any?> = emptyMap()
    /** HTTP routes "GET /api/files/download" -> handler. */
    open fun routes(): Map<String, (PhoneContext, Request) -> Response> = emptyMap()
    /** WebSocket paths "/ws/screen" -> handler factory (called once per opened socket). */
    open fun sockets(): Map<String, (PhoneContext, WsSession) -> WsHandler> = emptyMap()
    /** Called when the feature becomes on and ready while the service runs / when it stops. */
    open fun start(ctx: PhoneContext) {}
    open fun stop(ctx: PhoneContext) {}
    /** A short note for the app and /api/info (e.g. "waiting for screen capture consent"), or null. */
    open fun note(ctx: PhoneContext): String? = null
}
