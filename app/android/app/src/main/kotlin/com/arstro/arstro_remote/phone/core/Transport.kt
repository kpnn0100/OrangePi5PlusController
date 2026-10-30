package com.arstro.arstro_remote.phone.core

import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request as HttpRequest
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response as HttpResponse
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.IOException
import java.net.SocketTimeoutException
import java.net.URI
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/*
 * The connections to the device manager (docs/relay/PROTOCOL.md sections 2, 3 and 6): a WebSocket,
 * or an HTTP pipe - the same messages (text and binary, in order, with a close code) over plain
 * HTTP requests - when the device manager's proxy does not pass WebSockets. A port of
 * server/arstro_remote/relay/pipe.py (+ its ws.connect). Pure JVM + OkHttp.
 *
 * Transports.connect(url) chooses: mode "ws" | "http" | "auto" (default). auto tries the WebSocket
 * first; when that fails before it opens (anything but 401) it opens a pipe for the same route and
 * keeps using pipes for that device manager while the process runs.
 *
 * The pipe id is the pipe's only credential: it is never logged and never part of an error text.
 */

/** One message of a connection. */
class Msg(val text: Boolean, val data: ByteArray) {
    fun string() = String(data, Charsets.UTF_8)
}

/** The connection ended: the other side closed it (code, reason) or it was closed here. */
open class ConnClosed(val code: Int, reason: String) : IOException(reason)

/** The device manager refused the connection with this HTTP status (401: a password is needed). */
class ConnRefused(val status: Int, message: String) : IOException(message)

/** A message connection to the device manager: a WebSocket or a pipe. */
interface RelayConn {
    /** "ws" | "http" */
    val transport: String
    /** No more sends (closed here or by the other side, or lost). */
    val isClosed: Boolean
    /** The next message. Blocks up to timeoutMs (0 = no limit). Throws ConnClosed at the end,
     *  SocketTimeoutException after the timeout, IOException when the connection broke. */
    fun recv(timeoutMs: Long = 0): Msg
    /** Blocks while a lot is queued (backpressure). Throws IOException once closed / lost. */
    fun sendText(text: String)
    fun sendBinary(data: ByteArray)
    /** Close (sends a close with code / reason unless the other side closed first). */
    fun close(code: Int = 1000, reason: String = "")
}

internal fun describe(e: Throwable): String = when (e) {
    is ConnRefused -> e.message ?: "refused"
    is java.net.ConnectException -> "connection refused"
    is SocketTimeoutException -> "timed out"
    is java.net.UnknownHostException -> "unknown host"
    is javax.net.ssl.SSLException -> "secure connection failed (${e.message ?: "TLS"})"
    is ConnClosed -> "connection closed (${e.message})"
    else -> e.message?.takeIf { it.isNotBlank() } ?: e.javaClass.simpleName
}

private fun sendableCode(code: Int): Int =
    if (code in 1000..4999 && code !in 1004..1006 && code !in 1015..2999) code else 1000

private fun cutReason(reason: String): String {
    var r = reason
    while (r.toByteArray().size > 123) r = r.dropLast(1)
    return r
}

object Transports {
    /** "ws" | "http" | "auto" (tests force one). */
    @Volatile var mode: String = "auto"
    private val usePipes = ConcurrentHashMap.newKeySet<String>()
    private val routeRe = Regex("^(.*?)(/api/(?:agent/data|agent|connect/[^/?#]+))/?$")
    private val pipeIdRe = Regex("^[A-Za-z0-9_-]{16,128}$")

    /** WebSockets: no read timeout (the protocol has its own), pings find dead paths. */
    val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .pingInterval(30, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    /** Called with a sentence when the device manager does not take WebSockets (once per base URL). */
    @Volatile var onFallback: (String) -> Unit = {}

    fun forget() = usePipes.clear()

    /** ws(s)://host/base/api/agent -> ("http(s)://host/base", "/api/agent"). */
    fun split(url: String): Pair<String, String> {
        val u = URI(url)
        val scheme = when (u.scheme) { "ws", "http" -> "http"; "wss", "https" -> "https"; else -> null }
        require(scheme != null && !u.host.isNullOrEmpty()) { "the device manager URL must be http(s)://host/path" }
        val m = routeRe.find(u.rawPath ?: "") ?: throw IllegalArgumentException("not a device manager connection route")
        return "$scheme://${u.rawAuthority}${m.groupValues[1]}" to m.groupValues[2]
    }

    /** http(s)://host/base -> ws(s)://host/base (no trailing slash). */
    fun wsBase(url: String): String {
        val u = URI(url.trim())
        val scheme = when (u.scheme) { "https" -> "wss"; "http" -> "ws"; else -> null }
        require(scheme != null && !u.host.isNullOrEmpty()) { "the device manager URL must be http(s)://host/path" }
        return "$scheme://${u.rawAuthority}${(u.rawPath ?: "").trimEnd('/')}"
    }

    /** A connection to a device manager route (ws(s):// or http(s)://<base>/api/agent, /api/agent/data,
     *  /api/connect/<id>). Throws ConnRefused (401: no fallback), IOException otherwise. */
    fun connect(url: String, headers: Map<String, String> = emptyMap(), timeoutMs: Long = 10_000): RelayConn {
        val m = mode
        val (base, path) = split(url)
        if (m == "ws") return WsConn.open(url, headers, timeoutMs)
        if (m == "http" || base in usePipes) return openPipe(base, path, headers, timeoutMs)
        val wsError: IOException = try {
            return WsConn.open(url, headers, timeoutMs)
        } catch (e: ConnRefused) {
            if (e.status == 401) throw e
            e
        } catch (e: IOException) {
            e
        }
        val conn = try {
            openPipe(base, path, headers, timeoutMs)
        } catch (e: ConnRefused) {
            if (e.status == 404 || e.status == 405) {
                throw IOException("${describe(wsError)} (and the device manager has no HTTP transport: update it, " +
                                  "or let its proxy pass WebSockets)")
            }
            throw e
        }
        if (usePipes.add(base)) {
            try { onFallback("the device manager does not take WebSockets here - using HTTP (${describe(wsError)})") } catch (_: Exception) {}
        }
        return conn
    }

    /** POST <base>/api/pipe/open -> PipeConn. */
    fun openPipe(base: String, path: String, headers: Map<String, String> = emptyMap(), timeoutMs: Long = 10_000): PipeConn {
        val http = client.newBuilder().readTimeout(timeoutMs, TimeUnit.MILLISECONDS).pingInterval(0, TimeUnit.SECONDS).build()
        val body = JSONObject().put("path", path).toString().toRequestBody("application/json".toMediaType())
        val rb = HttpRequest.Builder().url(base.trimEnd('/') + "/api/pipe/open").post(body)
        headers.forEach { (k, v) -> rb.header(k, v) }
        val call = http.newCall(rb.build())
        call.timeout().timeout(timeoutMs + 5000, TimeUnit.MILLISECONDS)
        val status: Int
        val data: ByteArray
        try {
            call.execute().use { r -> status = r.code; data = r.body?.bytes() ?: ByteArray(0) }
        } catch (e: IOException) {
            throw if (e is SocketTimeoutException) e else IOException(describe(e), e)
        }
        if (status != 200) {
            if (status == 401) throw ConnRefused(401, "the server refused the token (401)")
            val err = errorText(data)
            throw ConnRefused(status, "the device manager answered $status${if (err.isNotEmpty()) ": $err" else ""}")
        }
        val d = try { JSONObject(String(data, Charsets.UTF_8)) } catch (e: Exception) {
            throw IOException("bad answer from the device manager to opening a pipe")
        }
        val id = d.opt("pipe")
        if (id !is String || !pipeIdRe.matches(id)) throw IOException("bad answer from the device manager to opening a pipe")
        val maxUp = d.optInt("max_up", E2e.PIPE_MAX_UP).let { if (it <= 0) E2e.PIPE_MAX_UP else it }.coerceIn(4096, 16 shl 20)
        val poll = d.optDouble("poll", E2e.PIPE_POLL.toDouble()).let { if (it.isNaN() || it <= 0) E2e.PIPE_POLL.toDouble() else it }
            .coerceIn(1.0, 120.0)
        return PipeConn(base, id, headers, maxUp, poll)
    }

    internal fun errorText(data: ByteArray): String = try {
        JSONObject(String(data, Charsets.UTF_8)).optString("error", "").take(200)
    } catch (e: Exception) { "" }
}

// ------------------------------------------------------------------------------------ pipe records
/** The record formats of the HTTP transport (PROTOCOL.md section 6). */
object PipeCodec {
    const val K_TEXT = 1
    const val K_BINARY = 2
    const val K_CLOSE = 8

    class Down(val kind: Int, val seq: Long, val payload: ByteArray)

    /** Up body: records kind u8 | length u32 BE | payload. */
    fun encodeUp(records: List<Pair<Int, ByteArray>>): ByteArray {
        val bb = ByteBuffer.allocate(records.sumOf { 5 + it.second.size })
        for ((k, p) in records) bb.put(k.toByte()).putInt(p.size).put(p)
        return bb.array()
    }

    /** Down body: records kind u8 | seq u32 BE | length u32 BE | payload. Throws IOException on a cut record. */
    fun decodeDown(body: ByteArray): List<Down> {
        val out = ArrayList<Down>()
        val buf = ByteBuffer.wrap(body)
        while (buf.hasRemaining()) {
            if (buf.remaining() < 9) throw IOException("bad answer from the device manager (cut record)")
            val k = buf.get().toInt() and 0xff
            val seq = buf.int.toLong() and 0xffffffffL
            val n = buf.int.toLong() and 0xffffffffL
            if (buf.remaining() < n) throw IOException("bad answer from the device manager (cut record)")
            val payload = ByteArray(n.toInt())
            buf.get(payload)
            out.add(Down(k, seq, payload))
        }
        return out
    }

    /** A close record's payload: code u16 BE + UTF-8 reason (at most 123 bytes). */
    fun closePayload(code: Int, reason: String): ByteArray {
        val r = cutReason(reason).toByteArray()
        return ByteBuffer.allocate(2 + r.size).putShort(sendableCode(code).toShort()).put(r).array()
    }

    /** -> (code, reason); 1005 when the payload has no code. */
    fun parseClose(payload: ByteArray): Pair<Int, String> =
        if (payload.size >= 2) (((payload[0].toInt() and 0xff) shl 8) or (payload[1].toInt() and 0xff)) to
            String(payload, 2, payload.size - 2, Charsets.UTF_8)
        else 1005 to ""
}

// ------------------------------------------------------------------------------------ WebSocket
/** A WebSocket through OkHttp, with the blocking interface of RelayConn. */
class WsConn private constructor() : RelayConn, WebSocketListener() {
    override val transport = "ws"
    private val lock = Object()
    private val inbox = ArrayDeque<Msg>()
    private var eof = false
    private var lost: String? = null
    private var localClosed = false
    private val opened = CountDownLatch(1)
    private var isOpen = false
    private var openError: Throwable? = null
    private var openStatus = 0
    private lateinit var ws: WebSocket
    var closeCode: Int? = null
        private set
    var closeReason = ""
        private set

    companion object {
        /** Throws ConnRefused (the handshake was answered with something else than 101) or IOException. */
        fun open(url: String, headers: Map<String, String>, timeoutMs: Long): WsConn {
            val c = WsConn()
            val rb = HttpRequest.Builder().url(url)
            headers.forEach { (k, v) -> rb.header(k, v) }
            c.ws = Transports.client.newWebSocket(rb.build(), c)
            if (!c.opened.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                c.ws.cancel()
                throw SocketTimeoutException("the WebSocket did not open in ${timeoutMs / 1000} s")
            }
            synchronized(c.lock) {
                if (!c.isOpen) {
                    val st = c.openStatus
                    if (st == 401) throw ConnRefused(401, "the server refused the token (401)")
                    if (st != 0) throw ConnRefused(st, "the device manager answered $st instead of a WebSocket")
                    val e = c.openError
                    throw IOException(if (e != null) describe(e) else "the WebSocket did not open", e)
                }
            }
            return c
        }
    }

    override val isClosed: Boolean get() = synchronized(lock) { localClosed || eof || lost != null }

    override fun onOpen(webSocket: WebSocket, response: HttpResponse) {
        synchronized(lock) { isOpen = true }
        opened.countDown()
    }

    override fun onMessage(webSocket: WebSocket, text: String) = add(Msg(true, text.toByteArray()))
    override fun onMessage(webSocket: WebSocket, bytes: ByteString) = add(Msg(false, bytes.toByteArray()))

    private fun add(m: Msg) = synchronized(lock) {
        if (!localClosed) inbox.addLast(m)
        lock.notifyAll()
    }

    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
        synchronized(lock) {
            if (closeCode == null) { closeCode = code; closeReason = reason }
            eof = true
            lock.notifyAll()
        }
        try { webSocket.close(sendableCode(code), null) } catch (_: Exception) {}
    }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
        synchronized(lock) {
            if (closeCode == null) { closeCode = code; closeReason = reason }
            eof = true
            lock.notifyAll()
        }
    }

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: HttpResponse?) {
        synchronized(lock) {
            if (!isOpen) {
                openError = t
                openStatus = response?.code ?: 0
                response?.close()
            } else if (!eof) {
                lost = describe(t)
            }
            lock.notifyAll()
        }
        opened.countDown()
    }

    override fun recv(timeoutMs: Long): Msg {
        synchronized(lock) {
            val deadline = if (timeoutMs > 0) System.nanoTime() + timeoutMs * 1_000_000 else 0L
            while (inbox.isEmpty()) {
                if (localClosed || eof || lost != null) break
                if (deadline == 0L) lock.wait() else {
                    val left = (deadline - System.nanoTime()) / 1_000_000
                    if (left <= 0) throw SocketTimeoutException("timed out")
                    lock.wait(left)
                }
            }
            if (localClosed) throw ConnClosed(1000, "closed")
            inbox.removeFirstOrNull()?.let { return it }
            if (eof) throw ConnClosed(closeCode ?: 1005, "closed by peer")
            throw IOException(lost ?: "connection lost")
        }
    }

    private fun waitRoom() {
        // OkHttp queues sends without limit (up to 16 MiB, then it fails the socket): wait instead
        var spins = 0
        while (ws.queueSize() > E2e.PIPE_BUFFER / 2) {
            if (isClosed) break
            Thread.sleep(if (spins++ < 20) 2 else 10)
        }
    }

    override fun sendText(text: String) {
        waitRoom()
        if (isClosed || !ws.send(text)) throw lostOrClosed()
    }

    override fun sendBinary(data: ByteArray) {
        waitRoom()
        if (isClosed || !ws.send(data.toByteString())) throw lostOrClosed()
    }

    private fun lostOrClosed(): IOException = synchronized(lock) {
        lost?.let { IOException(it) } ?: ConnClosed(closeCode ?: 1000, "closed")
    }

    override fun close(code: Int, reason: String) {
        val send: Boolean
        synchronized(lock) {
            if (localClosed) return
            localClosed = true
            send = !eof && lost == null
            inbox.clear()
            lock.notifyAll()
        }
        if (send) {
            try { if (!ws.close(sendableCode(code), cutReason(reason))) ws.cancel() } catch (_: Exception) { ws.cancel() }
            // the other side answers the close; a dead path is cut after a moment
            Thread {
                Thread.sleep(3000)
                synchronized(lock) { if (!eof) ws.cancel() }
            }.apply { isDaemon = true; name = "relay-ws-close" }.start()
        } else {
            ws.cancel()
        }
    }
}

// ------------------------------------------------------------------------------------ the pipe
/** One pipe (PROTOCOL.md section 6). Two threads: the poller (always one down request waiting;
 *  re-polls with the same ack after a network error, so nothing is lost or doubled) and the sender
 *  (one up request at a time, batches what is queued, retries with the same seq). */
class PipeConn internal constructor(
    private val base: String,
    pipeId: String,
    private val headers: Map<String, String>,
    val maxUp: Int,
    val poll: Double,
) : RelayConn {
    override val transport = "http"

    private companion object {
        const val K_TEXT = PipeCodec.K_TEXT
        const val K_BINARY = PipeCodec.K_BINARY
        const val K_CLOSE = PipeCodec.K_CLOSE
        const val SLACK = 15.0               // s a down request may take beyond the relay's poll time
        const val CLOSE_WAIT = 2000L         // ms close() waits for its close record to be taken
        const val CLOSE_FLUSH = 10_000L      // ms the sender keeps trying after close()
    }

    private val path = base.trimEnd('/') + "/api/pipe/" + pipeId
    private val http = Transports.client.newBuilder()
        .readTimeout(((poll + SLACK) * 1000).toLong(), TimeUnit.MILLISECONDS)
        .pingInterval(0, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()
    private val lock = Object()
    private val inbox = ArrayDeque<Msg>()
    private var inBytes = 0L
    private var received = 0L                // down messages received = the ack
    private val out = ArrayDeque<Pair<Int, ByteArray>>()   // not confirmed by the relay yet
    private var outBytes = 0L
    private var outBase = 0L                 // up index of out[0]
    private var localClosed = false
    private val closeDone = CountDownLatch(1)
    private var closeDeadline = 0L           // ms (monotonic), 0 = none
    private var eof = false                  // the other side's close record arrived
    private var lost: String? = null         // why the pipe broke
    @Volatile private var senderOk = true
    @Volatile private var alive = now()      // last answer from the device manager
    @Volatile var retries = 0                // requests sent again after a network error (tests)
        private set
    var closeCode: Int? = null
        private set
    var closeReason = ""
        private set
    private val calls = ConcurrentHashMap<String, Call>()
    @Volatile private var closedFlag = false

    init {
        Thread(::poller, "relay-pipe-down").apply { isDaemon = true }.start()
        Thread(::sender, "relay-pipe-up").apply { isDaemon = true }.start()
    }

    private fun now() = System.nanoTime() / 1_000_000

    override val isClosed: Boolean get() = closedFlag

    // ------------------------------------------------------------------ RelayConn
    override fun recv(timeoutMs: Long): Msg {
        synchronized(lock) {
            val deadline = if (timeoutMs > 0) now() + timeoutMs else 0L
            while (inbox.isEmpty()) {
                if (localClosed || eof || lost != null) break
                if (deadline == 0L) lock.wait() else {
                    val left = deadline - now()
                    if (left <= 0) throw SocketTimeoutException("timed out")
                    lock.wait(left)
                }
            }
            if (localClosed) throw ConnClosed(1000, "closed")
            inbox.removeFirstOrNull()?.let {
                inBytes -= it.data.size
                lock.notifyAll()
                return it
            }
            if (eof) throw ConnClosed(closeCode ?: 1005, "closed by peer")
            throw IOException(lost ?: "connection lost")
        }
    }

    override fun sendText(text: String) = send(K_TEXT, text.toByteArray())
    override fun sendBinary(data: ByteArray) = send(K_BINARY, data)

    private fun send(k: Int, payload: ByteArray) {
        if (5 + payload.size > maxUp) {
            close(1009, "message too big")
            throw ConnClosed(1009, "message too large for the HTTP transport (${payload.size} bytes)")
        }
        synchronized(lock) {
            while (outBytes > E2e.PIPE_BUFFER && !(closedFlag || lost != null)) lock.wait(1000)
            lost?.let { if (!localClosed) throw IOException(it) }
            if (closedFlag) throw ConnClosed(closeCode ?: 1000, "closed")
            out.addLast(k to payload)
            outBytes += payload.size
            lock.notifyAll()
        }
    }

    override fun close(code: Int, reason: String) {
        val send: Boolean
        val healthy: Boolean
        synchronized(lock) {
            if (localClosed) return
            localClosed = true
            send = !(closedFlag || lost != null)
            closedFlag = true
            inbox.clear()
            inBytes = 0
            if (send) {
                out.addLast(K_CLOSE to PipeCodec.closePayload(code, reason))
                closeDeadline = now() + CLOSE_FLUSH
            }
            lock.notifyAll()
            healthy = senderOk
        }
        if (send && healthy) closeDone.await(CLOSE_WAIT, TimeUnit.MILLISECONDS)
        if (!send) { abort("down"); abort("up") }
    }

    // ------------------------------------------------------------------ HTTP
    private fun request(which: String, method: String, query: String, body: ByteArray?, timeoutSec: Double): Pair<Int, ByteArray> {
        val rb = HttpRequest.Builder().url(path + query)
        headers.forEach { (k, v) -> rb.header(k, v) }
        if (body != null) rb.post(body.toRequestBody("application/octet-stream".toMediaType())) else rb.get()
        val call = http.newCall(rb.build())
        call.timeout().timeout(((timeoutSec + 5) * 1000).toLong(), TimeUnit.MILLISECONDS)
        calls[which] = call
        try {
            call.execute().use { r -> return r.code to (r.body?.bytes() ?: ByteArray(0)) }
        } finally {
            calls.remove(which, call)
        }
    }

    /** Break the request of one thread (unblocks a waiting down request). */
    private fun abort(which: String) {
        calls[which]?.cancel()
    }

    private fun expired() = now() - alive > E2e.PIPE_IDLE * 1000L

    private fun fail(why: String) {
        synchronized(lock) {
            if (lost == null && !eof) lost = why
            closedFlag = true
            lock.notifyAll()
        }
        closeDone.countDown()
        abort("down")
        abort("up")
    }

    private fun pause(delayMs: Long, stop: () -> Boolean) {
        val end = now() + delayMs
        synchronized(lock) {
            while (!stop()) {
                val left = end - now()
                if (left <= 0) return
                lock.wait(left)
            }
        }
    }

    // ------------------------------------------------------------------ down (the poller)
    private fun downGone() = eof || lost != null || (closeDeadline != 0L && now() > closeDeadline)

    private fun poller() {
        var delay = 0L
        while (true) {
            val ack: Long
            synchronized(lock) {
                val waited = now()                // the reader is slow: let it catch up first
                while (inBytes > E2e.PIPE_BUFFER && !downGone() && now() - waited < E2e.PIPE_IDLE * 500L) lock.wait(1000)
                ack = if (downGone()) -1 else received
            }
            if (ack < 0) break
            val res = try {
                request("down", "GET", "/down?ack=$ack", null, poll + SLACK)
            } catch (e: IOException) {
                if (downGone()) break
                retries++
                if (expired()) { fail("lost the connection to the device manager (${describe(e)})"); break }
                pause(delay, ::downGone)
                delay = (delay * 2).coerceAtLeast(250).coerceAtMost(5000)
                continue
            }
            val (status, data) = res
            when {
                status == 200 -> {
                    alive = now(); delay = 0
                    if (!deliver(data)) break
                }
                status == 204 -> { alive = now(); delay = 0 }
                status == 410 -> { fail("the device manager dropped the connection"); break }
                status >= 500 || status == 408 || status == 429 -> {
                    if (expired()) { fail("the device manager did not answer for ${E2e.PIPE_IDLE} s (HTTP $status)"); break }
                    pause(delay.coerceAtLeast(500), ::downGone)
                    delay = (delay * 2).coerceAtLeast(500).coerceAtMost(5000)
                }
                else -> {
                    val err = Transports.errorText(data)
                    fail("the device manager answered $status${if (err.isNotEmpty()) ": $err" else ""}")
                    break
                }
            }
        }
        finishPoller()
    }

    private fun finishPoller() {
        if (eof) {                               // tell the relay the close arrived (frees the pipe)
            try {
                val ack = synchronized(lock) { received }
                request("down", "GET", "/down?ack=$ack", null, 5.0)
            } catch (_: IOException) {}
        }
    }

    /** Records of one down answer -> the inbox. False when the pipe ended. */
    private fun deliver(body: ByteArray): Boolean {
        val items = try { PipeCodec.decodeDown(body) } catch (e: IOException) {
            fail(e.message ?: "bad answer from the device manager")
            return false
        }
        var bad: String? = null
        val isEof: Boolean
        synchronized(lock) {
            for (d in items) {
                val k = d.kind
                val seq = d.seq
                val payload = d.payload
                if (seq < received) continue      // sent again after a lost answer: already have it
                if (seq > received) { bad = "messages from the device manager were lost (expected $received, got $seq)"; break }
                received++
                if (k == K_TEXT || k == K_BINARY) {
                    if (!localClosed) {           // closed here: nobody reads any more
                        inbox.addLast(Msg(k == K_TEXT, payload))
                        inBytes += payload.size
                    }
                } else if (k == K_CLOSE) {
                    val (code, reason) = PipeCodec.parseClose(payload)
                    closeCode = code
                    closeReason = reason
                    eof = true
                    closedFlag = true
                    break
                } else {
                    bad = "bad answer from the device manager (record kind $k)"
                    break
                }
            }
            lock.notifyAll()
            isEof = eof
        }
        if (isEof) {
            closeDone.countDown()
            abort("up")
            return false
        }
        bad?.let { fail(it); return false }
        return true
    }

    // ------------------------------------------------------------------ up (the sender)
    private fun sender() {
        var delay = 0L
        val stop = { eof || lost != null }
        while (true) {
            val seq: Long
            val body: ByteArray
            synchronized(lock) {
                while (out.isEmpty() && !(eof || lost != null || localClosed)) lock.wait()
                if (eof || lost != null || out.isEmpty()) return finishSender()
                if (closeDeadline != 0L && now() > closeDeadline) return finishSender()
                seq = outBase
                var size = 0
                val parts = ArrayList<Pair<Int, ByteArray>>()
                for (item in out) {
                    val rec = 5 + item.second.size
                    if (parts.isNotEmpty() && size + rec > maxUp) break
                    parts.add(item)
                    size += rec
                }
                body = PipeCodec.encodeUp(parts)
            }
            val res = try {
                request("up", "POST", "/up?seq=$seq", body, poll + SLACK)
            } catch (e: IOException) {
                if (stop()) break
                retries++
                senderOk = false
                if (expired()) { fail("lost the connection to the device manager (${describe(e)})"); break }
                pause(delay, stop)
                delay = (delay * 2).coerceAtLeast(250).coerceAtMost(5000)
                continue
            }
            val (status, data) = res
            when {
                status == 200 || status == 409 -> {
                    alive = now(); senderOk = true; delay = 0
                    val nxt = try { JSONObject(String(data, Charsets.UTF_8)).getLong("next") } catch (e: Exception) {
                        fail("bad answer from the device manager to sending"); break
                    }
                    var bad = false
                    synchronized(lock) {
                        val drop = nxt - outBase
                        if (drop < 0 || drop > out.size) bad = true else {
                            repeat(drop.toInt()) {
                                val (k, p) = out.removeFirst()
                                outBytes -= p.size
                                if (k == K_CLOSE) closeDone.countDown()
                            }
                            outBase = nxt
                            lock.notifyAll()
                        }
                    }
                    if (bad) { fail("the device manager lost messages (it has $nxt, this side $outBase)"); break }
                    if (closeDone.count == 0L && localClosed) break
                }
                status == 410 -> {
                    if (localClosed) closeDone.countDown() else fail("the device manager dropped the connection")
                    break
                }
                status >= 500 || status == 408 || status == 429 -> {
                    senderOk = false
                    if (expired()) { fail("the device manager did not answer for ${E2e.PIPE_IDLE} s (HTTP $status)"); break }
                    pause(delay.coerceAtLeast(500), stop)
                    delay = (delay * 2).coerceAtLeast(500).coerceAtMost(5000)
                }
                else -> {
                    val err = Transports.errorText(data)
                    fail("the device manager refused a message ($status${if (err.isNotEmpty()) ": $err" else ""})")
                    break
                }
            }
        }
        finishSender()
    }

    private fun finishSender() {
        closeDone.countDown()
    }
}
