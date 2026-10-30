package com.arstro.arstro_remote.phone.core

import android.content.Context
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.security.MessageDigest

/** Test doubles: a PhoneContext without Android, features with every kind of API, a gate, assets. */

class FakeContext(val hub: EventHub, private val gate: FakeGate) : PhoneContext {
    override val app: Context get() = throw UnsupportedOperationException("no Android in JVM tests")
    override val events: EventBus get() = hub
    override fun isOn(featureId: String) = gate.on[featureId] == true
    override fun isReady(featureId: String) = isOn(featureId) && gate.missingById[featureId].isNullOrEmpty()
    override fun foreground(owner: String, types: Int, done: (Boolean, String?) -> Unit) = done(true, null)
    override fun releaseForeground(owner: String) {}
    override fun withActivity(block: (android.app.Activity) -> Unit) {}
    override fun log(level: Int, message: String) {}
}

class FakeGate : FeatureGate {
    val on = HashMap<String, Boolean>()
    val missingById = HashMap<String, List<String>>()
    override fun isOn(f: Feature) = on[f.id] == true
    override fun missing(f: Feature) = missingById[f.id] ?: emptyList()
}

fun sha256(data: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }

/** n bytes of a fixed pattern (a download test checks them). */
fun pattern(n: Int) = ByteArray(n) { (it * 31 + 7).toByte() }

/** A feature with ops, routes and a socket, like the real ones. */
class EchoFeature : Feature("echo", "Echo") {
    override val permissions = listOf("android.permission.CAMERA")
    @Volatile var started = 0
    val closes = java.util.concurrent.LinkedBlockingQueue<Pair<Int, String>>()

    override fun ops(): Map<String, (PhoneContext, JSONObject) -> Any?> = mapOf(
        "echo.say" to { _, a -> JSONObject(a.toString()).put("echoed", true) },
        "echo.text" to { _, _ -> "hello" },
        "echo.number" to { _, _ -> 42 },
        "echo.null" to { _, _ -> null },
        "echo.list" to { _, _ -> listOf(1, "two", mapOf("three" to 3)) },
        "echo.fail" to { _, _ -> throw OpError("that is not possible now", 422) },
        "echo.crash" to { _, _ -> throw IllegalStateException("boom") },
        "echo.publish" to { c, a -> c.events.publish("echo", a); c.events.event("ping", JSONObject().put("n", 1)); true },
    )

    override fun routes(): Map<String, (PhoneContext, Request) -> Response> = mapOf(
        "GET /api/echo/big" to { _, r ->
            val n = r.param("n")?.toInt() ?: 0
            Response(200, listOf("Content-Type" to "application/octet-stream"), ByteArrayInputStream(pattern(n)), n.toLong())
        },
        "GET /api/echo/stream" to { _, r ->
            // unknown length: an InputStream the router streams to the end
            val n = r.param("n")?.toInt() ?: 0
            Response(200, listOf("Content-Type" to "application/octet-stream"), SlowStream(pattern(n)))
        },
        "POST /api/echo/upload" to { _, r ->
            val data = r.body?.readBytes() ?: ByteArray(0)
            Response.json(JSONObject().put("size", data.size).put("sha256", sha256(data)).put("length", r.contentLength)
                .put("type", r.headers["content-type"]).put("q", r.param("q")))
        },
    )

    override fun sockets(): Map<String, (PhoneContext, WsSession) -> WsHandler> = mapOf(
        "/ws/echo" to { _, s ->
            s.sendText("{\"hello\":\"${s.query["who"]?.firstOrNull() ?: ""}\"}")
            object : WsHandler {
                override fun onText(session: WsSession, text: String) {
                    if (text == "close") session.close(4001, "bye") else session.sendText(text)
                }
                override fun onBinary(session: WsSession, data: ByteArray) = session.sendBinary(data)
                override fun onClose(session: WsSession, code: Int, reason: String) { closes.add(code to reason) }
            }
        },
    )

    override fun start(ctx: PhoneContext) { started++ }
    override fun note(ctx: PhoneContext) = "all good"
}

class OffFeature : Feature("off", "Off thing") {
    override fun ops(): Map<String, (PhoneContext, JSONObject) -> Any?> = mapOf("off.do" to { _, _ -> true })
    override fun sockets(): Map<String, (PhoneContext, WsSession) -> WsHandler> = mapOf("/ws/off" to { _, _ -> object : WsHandler {} })
}

class NeedyFeature : Feature("needy", "Needy") {
    override val specialAccess = listOf(SpecialAccess.OVERLAY)
    override fun ops(): Map<String, (PhoneContext, JSONObject) -> Any?> = mapOf("needy.do" to { _, _ -> true })
    override fun routes(): Map<String, (PhoneContext, Request) -> Response> =
        mapOf("GET /api/needy" to { _, _ -> Response.json(JSONObject().put("ok", true)) })
}

/** Hands out its bytes in small, uneven pieces (a body without a known length). */
class SlowStream(private val data: ByteArray) : InputStream() {
    private var pos = 0
    override fun read(): Int = if (pos < data.size) data[pos++].toInt() and 0xff else -1
    override fun read(b: ByteArray, off: Int, len: Int): Int {
        if (pos >= data.size) return -1
        val n = minOf(len, data.size - pos, 1000 + pos % 7000)
        System.arraycopy(data, pos, b, off, n)
        pos += n
        return n
    }
}

val ASSETS = mapOf(
    "index.html" to "<!doctype html><html><head><title>Phone</title></head><body>phone</body></html>",
    "app.js" to "console.log('phone')",
    "style.css" to "body{}",
)

/** A router with the three test features: echo (on, ready), off (off), needy (on, not ready). */
fun testRouter(hub: EventHub = EventHub()): Pair<Router, EchoFeature> {
    val gate = FakeGate()
    gate.on["echo"] = true
    gate.on["needy"] = true
    gate.missingById["needy"] = listOf("Display over other apps")
    val echo = EchoFeature()
    val features = listOf(echo, OffFeature(), NeedyFeature())
    val ctx = FakeContext(hub, gate)
    val router = Router(ctx, { features }, gate, AssetSource { path -> ASSETS[path]?.let { ByteArrayInputStream(it.toByteArray()) } },
                        { JSONObject().put("name", "Test phone").put("model", "JVM").put("id", "jvm-phone-test01") }, hub)
    return router to echo
}

/** A WsSession that records what is sent (Router / EventHub tests). */
class RecordingSession(override val path: String, override val query: Map<String, List<String>> = emptyMap()) : WsSession {
    val texts = java.util.concurrent.LinkedBlockingQueue<String>()
    val binaries = java.util.concurrent.LinkedBlockingQueue<ByteArray>()
    @Volatile var closed: Pair<Int, String>? = null
    override val isOpen get() = closed == null
    override fun sendText(text: String) { texts.add(text) }
    override fun sendBinary(data: ByteArray) { binaries.add(data) }
    override fun close(code: Int, reason: String) { closed = code to reason }
    override val queued: Long get() = 0
}
