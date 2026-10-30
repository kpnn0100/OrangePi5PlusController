package com.arstro.arstro_remote_client.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.net.SocketTimeoutException
import java.net.URLDecoder
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.random.Random

/*
 * The device side of the device manager tunnel (docs/relay/PROTOCOL.md sections 2 and 4), a port of
 * server/arstro_remote/relay/agent.py - except that streams do not go to 127.0.0.1:<port>: every
 * http / ws stream is handed to the in-process RequestHandler (the Router). Nothing listens on a port.
 *
 *   device manager <──<url>/api/agent (control: hello, ping, open, refuse)── RelayAgent
 *                  <──<url>/api/agent/data (one per viewer)────────────────── Channel
 *                                                                               │ Stream per request
 *                                                                               ▼
 *                                                                     RequestHandler (Router)
 *
 * Threads per channel: a reader (never blocks: it only queues), a writer (the only thread that seals
 * and sends, under the channel's send lock - so the nonce order is the wire order), and one thread per
 * stream. Flow control (WINDOW per stream and direction) blocks only a stream's own sender.
 *
 * Pure JVM: logs through `log`, never the access key, the join token or the device secret.
 */

/** What the agent hands its streams to (the Router). */
interface RequestHandler {
    /** One HTTP request -> the answer (never throws for a bad request: answers 4xx / 5xx). */
    fun handle(req: Request): Response
    /** A viewer opened a WebSocket -> its handler. Throw OpError to refuse: the socket opens and closes
     *  at once with code 4000 + status (4403 off, 4409 not ready, 4404 unknown path) and the message. */
    fun openSocket(session: WsSession): WsHandler
}

/** Settings of the agent (Store + the service fill it in). */
class AgentConfig(
    val url: String,
    val token: String,
    val id: String,
    val name: String,
    val secret: String,
    val keyRaw: ByteArray,
    val version: String,
    val hostname: String,
    val platform: String,
    val slot: String = "phone",
    val web: Int = 8080,
    val ports: List<Int>? = listOf(8080),
    val enabled: Boolean = true,
)

/** One connected viewer (for the app and the notification). */
class ViewerInfo(val n: Int, val addr: String, val since: Long, val streams: Int, val bytesIn: Long, val bytesOut: Long) {
    fun toMap(): Map<String, Any> = mapOf("n" to n, "addr" to addr, "since" to since, "streams" to streams,
                                          "bytes_in" to bytesIn, "bytes_out" to bytesOut)
}

object LogLevel {
    const val DEBUG = 3
    const val INFO = 4
    const val WARN = 5
    const val ERROR = 6
}

private class TunnelError(message: String) : Exception(message)
private class StreamError(message: String) : Exception(message)
private class Closed : Exception("closed")

private val HOP = setOf("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
                        "te", "trailer", "transfer-encoding", "upgrade")
private val PATH_RE = Regex("^/[^\\s\\x00-\\x1f\\x7f]*$")
private val METHOD_RE = Regex("^[A-Za-z][A-Za-z0-9_-]{0,31}$")

private fun reasonPhrase(status: Int) = when (status) {
    200 -> "OK"; 201 -> "Created"; 204 -> "No Content"; 206 -> "Partial Content"
    301 -> "Moved Permanently"; 302 -> "Found"; 304 -> "Not Modified"
    400 -> "Bad Request"; 401 -> "Unauthorized"; 403 -> "Forbidden"; 404 -> "Not Found"; 405 -> "Method Not Allowed"
    409 -> "Conflict"; 411 -> "Length Required"; 413 -> "Payload Too Large"; 416 -> "Range Not Satisfiable"
    500 -> "Internal Server Error"; 501 -> "Not Implemented"; 503 -> "Service Unavailable"
    else -> ""
}

/** "/a/b?x=1&y=%20" -> ("/a/b" decoded, {x: [1], y: [" "]}). */
internal fun splitTarget(target: String): Pair<String, Map<String, List<String>>> {
    val q = target.indexOf('?')
    val rawPath = if (q < 0) target else target.substring(0, q)
    val path = try { URLDecoder.decode(rawPath.replace("+", "%2B"), "UTF-8") } catch (e: Exception) { rawPath }
    val query = LinkedHashMap<String, MutableList<String>>()
    if (q >= 0) {
        for (part in target.substring(q + 1).split('&')) {
            if (part.isEmpty()) continue
            val eq = part.indexOf('=')
            val k = if (eq < 0) part else part.substring(0, eq)
            val v = if (eq < 0) "" else part.substring(eq + 1)
            fun dec(s: String) = try { URLDecoder.decode(s, "UTF-8") } catch (e: Exception) { s }
            query.getOrPut(dec(k)) { ArrayList() }.add(dec(v))
        }
    }
    return path to query
}

class RelayAgent(
    private val handler: RequestHandler,
    private val onChange: () -> Unit = {},
    private val log: (Int, String) -> Unit = { _, _ -> },
) {
    companion object {
        const val HEARTBEAT = 20.0              // s, unless the relay's welcome says otherwise
        const val BACKOFF_MIN = 1.0
        const val BACKOFF_MAX = 60.0
        const val CONTROL_TIMEOUT = 10_000L     // ms: connect, welcome, linked
        const val MAX_CHANNELS = 16
        const val MAX_STREAMS = 128
        const val HANDSHAKE_MS = 15_000L
        const val WRONG_MAX = 5                 // more wrong proofs than this within WRONG_WINDOW ...
        const val WRONG_WINDOW = 60_000L
        const val LOCKOUT_MS = 60_000L          // ... refuse new channels this long
        const val QUEUE_MAX = 2L * E2e.WINDOW   // DATA bytes waiting for one channel's writer
        const val TOO_MANY_WRONG = "too many wrong keys - wait a minute"
        const val WS_OUT_MAX = 8L shl 20        // a WebSocket's unsent bytes before its sender waits
    }

    private val lock = Object()
    @Volatile var cfg: AgentConfig? = null
        private set
    /** "offline" | "connecting" | "online" | "error" */
    @Volatile var state = "offline"
        private set
    @Volatile var error: String? = null
        private set
    @Volatile var since = System.currentTimeMillis()
        private set
    /** "ws" | "http" while the control connection is up. */
    @Volatile var transport: String? = null
        private set
    @Volatile var relayVersion: String? = null
        private set
    @Volatile var wrongTotal = 0
        private set
    private val channels = LinkedHashMap<Int, Channel>()
    private val wrong = ArrayDeque<Long>()
    private var lockedUntil = 0L
    private var refusedLocked = 0
    private val numbers = AtomicInteger(1)
    private var gen = 0
    private var conn: RelayConn? = null
    private var thread: Thread? = null
    @Volatile private var stopped = false
    private val wake = Object()
    private var woken = false

    private fun mono() = System.nanoTime() / 1_000_000

    // ------------------------------------------------------------------------ settings
    /** New settings: the control connection is made again (with the new ones). closeChannels: also
     *  drop the viewers (the access key changed). */
    fun configure(c: AgentConfig?, closeChannels: Boolean = false) {
        val old: RelayConn?
        synchronized(lock) {
            cfg = c
            gen++
            old = conn
        }
        if (closeChannels) closeChannels("the access key changed")
        old?.close()
        signal()
    }

    /** The phone's network changed (Wi-Fi <-> mobile data, or back after none): connect again now
     *  instead of waiting for the old connection to time out and then for the back-off. */
    fun networkChanged() {
        val old: RelayConn?
        synchronized(lock) {
            gen++
            old = conn
        }
        old?.close()
        signal()
    }

    fun allow(): Set<Int>? = cfg?.ports?.toSet()

    internal fun readyMessage(): JSONObject {
        val c = cfg
        return JSONObject().put("t", "ready").put("v", E2e.VERSION).put("id", c?.id).put("name", c?.name)
            .put("hostname", c?.hostname).put("version", c?.version).put("slot", c?.slot)
            .put("ports", c?.ports?.let { JSONArray(it) } ?: JSONObject.NULL)
    }

    // ------------------------------------------------------------------------ state
    private fun setState(s: String, err: String? = null) {
        synchronized(lock) {
            if (s != "online") transport = null
            if (s == state && err == error) return
            state = s; error = err; since = System.currentTimeMillis()
        }
        changed()
    }

    internal fun changed() {
        try { onChange() } catch (e: Exception) { log(LogLevel.DEBUG, "relay onChange failed: ${e.javaClass.simpleName}") }
    }

    fun viewers(): List<ViewerInfo> {
        val chans = synchronized(lock) { channels.values.filter { it.ready && !it.closed } }
        return chans.sortedBy { it.n }.map { it.describe() }
    }

    // ------------------------------------------------------------------------ lifecycle
    fun start() {
        synchronized(lock) {
            if (thread != null) return
            stopped = false
            thread = Thread(::run, "relay-agent").apply { isDaemon = true; start() }
        }
    }

    fun stop() {
        stopped = true
        signal()
        val c = synchronized(lock) { conn }
        c?.close()
        closeChannels("the phone stopped sharing")
        thread?.join(5000)
        synchronized(lock) { thread = null }
    }

    fun closeChannels(reason: String): Int {
        val chans = synchronized(lock) { channels.values.toList() }
        chans.forEach { it.close(reason) }
        return chans.count { it.ready }
    }

    internal fun channelClosed(ch: Channel) {
        synchronized(lock) { channels.remove(ch.n) }
        changed()
    }

    private fun signal() = synchronized(wake) { woken = true; wake.notifyAll() }

    private fun waitWake(ms: Long) = synchronized(wake) {
        val end = mono() + ms
        while (!woken && !stopped) {
            val left = if (ms <= 0) 0 else end - mono()
            if (ms > 0 && left <= 0) break
            wake.wait(if (ms <= 0) 0 else left)
        }
        woken = false
    }

    // ------------------------------------------------------------------------ wrong keys
    internal fun wrongKey(addr: String, how: String) {
        val now = mono()
        val recent: Int
        val lockNow: Boolean
        synchronized(lock) {
            wrongTotal++
            wrong.addLast(now)
            while (wrong.isNotEmpty() && wrong.first() < now - WRONG_WINDOW) wrong.removeFirst()
            recent = wrong.size
            lockNow = recent > WRONG_MAX && lockedUntil <= now
            if (lockNow) { lockedUntil = now + LOCKOUT_MS; refusedLocked = 0 }
        }
        if (lockNow) log(LogLevel.WARN, "$recent wrong access keys in ${WRONG_WINDOW / 1000} s (last from $addr): refusing new viewers for ${LOCKOUT_MS / 1000} s")
        else log(LogLevel.WARN, "viewer from $addr: wrong access key - $how ($recent in the last minute)")
        changed()
    }

    private fun locked(): Boolean = synchronized(lock) {
        if (lockedUntil > mono()) { refusedLocked++; return true }
        if (refusedLocked > 0) {
            log(LogLevel.WARN, "wrong-key lockout over: $refusedLocked viewer(s) were refused during it")
            refusedLocked = 0
        }
        false
    }

    // ------------------------------------------------------------------------ the loop
    private fun run() {
        var backoff = BACKOFF_MIN
        while (!stopped) {
            val c: AgentConfig?
            val g: Int
            synchronized(lock) { c = cfg; g = gen }
            val why: String? = when {
                c == null || !c.enabled -> "off"
                c.url.isBlank() || c.token.isBlank() -> "set the device manager URL and its join token"
                else -> null
            }
            if (why != null) {
                relayVersion = null
                setState("offline", if (why == "off") null else why)
                waitWake(0)
                backoff = BACKOFF_MIN
                continue
            }
            setState("connecting", if (state == "error") error else null)
            val online = AtomicBoolean(false)
            val err: String = try {
                session(c!!, g, online)
                "the device manager closed the connection"
            } catch (e: TunnelError) {
                e.message ?: "error"
            } catch (e: IOException) {
                describe(e)
            } catch (e: IllegalArgumentException) {
                e.message ?: "bad settings"
            } catch (e: InterruptedException) {
                break
            }
            if (stopped) break
            if (synchronized(lock) { g != gen }) { backoff = BACKOFF_MIN; continue }
            if (online.get()) backoff = BACKOFF_MIN
            relayVersion = null
            setState("error", err)
            val delay = backoff * Random.nextDouble(0.8, 1.2)
            backoff = minOf(backoff * 2, BACKOFF_MAX)
            log(LogLevel.DEBUG, "relay: retrying in %.1f s (%s)".format(delay, err))
            waitWake((delay * 1000).toLong())
        }
        setState("offline", null)
    }

    private fun recvJson(c: RelayConn, timeoutMs: Long): JSONObject {
        val m = c.recv(timeoutMs)
        if (!m.text) throw TunnelError("unexpected binary message from the device manager")
        return try { JSONObject(m.string()) } catch (e: Exception) { throw TunnelError("bad message from the device manager") }
    }

    private fun session(c: AgentConfig, g: Int, online: AtomicBoolean) {
        val url = Transports.wsBase(c.url)
        val cn = Transports.connect("$url/api/agent", timeoutMs = CONTROL_TIMEOUT)
        synchronized(lock) {
            if (g != gen || stopped) { cn.close(); return }
            conn = cn
        }
        try {
            cn.sendText(JSONObject().put("t", "hello").put("v", E2e.VERSION).put("id", c.id).put("name", c.name)
                .put("secret", c.secret).put("token", c.token).put("version", c.version).put("hostname", c.hostname)
                .put("platform", c.platform).put("slot", c.slot).put("web", c.web).toString())
            val welcome = try { recvJson(cn, CONTROL_TIMEOUT) } catch (e: SocketTimeoutException) {
                throw TunnelError("the device manager did not answer")
            }
            if (welcome.opt("t") == "error") throw TunnelError("the device manager refused this device: ${welcome.optString("error", "no reason")}")
            if (welcome.opt("t") != "welcome") throw TunnelError("unexpected answer from the device manager: ${welcome.opt("t")}")
            val heartbeat = welcome.optDouble("heartbeat", HEARTBEAT).let { if (it.isNaN() || it <= 0) HEARTBEAT else it }.coerceIn(0.2, 300.0)
            relayVersion = welcome.opt("relay")?.takeIf { it != JSONObject.NULL }?.toString()
            online.set(true)
            log(LogLevel.INFO, "tunnel up: as ${c.id}, device manager ${relayVersion ?: "?"}${if (cn.transport == "http") " over HTTP" else ""}")
            transport = cn.transport
            setState("online", null)
            Thread({ pinger(cn, heartbeat) }, "relay-ping").apply { isDaemon = true; start() }
            val limit = (3 * heartbeat * 1000).toLong()
            while (true) {
                val msg = try { recvJson(cn, limit) } catch (e: SocketTimeoutException) {
                    throw TunnelError("no answer from the device manager for %.0f s".format(3 * heartbeat))
                }
                when (msg.opt("t")) {
                    "open" -> open(cn, c, msg)
                    "bye" -> throw TunnelError("the device manager closed the connection: ${msg.optString("reason", "bye")}")
                    "error" -> throw TunnelError("the device manager: ${msg.optString("error", "error")}")
                }
            }
        } catch (e: IOException) {
            if (synchronized(lock) { g != gen || stopped }) return
            if (online.get()) throw TunnelError("connection lost (${describe(e)})")
            throw e
        } finally {
            synchronized(lock) { if (conn === cn) conn = null }
            transport = null
            cn.close()
            if (online.get()) log(LogLevel.INFO, "tunnel down")
        }
    }

    /** A ping every heartbeat. Measured on the wall clock in short steps: a phone's CPU sleeps, and
     *  after it wakes (e.g. for the pipe's next answer) the ping must go out at once. */
    private fun pinger(cn: RelayConn, heartbeat: Double) {
        val every = (heartbeat * 1000).toLong()
        var last = System.currentTimeMillis()
        while (!cn.isClosed && !stopped) {
            try { Thread.sleep(minOf(every, 1000L)) } catch (e: InterruptedException) { return }
            val now = System.currentTimeMillis()
            if (now - last < every && now >= last) continue
            last = now
            if (cn.isClosed) return
            try { cn.sendText("{\"t\":\"ping\"}") } catch (e: IOException) { return }
        }
    }

    private fun open(cn: RelayConn, c: AgentConfig, msg: JSONObject) {
        val sid = msg.opt("sid") as? String ?: return
        if (sid.isEmpty()) return
        val addr = msg.optString("addr", "?").take(64)
        var err: String? = null
        var ch: Channel? = null
        if (locked()) err = TOO_MANY_WRONG
        else synchronized(lock) {
            if (channels.size >= MAX_CHANNELS) err = "too many viewers"
            else {
                ch = Channel(numbers.getAndIncrement(), sid, addr)
                channels[ch!!.n] = ch!!
            }
        }
        if (err != null) {
            log(LogLevel.INFO, "viewer from $addr refused: $err")
            try { cn.sendText(JSONObject().put("t", "refuse").put("sid", sid).put("error", err).toString()) } catch (_: IOException) {}
            return
        }
        val channel = ch!!
        Thread({ channel.run(c) }, "relay-ch${channel.n}").apply { isDaemon = true; start() }
    }

    // ========================================================================= a channel
    inner class Channel(val n: Int, private val sid: String, val addr: String) {
        @Volatile var ready = false
        @Volatile var closed = false
        @Volatile var reason: String? = null
        private val since = System.currentTimeMillis()
        @Volatile var bytesIn = 0L
        @Volatile var bytesOut = 0L
        private var conn: RelayConn? = null
        private var secure: Secure? = null
        private val streams = HashMap<Long, Stream>()
        private var maxSid = 0L
        private val chLock = Object()            // streams / closed
        private val sendLock = Object()          // seal + send: nonce order = wire order
        private val qLock = Object()
        private val q = ArrayDeque<Array<Any>>() // (kind, sid, payload, flags)
        private var qBytes = 0L

        fun describe(): ViewerInfo {
            val s = synchronized(chLock) { streams.size }
            return ViewerInfo(n, addr, since / 1000, s, bytesIn, bytesOut)
        }

        // ------------------------------------------------------------------ sending
        /** Queue a frame for the writer. control = true never waits (the reader uses it). */
        fun send(kind: Int, sid: Long, payload: ByteArray, flags: Int = 0, control: Boolean = false) {
            synchronized(qLock) {
                if (!control) while (qBytes > QUEUE_MAX && !closed) qLock.wait(1000)
                if (closed) throw Closed()
                q.addLast(arrayOf(kind, sid, payload, flags))
                qBytes += payload.size
                qLock.notifyAll()
            }
        }

        fun sendJson(sid: Long, obj: JSONObject, control: Boolean = false) =
            send(E2e.K_JSON, sid, obj.toString().toByteArray(), 0, control)

        private fun writer() {
            try {
                while (true) {
                    val item: Array<Any>
                    synchronized(qLock) {
                        while (q.isEmpty() && !closed) qLock.wait()
                        if (closed) return
                        item = q.removeFirst()
                    }
                    val payload = item[2] as ByteArray
                    val ct: ByteArray
                    synchronized(sendLock) {
                        ct = secure!!.seal(item[0] as Int, item[1] as Long, payload, item[3] as Int)
                        conn!!.sendBinary(ct)
                    }
                    bytesOut += ct.size
                    synchronized(qLock) {
                        qBytes -= payload.size
                        qLock.notifyAll()
                    }
                }
            } catch (e: IOException) {
                close("connection lost (${describe(e)})")
            } catch (e: InterruptedException) {
                close("stopped")
            }
        }

        // ------------------------------------------------------------------ life
        fun run(c: AgentConfig) {
            try {
                link(c)
                handshake()
            } catch (e: WrongKey) {
                close("wrong key", quiet = true); return
            } catch (e: Closed) {
                close("closed", quiet = true); return
            } catch (e: Exception) {
                if (!closed) log(LogLevel.INFO, "viewer $n from $addr: no channel (${if (e is IOException) describe(e) else e.message})")
                close(e.message ?: "error", quiet = true); return
            }
            ready = true
            log(LogLevel.INFO, "viewer $n connected from $addr")
            changed()
            Thread(::writer, "relay-ch$n-tx").apply { isDaemon = true; start() }
            var why = "closed by the viewer"
            try {
                sendJson(0, readyMessage(), control = true)
                readLoop()
            } catch (e: Closed) {
                why = reason ?: "closed"
            } catch (e: ConnClosed) {
                why = "closed by the viewer"
            } catch (e: IOException) {
                why = "connection lost (${describe(e)})"
            } catch (e: ProtocolError) {
                why = "protocol error: ${e.message}"
            } catch (e: InterruptedException) {
                why = "stopped"
            }
            close(why)
        }

        private fun link(c: AgentConfig) {
            val cn = Transports.connect(Transports.wsBase(c.url) + "/api/agent/data", timeoutMs = CONTROL_TIMEOUT)
            synchronized(chLock) {
                if (closed) { cn.close(); throw Closed() }
                conn = cn
            }
            cn.sendText(JSONObject().put("t", "data").put("v", E2e.VERSION).put("id", c.id).put("secret", c.secret)
                .put("sid", sid).toString())
            val msg = try { recvJson(cn, CONTROL_TIMEOUT) } catch (e: SocketTimeoutException) {
                throw TunnelError("the device manager did not link the data connection")
            }
            if (msg.opt("t") == "error") throw TunnelError("the device manager refused the data connection: ${msg.optString("error")}")
            if (msg.opt("t") != "linked") throw TunnelError("expected linked, got ${msg.opt("t")}")
        }

        private fun text(deadline: Long): JSONObject {
            val left = deadline - mono()
            if (left <= 0) throw ProtocolError("the viewer did not finish the handshake in ${HANDSHAKE_MS / 1000} s")
            val m = try { conn!!.recv(left) } catch (e: SocketTimeoutException) {
                throw ProtocolError("the viewer did not finish the handshake in ${HANDSHAKE_MS / 1000} s")
            }
            bytesIn += m.data.size
            if (!m.text) throw ProtocolError("a handshake message must be text")
            return E2e.parseText(m.string())
        }

        private fun say(obj: JSONObject) {
            val t = obj.toString()
            synchronized(sendLock) { conn!!.sendText(t) }
            bytesOut += t.length
        }

        private fun handshake() {
            val deadline = mono() + HANDSHAKE_MS
            val hello = text(deadline)
            // the key of NOW (not of when the control connection started): a replaced key counts at once
            val key = cfg?.keyRaw ?: throw Closed()
            val (st, answer) = E2e.deviceAnswer(key, hello)
            say(answer)
            val proof = try { text(deadline) } catch (e: IOException) {
                if (closed) throw Closed()
                // A viewer checks this device's proof first and, with another key, just goes away
                // (it sends nothing derived from its key): that is how a wrong key looks from here.
                wrongKey(addr, "the viewer did not accept this device's proof")
                throw WrongKey("the viewer has a different access key")
            }
            try {
                secure = E2e.deviceCheck(st, proof)
            } catch (e: WrongKey) {
                try { say(JSONObject().put("t", "error").put("error", "wrong key")) } catch (_: IOException) {}
                wrongKey(addr, "wrong proof")
                throw e
            }
        }

        private fun readLoop() {
            val sec = secure!!
            while (true) {
                val m = conn!!.recv()
                bytesIn += m.data.size
                if (m.text) throw ProtocolError("a text message after the handshake")
                val f = sec.open(m.data)
                if (f.stream == 0L) {
                    if (f.kind == E2e.K_JSON && E2e.decodeJson(f.payload).opt("t") == "ping") {
                        send(E2e.K_JSON, 0, "{\"t\":\"pong\"}".toByteArray(), control = true)
                    }
                    continue
                }
                val stream = synchronized(chLock) { streams[f.stream] }
                if (stream == null) {
                    if (f.kind == E2e.K_JSON && f.stream > maxSid) openStream(f.stream, E2e.decodeJson(f.payload))
                    continue                       // late frames of a finished stream
                }
                if (f.kind == E2e.K_DATA) {
                    stream.push(Item.Data(f.flags, f.payload))
                    continue
                }
                val msg = E2e.decodeJson(f.payload)
                when (msg.optString("t")) {
                    "ack" -> stream.acked(msg.optLong("n", 0))
                    "end" -> stream.push(Item.End)
                    "close" -> if (stream.kind == "ws") {
                        val code = msg.opt("code")
                        stream.push(Item.Close(if (code is Int) code else 1000, msg.optString("reason", "")))
                    } else stream.abort()
                    "cancel" -> stream.abort()
                }
            }
        }

        private fun error(sid: Long, text: String) = sendJson(sid, JSONObject().put("t", "error").put("error", text), control = true)

        private fun openStream(sid: Long, msg: JSONObject) {
            maxSid = sid
            val t = msg.optString("t")
            if (t !in setOf("http", "ws", "tcp", "ports")) return error(sid, "unknown stream type $t")
            if (t == "tcp") return error(sid, "tcp is not available on a phone")
            if (t != "ports") {
                val port = msg.opt("port")
                if (port !is Int || port !in 1..65535) return error(sid, "port must be 1-65535")
                val allow = allow()
                if (allow != null && port !in allow) return error(sid, "port $port is not allowed on this device")
            }
            val s: Stream
            synchronized(chLock) {
                if (closed) return
                if (streams.size >= MAX_STREAMS) return error(sid, "too many streams")
                s = Stream(this, sid, msg)
                streams[sid] = s
            }
            Thread(s::run, "relay-ch$n-s$sid").apply { isDaemon = true; start() }
        }

        fun forget(s: Stream) {
            synchronized(chLock) { if (streams[s.sid] === s) streams.remove(s.sid) }
        }

        fun close(why: String = "closed", quiet: Boolean = false) {
            val all: List<Stream>
            val cn: RelayConn?
            synchronized(chLock) {
                if (closed) return
                closed = true
                reason = why
                all = streams.values.toList()
                streams.clear()
                cn = conn
            }
            synchronized(qLock) { qLock.notifyAll() }
            all.forEach { it.abort() }
            cn?.close()
            if (ready && !quiet) {
                log(LogLevel.INFO, "viewer $n ($addr) closed after ${(System.currentTimeMillis() - since) / 1000}s: " +
                                   "$bytesIn bytes in, $bytesOut bytes out ($why)")
            }
            channelClosed(this)
        }
    }

    // ========================================================================= a stream
    sealed class Item {
        class Data(val flags: Int, val data: ByteArray) : Item()
        object End : Item()
        class Close(val code: Int, val reason: String) : Item()
    }

    /** One stream of a channel. The channel's reader only queues into it (push, acked); the stream's
     *  own thread runs the request / the WebSocket and waits for flow control. */
    inner class Stream(val ch: Channel, val sid: Long, private val opener: JSONObject) {
        val kind: String = opener.optString("t")
        private val cv = Object()
        private val inq = ArrayDeque<Item>()
        private var queued = 0L                  // V->D bytes waiting in inq
        private var inflight = 0L                // D->V bytes not acknowledged yet
        private var unacked = 0L                 // V->D bytes consumed, not acknowledged yet
        @Volatile var done = false

        // ------------------------------------------------------ reader side (never blocks)
        fun push(item: Item) {
            var overflow = false
            synchronized(cv) {
                if (done) return
                if (item is Item.Data) {
                    queued += item.data.size
                    if (queued > E2e.WINDOW + E2e.CHUNK) { done = true; overflow = true } else inq.addLast(item)
                } else inq.addLast(item)
                cv.notifyAll()
            }
            if (overflow) fail("the viewer sent more than the flow-control window")
        }

        fun acked(n: Long) = synchronized(cv) {
            inflight = maxOf(0, inflight - maxOf(0, n))
            cv.notifyAll()
        }

        val inflightBytes: Long get() = synchronized(cv) { inflight }

        /** Cancel / abort: wakes the stream's threads, the stream ends. */
        fun abort() {
            synchronized(cv) { done = true; cv.notifyAll() }
            ch.forget(this)
        }

        // ------------------------------------------------------ stream threads
        /** Next item from the viewer (blocks); null when the stream is done. */
        fun pull(): Item? = synchronized(cv) {
            while (inq.isEmpty() && !done) cv.wait()
            val item = inq.removeFirstOrNull() ?: return null
            if (item is Item.Data) queued -= item.data.size
            item
        }

        /** n bytes from the viewer were taken: acknowledge every WINDOW / 4. */
        fun consumed(n: Int) {
            val ack: Long
            synchronized(cv) {
                unacked += n
                if (unacked < E2e.WINDOW / 4) return
                ack = unacked
                unacked = 0
            }
            try { sendJson(JSONObject().put("t", "ack").put("n", ack), control = true) } catch (_: Closed) {}
        }

        fun sendJson(obj: JSONObject, control: Boolean = false) = ch.sendJson(sid, obj, control)

        /** DATA to the viewer in CHUNK pieces; waits while WINDOW bytes are unacknowledged. */
        fun sendData(data: ByteArray, flags: Int = 0) {
            var i = 0
            val n = data.size
            do {
                val end = minOf(n, i + E2e.CHUNK)
                val piece = if (i == 0 && end == n) data else data.copyOfRange(i, end)
                synchronized(cv) {
                    while (inflight >= E2e.WINDOW && !done && !ch.closed) cv.wait(1000)
                    if (done || ch.closed) throw Closed()
                    inflight += piece.size
                }
                val last = end >= n
                ch.send(E2e.K_DATA, sid, piece, if (last) flags else flags and E2e.F_FIN.inv())
                i = end
            } while (i < n)
        }

        fun fail(error: String) {
            try { sendJson(JSONObject().put("t", "error").put("error", error), control = true) } catch (_: Closed) {}
            abort()
        }

        fun finish() {
            synchronized(cv) { done = true; cv.notifyAll() }
            ch.forget(this)
        }

        fun run() {
            try {
                when (kind) {
                    "ports" -> sendJson(JSONObject().put("t", "ports").put("ports", JSONArray().put(
                        JSONObject().put("port", cfg?.web ?: 8080).put("addr", "in-app").put("proc", "Arstro"))))
                    "http" -> runHttp()
                    "ws" -> runWs()
                }
            } catch (e: StreamError) {
                if (!done) fail(e.message ?: "error")
            } catch (e: Closed) {
            } catch (e: InterruptedException) {
            } catch (e: Exception) {                  // never kill the channel for one stream
                log(LogLevel.WARN, "relay stream $kind failed: ${e.javaClass.simpleName}")
                if (!done) fail("internal error: ${e.javaClass.simpleName}${e.message?.let { ": " + it.take(200) } ?: ""}")
            } finally {
                finish()
            }
        }

        // ------------------------------------------------------ http
        private fun runHttp() {
            val method = opener.optString("method", "GET").ifEmpty { "GET" }
            val target = opener.optString("path", "/").ifEmpty { "/" }
            if (!METHOD_RE.matches(method)) throw StreamError("bad HTTP method")
            if (!PATH_RE.matches(target)) throw StreamError("bad path (it must start with /)")
            val headers = requestHeaders(opener.optJSONArray("headers"))
            val hasBody = opener.optBoolean("body", false)
            val length = headers["content-length"]?.toLongOrNull() ?: -1L
            val (path, query) = splitTarget(target)
            val body = if (hasBody) BodyStream() else null
            val req = Request(method.uppercase(), path, query, headers, body, if (hasBody) length else 0L)
            val res = try { handler.handle(req) } catch (e: Exception) {
                Response.error(500, "internal error: ${e.javaClass.simpleName}")
            }
            if (done || ch.closed) { try { res.body?.close() } catch (_: IOException) {}; throw Closed() }
            val out = JSONArray()
            val seen = HashSet<String>()
            for ((k, v) in res.headers) {
                val lk = k.lowercase()
                if (lk in HOP || lk.startsWith("proxy-")) continue
                seen.add(lk)
                out.put(JSONArray().put(k).put(v))
            }
            if (res.length >= 0 && "content-length" !in seen) out.put(JSONArray().put("Content-Length").put(res.length.toString()))
            sendJson(JSONObject().put("t", "res").put("status", res.status).put("reason", reasonPhrase(res.status)).put("headers", out))
            res.body?.use { input ->
                val buf = ByteArray(E2e.CHUNK)
                while (true) {
                    val r = try { input.read(buf) } catch (e: IOException) {
                        throw StreamError("the answer broke off: ${e.message ?: e.javaClass.simpleName}")
                    }
                    if (r < 0) break
                    if (r > 0) sendData(buf.copyOf(r))
                }
            }
            sendJson(JSONObject().put("t", "end"))
            // a body the handler did not read: drop the rest (the stream ends)
        }

        private fun requestHeaders(list: JSONArray?): Map<String, String> {
            val out = LinkedHashMap<String, String>()
            if (list == null) return out
            val listed = HashSet<String>()
            val items = ArrayList<Pair<String, String>>()
            for (i in 0 until list.length()) {
                val kv = list.optJSONArray(i) ?: throw StreamError("headers must be a list of [name, value]")
                if (kv.length() != 2) throw StreamError("headers must be a list of [name, value]")
                val k = kv.optString(0).trim()
                val v = kv.optString(1).trim()
                if (k.isEmpty() || v.any { it == '\r' || it == '\n' || it == '\u0000' }) throw StreamError("bad header ${k.take(40)}")
                items.add(k to v)
                if (k.equals("connection", true)) v.split(',').map { it.trim().lowercase() }.filter { it.isNotEmpty() }.forEach { listed.add(it) }
            }
            for ((k, v) in items) {
                val lk = k.lowercase()
                if (lk in HOP || lk in listed || lk.startsWith("proxy-") || lk == "x-forwarded-for") continue
                out[lk] = out[lk]?.let { "$it, $v" } ?: v
            }
            out["x-forwarded-for"] = ch.addr
            return out
        }

        /** The request body: DATA frames of the stream until `end`. */
        private inner class BodyStream : InputStream() {
            private var cur: ByteArray? = null
            private var pos = 0
            private var ended = false

            private fun fill(): Boolean {
                while (true) {
                    val c = cur
                    if (c != null && pos < c.size) return true
                    if (ended) return false
                    when (val item = pull()) {
                        null -> throw IOException("the viewer went away")
                        is Item.End -> { ended = true; return false }
                        is Item.Data -> { cur = item.data; pos = 0; consumed(item.data.size) }
                        is Item.Close -> throw IOException("the viewer cancelled the request")
                    }
                }
            }

            override fun read(): Int {
                if (!fill()) return -1
                return cur!![pos++].toInt() and 0xff
            }

            override fun read(b: ByteArray, off: Int, len: Int): Int {
                if (len == 0) return 0
                if (!fill()) return -1
                val c = cur!!
                val n = minOf(len, c.size - pos)
                System.arraycopy(c, pos, b, off, n)
                pos += n
                return n
            }

            override fun available(): Int = cur?.let { it.size - pos } ?: 0
        }

        // ------------------------------------------------------ ws
        private fun runWs() {
            val target = opener.optString("path", "/").ifEmpty { "/" }
            if (!PATH_RE.matches(target)) throw StreamError("bad path (it must start with /)")
            val (path, query) = splitTarget(target)
            val s = StreamSession(this, path, query)
            sendJson(JSONObject().put("t", "open").put("protocol", ""))
            val h: WsHandler = try { handler.openSocket(s) } catch (e: OpError) {
                s.close(4000 + e.status, e.message ?: "refused")
                s.drain()
                return
            } catch (e: Exception) {
                s.close(4500, "internal error: ${e.javaClass.simpleName}")
                s.drain()
                return
            }
            s.handler = h
            val parts = java.io.ByteArrayOutputStream()
            var isText = false
            var started = false
            while (true) {
                val item = pull()
                if (item == null) { s.closedBy(1006, "the viewer went away"); return }
                when (item) {
                    is Item.End -> continue
                    is Item.Close -> {
                        s.closedBy(item.code, item.reason)
                        return
                    }
                    is Item.Data -> {
                        if (!started) { isText = item.flags and E2e.F_TEXT != 0; started = true }
                        parts.write(item.data)
                        if (parts.size() > E2e.MAX_MESSAGE * 16) {
                            s.close(1009, "message too big"); s.closedBy(1009, "message too big"); return
                        }
                        if (item.flags and E2e.F_FIN != 0) {
                            val msg = parts.toByteArray()
                            parts.reset()
                            started = false
                            try {
                                if (isText) h.onText(s, String(msg, Charsets.UTF_8)) else h.onBinary(s, msg)
                            } catch (e: Exception) {
                                log(LogLevel.WARN, "socket $path: the handler failed: ${e.javaClass.simpleName}")
                            }
                            consumed(msg.size)
                        }
                    }
                }
            }
        }

        fun closeSent() { /* the session's close went out: the stream is over */ finish() }
    }

    // ========================================================================= a viewer's WebSocket
    /** WsSession over a ws stream. Sends go into a queue that the session's sender thread hands to
     *  the channel (flow control); `queued` = unsent + unacknowledged bytes. */
    inner class StreamSession(private val stream: Stream, override val path: String,
                              override val query: Map<String, List<String>>) : WsSession {
        @Volatile var handler: WsHandler? = null
        private val lock = Object()
        private val out = ArrayDeque<Any>()      // Pair(flags, bytes) | Item.Close
        private var outBytes = 0L
        @Volatile private var closing = false
        private var sender: Thread? = null
        private val ended = AtomicBoolean(false)

        override val isOpen: Boolean get() = !closing && !stream.done && !stream.ch.closed

        override val queued: Long get() = synchronized(lock) { outBytes } + stream.inflightBytes

        override fun sendText(text: String) = enqueue(E2e.F_FIN or E2e.F_TEXT, text.toByteArray())
        override fun sendBinary(data: ByteArray) = enqueue(E2e.F_FIN, data)

        private fun enqueue(flags: Int, data: ByteArray) {
            synchronized(lock) {
                while (outBytes > WS_OUT_MAX && isOpen) lock.wait(1000)
                if (!isOpen) return
                out.addLast(flags to data)
                outBytes += data.size
                startSender()
                lock.notifyAll()
            }
        }

        override fun close(code: Int, reason: String) {
            synchronized(lock) {
                if (closing) return
                closing = true
                out.addLast(Item.Close(code, reason))
                startSender()
                lock.notifyAll()
            }
        }

        private fun startSender() {
            if (sender != null) return
            sender = Thread(::sendLoop, "relay-ws-tx").apply { isDaemon = true; start() }
        }

        private fun sendLoop() {
            try {
                while (true) {
                    val item: Any
                    synchronized(lock) {
                        while (out.isEmpty() && !stream.done && !stream.ch.closed) lock.wait(1000)
                        if (stream.done || stream.ch.closed) return
                        item = out.removeFirst()
                    }
                    if (item is Item.Close) {
                        stream.sendJson(JSONObject().put("t", "close").put("code", item.code).put("reason", item.reason.take(123)), control = true)
                        closedBy(item.code, item.reason)
                        stream.closeSent()
                        return
                    }
                    @Suppress("UNCHECKED_CAST")
                    val pair = item as Pair<Int, ByteArray>
                    val (flags, data) = pair
                    try {
                        stream.sendData(data, flags)
                    } finally {
                        synchronized(lock) { outBytes -= data.size; lock.notifyAll() }
                    }
                }
            } catch (_: Closed) {
            } catch (_: InterruptedException) {
            } finally {
                synchronized(lock) { out.clear(); outBytes = 0; lock.notifyAll() }
            }
        }

        /** Wait (a moment) for a refusal's close to go out. */
        internal fun drain() {
            val t = synchronized(lock) { sender }
            t?.join(2000)
        }

        /** The socket ended (viewer close, close here, channel lost): onClose once. */
        internal fun closedBy(code: Int, reason: String) {
            if (!ended.compareAndSet(false, true)) return
            synchronized(lock) { closing = true; lock.notifyAll() }
            try { handler?.onClose(this, code, reason) } catch (e: Exception) {
                log(LogLevel.WARN, "socket $path: onClose failed: ${e.javaClass.simpleName}")
            }
        }
    }
}
