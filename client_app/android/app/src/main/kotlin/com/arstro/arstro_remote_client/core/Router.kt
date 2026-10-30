package com.arstro.arstro_remote_client.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream

/*
 * The phone's in-process HTTP / WebSocket dispatch (docs/phone/PHONE.md "The phone's API"): the relay
 * agent hands it every http / ws stream of a channel. Pure JVM - the service gives it the Android
 * assets and the feature state, the JVM tests give it files and fakes.
 *
 *   GET /, /static/<file>       the web UI (assets phone/)
 *   GET /api/info               the phone and its features
 *   POST /api/op/<op>           a feature's op (403 off, 409 not ready, OpError status, 500)
 *   <feature routes>            e.g. GET /api/files/download
 *   GET /ws                     events (retained topics on connect, then live)
 *   <feature sockets>           e.g. /ws/screen
 */

/** Files of the web UI: `open("index.html")` = the asset phone/index.html (null if there is none). */
fun interface AssetSource {
    fun open(path: String): InputStream?
}

/** What the router asks about a feature (the service answers from the switches and Android). */
interface FeatureGate {
    fun isOn(f: Feature): Boolean
    /** Titles of the permissions / special accesses not granted yet (empty = ready when on). */
    fun missing(f: Feature): List<String>
}

/** The EventBus of the phone: retained topics + one-off events to every `/ws` viewer. */
class EventHub(private val log: (Int, String) -> Unit = { _, _ -> }) : EventBus {
    private val lock = Object()
    private val retained = LinkedHashMap<String, String>()   // topic -> the message text
    private val sessions = LinkedHashSet<WsSession>()
    /** Called when the number of /ws viewers changes. */
    @Volatile var onWatchers: (Int) -> Unit = {}

    val watchers: Int get() = synchronized(lock) { sessions.size }

    override fun publish(topic: String, data: Any?) {
        val text = JSONObject().put("ev", "state").put("topic", topic).put("data", wrap(data)).toString()
        synchronized(lock) {
            retained[topic] = text
            sessions.forEach { send(it, text) }
        }
    }

    override fun event(name: String, data: JSONObject) {
        val msg = JSONObject().put("ev", name)
        val keys = data.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            if (k != "ev") msg.put(k, data.opt(k))
        }
        val text = msg.toString()
        synchronized(lock) { sessions.forEach { send(it, text) } }
    }

    /** The last published data of a topic as JSON text, or null. */
    fun retainedText(topic: String): String? = synchronized(lock) { retained[topic] }

    private fun send(s: WsSession, text: String) {
        // a viewer that does not take its events any more misses some instead of holding up the others
        try { if (s.isOpen && s.queued < (4L shl 20)) s.sendText(text) } catch (e: Exception) { log(LogLevel.DEBUG, "events: send failed") }
    }

    /** A new `/ws` viewer: every retained topic now, then everything published. */
    fun attach(session: WsSession): WsHandler {
        val n: Int
        synchronized(lock) {
            retained.values.forEach { send(session, it) }
            sessions.add(session)
            n = sessions.size
        }
        onWatchers(n)
        return object : WsHandler {
            override fun onText(session: WsSession, text: String) {
                // viewers may ping to keep proxies happy
                if (text.contains("\"ping\"")) send(session, "{\"ev\":\"pong\"}")
            }

            override fun onClose(session: WsSession, code: Int, reason: String) {
                val left = synchronized(lock) { sessions.remove(session); sessions.size }
                onWatchers(left)
            }
        }
    }

    companion object {
        fun wrap(data: Any?): Any = when (data) {
            null -> JSONObject.NULL
            is JSONObject, is JSONArray -> data
            else -> JSONObject.wrap(data) ?: JSONObject.NULL
        }
    }
}

class Router(
    private val ctx: PhoneContext,
    private val features: () -> List<Feature>,
    private val gate: FeatureGate,
    private val assets: AssetSource,
    /** The phone part of /api/info: name, model, manufacturer, android, sdk, app_version, id. */
    private val info: () -> JSONObject,
    val bus: EventHub,
    private val log: (Int, String) -> Unit = { _, _ -> },
) : RequestHandler {

    companion object {
        const val CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss:; " +
            "frame-src https://www.openstreetmap.org; worker-src 'self' blob:; object-src 'none'; base-uri 'none'"
        const val MAX_OP_BODY = 4 shl 20
        private val TYPES = mapOf(
            "html" to "text/html; charset=utf-8", "htm" to "text/html; charset=utf-8",
            "js" to "text/javascript; charset=utf-8", "mjs" to "text/javascript; charset=utf-8",
            "css" to "text/css; charset=utf-8", "json" to "application/json", "map" to "application/json",
            "svg" to "image/svg+xml", "png" to "image/png", "jpg" to "image/jpeg", "jpeg" to "image/jpeg",
            "gif" to "image/gif", "webp" to "image/webp", "ico" to "image/x-icon", "woff2" to "font/woff2",
            "woff" to "font/woff", "ttf" to "font/ttf", "wasm" to "application/wasm", "txt" to "text/plain; charset=utf-8",
            "webmanifest" to "application/manifest+json", "mp3" to "audio/mpeg", "ogg" to "audio/ogg", "wav" to "audio/wav",
        )

        fun contentType(name: String): String =
            TYPES[name.substringAfterLast('.', "").lowercase()] ?: "application/octet-stream"

        fun readAll(input: InputStream, limit: Int): ByteArray? {
            val out = ByteArrayOutputStream()
            val buf = ByteArray(16 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                out.write(buf, 0, n)
                if (out.size() > limit) return null
            }
            return out.toByteArray()
        }

        fun ok(data: Any?): Response = Response.json(JSONObject().put("ok", true).put("data", EventHub.wrap(data)))
    }

    private class Op(val feature: Feature, val fn: (PhoneContext, JSONObject) -> Any?)
    private class Route(val feature: Feature, val fn: (PhoneContext, Request) -> Response)
    private class Sock(val feature: Feature, val fn: (PhoneContext, WsSession) -> WsHandler)

    private val ops: Map<String, Op> by lazy {
        val m = LinkedHashMap<String, Op>()
        for (f in features()) for ((k, v) in safe(f) { f.ops() }) m[k] = Op(f, v)
        m
    }
    private val routes: Map<String, Route> by lazy {
        val m = LinkedHashMap<String, Route>()
        for (f in features()) for ((k, v) in safe(f) { f.routes() }) m[k] = Route(f, v)
        m
    }
    private val sockets: Map<String, Sock> by lazy {
        val m = LinkedHashMap<String, Sock>()
        for (f in features()) for ((k, v) in safe(f) { f.sockets() }) m[k] = Sock(f, v)
        m
    }

    private fun <K, V> safe(f: Feature, block: () -> Map<K, V>): Map<K, V> = try { block() } catch (e: Exception) {
        log(LogLevel.ERROR, "feature ${f.id}: ${e.javaClass.simpleName} while listing its API")
        emptyMap()
    }

    // ------------------------------------------------------------------------ features
    /** [{id, title, on, ready, missing: [names], note}] - /api/info, features.list, the `features` topic. */
    fun featuresJson(): JSONArray {
        val arr = JSONArray()
        for (f in features()) {
            val on = gate.isOn(f)
            val missing = try { gate.missing(f) } catch (e: Exception) { emptyList() }
            val note = try { f.note(ctx) } catch (e: Exception) { null }
            arr.put(JSONObject().put("id", f.id).put("title", f.title).put("on", on).put("ready", on && missing.isEmpty())
                .put("missing", JSONArray(missing)).put("note", note ?: JSONObject.NULL))
        }
        return arr
    }

    /** Publishes the `features` topic (the service calls it on every change). */
    fun publishFeatures() = bus.publish("features", featuresJson())

    /** null when the feature may be used now, else the refusal (403 off / 409 not ready). */
    private fun refusal(f: Feature): OpError? {
        if (!gate.isOn(f)) return OpError("${f.title} is turned off on the phone", 403)
        val missing = gate.missing(f)
        if (missing.isNotEmpty()) return OpError("${f.title} needs ${missing.joinToString(", ")} - allow it in the app", 409)
        return null
    }

    // ------------------------------------------------------------------------ HTTP
    override fun handle(req: Request): Response = try {
        dispatch(req)
    } catch (e: OpError) {
        Response.error(e.status, e.message ?: "refused")
    } catch (e: Exception) {
        log(LogLevel.WARN, "${req.method} ${req.path}: ${e.javaClass.simpleName}")
        Response.error(500, short(e))
    }

    private fun short(e: Throwable): String =
        (e.message?.takeIf { it.isNotBlank() } ?: e.javaClass.simpleName).lineSequence().first().take(200)

    private fun dispatch(req: Request): Response {
        val p = req.path
        val get = req.method == "GET" || req.method == "HEAD"
        if (get && (p == "/" || p == "/index.html")) return static("index.html")
        if (get && p.startsWith("/static/")) return static(p.removePrefix("/static/"))
        if (get && p == "/api/info") return Response.json(info().put("features", featuresJson()))
        if (p.startsWith("/api/op/")) {
            if (req.method != "POST") return Response.error(405, "use POST for ops")
            return op(p.removePrefix("/api/op/"), req)
        }
        val r = routes["${req.method} $p"] ?: (if (req.method == "HEAD") routes["GET $p"] else null)
        if (r != null) {
            refusal(r.feature)?.let { throw it }
            return try { r.fn(ctx, req) } catch (e: SecurityException) {
                Response.error(409, "${r.feature.title}: Android did not allow it (${short(e)}) - check its permissions in the app")
            }
        }
        if (routes.keys.any { it.substringAfter(' ') == p }) return Response.error(405, "method not allowed")
        if (get && p == "/favicon.ico") return static("favicon.ico")
        return Response.error(404, "not found: ${p.take(200)}")
    }

    private fun static(name: String): Response {
        if (name.isEmpty() || name.startsWith("/") || name.contains('\\') || name.split('/').any { it == ".." || it == "." || it.isEmpty() }) {
            return Response.error(404, "not found")
        }
        // /static/<file> = the asset phone/static/<file> (or phone/<file>); / = phone/index.html
        val input = (if (name == "index.html" || name == "favicon.ico") null else assets.open("static/$name"))
            ?: assets.open(name) ?: return Response.error(404, "not found: /static/${name.take(200)}")
        val data = input.use { it.readBytes() }
        val type = contentType(name)
        val extra = mutableListOf("Cache-Control" to "no-cache", "X-Content-Type-Options" to "nosniff")
        if (type.startsWith("text/html")) {
            extra.add("Content-Security-Policy" to CSP)
            extra.add("Referrer-Policy" to "no-referrer")
        }
        return Response.bytes(data, type, 200, extra)
    }

    private fun op(name: String, req: Request): Response {
        val args = try { parseArgs(req) } catch (e: OpError) { return Response.error(e.status, e.message ?: "bad request") }
        if (name == "features.list") return ok(featuresJson())
        val o = ops[name] ?: return Response.error(404, "unknown op ${name.take(80)}")
        refusal(o.feature)?.let { return Response.error(it.status, it.message ?: "refused") }
        return try {
            when (val r = o.fn(ctx, args)) {
                is Response -> r
                is ByteArray -> Response.bytes(r, "application/octet-stream")
                else -> ok(r)
            }
        } catch (e: OpError) {
            Response.error(e.status, e.message ?: "refused")
        } catch (e: SecurityException) {
            Response.error(409, "${o.feature.title}: Android did not allow it (${short(e)}) - check its permissions in the app")
        } catch (e: Exception) {
            log(LogLevel.WARN, "op $name failed: ${e.javaClass.simpleName}")
            Response.error(500, "$name failed: ${short(e)}")
        }
    }

    private fun parseArgs(req: Request): JSONObject {
        val body = req.body ?: return JSONObject()
        val data = readAll(body, MAX_OP_BODY) ?: throw OpError("the request is too big", 413)
        val text = String(data, Charsets.UTF_8).trim()
        if (text.isEmpty()) return JSONObject()
        return try { JSONObject(text) } catch (e: Exception) { throw OpError("the body must be a JSON object", 400) }
    }

    // ------------------------------------------------------------------------ WebSockets
    override fun openSocket(session: WsSession): WsHandler {
        if (session.path == "/ws") return bus.attach(session)
        val s = sockets[session.path] ?: throw OpError("nothing at ${session.path.take(200)}", 404)
        refusal(s.feature)?.let { throw it }
        return s.fn(ctx, session)
    }
}
