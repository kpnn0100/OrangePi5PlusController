package com.arstro.arstro_devices.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.SocketTimeoutException
import java.net.URLEncoder
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/*
 * The viewer side of the end-to-end channel (docs/relay/PROTOCOL.md sections 3 and 4): what the
 * browser viewer and server/arstro_remote/relay/client.py do, for the app (MGR-03). One Channel per
 * device; every request of the WebView is a stream of it. Pure JVM + OkHttp (the JVM tests run it
 * against the real device manager and a real Python device).
 *
 * Seal + send is one step under the send lock (nonce order = wire order); the reader thread opens.
 */

/** The device manager or the device refused, or the path broke; the message says why. */
class ViewerError(message: String) : IOException(message)

/** One stream of a channel: the reader thread queues its events, one consumer takes them. */
class Stream internal constructor(private val ch: Channel, val id: Long) {
    sealed class Event {
        class Json(val msg: JSONObject) : Event() { val t: String get() = msg.optString("t") }
        class Data(val flags: Int, val bytes: ByteArray) : Event()
        class Closed(val reason: String) : Event()
    }

    private val events = LinkedBlockingQueue<Event>()
    private val lock = Object()
    private var inflight = 0L
    private var unacked = 0L
    @Volatile var closed = false
        private set

    internal fun acked(n: Long) = synchronized(lock) {
        inflight = maxOf(0L, inflight - n)
        lock.notifyAll()
    }

    internal fun push(e: Event) = events.put(e)

    internal fun end(reason: String) {
        synchronized(lock) {
            closed = true
            lock.notifyAll()
        }
        events.put(Event.Closed(reason))
    }

    fun sendJson(obj: JSONObject) = ch.send(E2e.K_JSON, id, obj.toString().toByteArray())

    /** DATA in pieces of at most CHUNK, each waiting for room in the window; FIN only on the last
     *  piece. An empty message is one empty frame. */
    fun sendData(data: ByteArray, off: Int = 0, len: Int = data.size, flags: Int = 0) {
        var i = 0
        do {
            val n = minOf(E2e.CHUNK, len - i)
            synchronized(lock) {
                while (inflight >= E2e.WINDOW && !closed) lock.wait(1000)
                if (closed) throw ViewerError("the stream was closed")
                inflight += n
            }
            val last = i + n >= len
            ch.send(E2e.K_DATA, id, data, off + i, n, if (last) flags else flags and E2e.F_FIN.inv())
            i += n
        } while (i < len)
    }

    /** The next event; 0 = wait as long as it takes. Data is acknowledged as it is taken. */
    fun next(timeoutMs: Long = 0): Event {
        val e = (if (timeoutMs > 0) events.poll(timeoutMs, TimeUnit.MILLISECONDS) else events.take())
            ?: throw ViewerError("the device did not answer in time")
        if (e is Event.Data) {
            unacked += e.bytes.size
            if (unacked >= E2e.WINDOW / 4) {
                try { sendJson(JSONObject().put("t", "ack").put("n", unacked)) } catch (_: IOException) {}
                unacked = 0
            }
        }
        return e
    }

    /** Forget it here (the device ended it, or it is done). */
    fun close() = ch.drop(id)

    /** Give it up: the device stops sending (PROTOCOL.md 4.3 cancel), then forget it. */
    fun cancel() {
        if (!closed) try { sendJson(JSONObject().put("t", "cancel")) } catch (_: IOException) {}
        close()
    }
}

/** Why an event is not the expected answer, as a sentence. */
fun why(e: Stream.Event): String = when (e) {
    is Stream.Event.Json -> if (e.t == "error") e.msg.optString("error", "refused") else "unexpected answer from the device (${e.t})"
    is Stream.Event.Closed -> "the channel closed: ${e.reason}"
    is Stream.Event.Data -> "unexpected data from the device"
}

/** An end-to-end encrypted channel to one device. */
class Channel private constructor(private val conn: RelayConn, private val secure: Secure, val info: JSONObject) {
    companion object {
        /** Opens a channel and runs the handshake. Throws WrongKey (wrong access key), ViewerError. */
        fun open(base: String, deviceId: String, rawKey: ByteArray, password: String? = null,
                 timeoutMs: Long = 20_000): Channel {
            val url = Transports.wsBase(base) + "/api/connect/" + URLEncoder.encode(deviceId, "UTF-8")
            val headers = if (password.isNullOrEmpty()) emptyMap() else mapOf("Authorization" to "Bearer $password")
            val conn = try {
                Transports.connect(url, headers, timeoutMs)
            } catch (e: ConnRefused) {
                throw ViewerError(if (e.status == 401) "the device manager needs its viewer password" else describe(e))
            } catch (e: IOException) {
                throw ViewerError("can't reach the device manager: ${describe(e)}")
            }
            try {
                val first = text(conn, timeoutMs)
                if (first.optString("t") != "relay" || !first.optBoolean("ok")) {
                    throw ViewerError(first.optString("error").ifEmpty { "the device manager refused the connection" })
                }
                val (cn, hello) = E2e.viewerHello()
                conn.sendText(hello.toString())
                val (sec, proof) = try {
                    E2e.viewerCheck(rawKey, cn, text(conn, timeoutMs))
                } catch (e: ProtocolError) {
                    throw ViewerError(e.message ?: "refused")
                }
                conn.sendText(proof.toString())
                val m = conn.recv(timeoutMs)
                if (m.text) {
                    val err = try { JSONObject(m.string()).optString("error", "refused") } catch (_: Exception) { "refused" }
                    if ("wrong key" in err) throw WrongKey("wrong access key for this device")
                    throw ViewerError(err)
                }
                val f = try { sec.open(m.data) } catch (e: ProtocolError) { throw ViewerError(e.message ?: "bad frame") }
                val info = E2e.decodeJson(f.payload)
                if (f.kind != E2e.K_JSON || f.stream != 0L || info.optString("t") != "ready") {
                    throw ViewerError("the device did not send ready")
                }
                return Channel(conn, sec, info).also { it.startReader() }
            } catch (e: Exception) {
                conn.close()
                throw when (e) {
                    is WrongKey, is ViewerError -> e
                    is SocketTimeoutException -> ViewerError("the device did not answer in time")
                    is ConnClosed -> ViewerError(e.message?.ifEmpty { null } ?: "the device manager closed the connection")
                    is IOException -> ViewerError(describe(e))
                    is ProtocolError -> ViewerError(e.message ?: "protocol error")
                    else -> ViewerError(e.message ?: e.javaClass.simpleName)
                }
            }
        }

        private fun text(conn: RelayConn, timeoutMs: Long): JSONObject {
            val m = conn.recv(timeoutMs)
            if (!m.text) throw ViewerError("unexpected binary message during the handshake")
            return E2e.parseText(m.string())
        }
    }

    /** "ws" | "http" */
    val transport: String get() = conn.transport
    private val sendLock = Object()
    private val streams = ConcurrentHashMap<Long, Stream>()
    private val ids = AtomicLong(0)
    @Volatile var closed = false
        private set
    @Volatile var reason = ""
        private set
    /** Last time a stream was opened or ended (the app closes idle channels). */
    @Volatile var lastUse = System.currentTimeMillis()
        private set
    val openStreams: Int get() = streams.size

    internal fun send(kind: Int, sid: Long, payload: ByteArray, off: Int = 0, len: Int = payload.size, flags: Int = 0) {
        synchronized(sendLock) {
            if (closed) throw ViewerError("the channel is closed: $reason")
            conn.sendBinary(secure.seal(kind, sid, payload, off, len, flags))
        }
    }

    private fun startReader() = Thread({ read() }, "viewer-channel").apply { isDaemon = true; start() }

    private fun read() {
        var why = "closed"
        try {
            while (true) {
                val m = conn.recv()
                if (m.text) throw ProtocolError("text message after the handshake")
                val f = secure.open(m.data)
                if (f.stream == 0L) {
                    val msg = if (f.kind == E2e.K_JSON) E2e.decodeJson(f.payload) else null
                    if (msg?.optString("t") == "ping") send(E2e.K_JSON, 0, JSONObject().put("t", "pong").toString().toByteArray())
                    continue
                }
                val s = streams[f.stream] ?: continue
                if (f.kind == E2e.K_JSON) {
                    val msg = E2e.decodeJson(f.payload)
                    if (msg.optString("t") == "ack") s.acked(msg.optLong("n", 0)) else s.push(Stream.Event.Json(msg))
                } else {
                    s.push(Stream.Event.Data(f.flags, f.payload))
                }
            }
        } catch (e: ConnClosed) {
            why = if (closed) reason else "the device manager closed it (${e.code})"
        } catch (e: ProtocolError) {
            why = e.message ?: "protocol error"
        } catch (e: IOException) {
            why = "connection lost (${describe(e)})"
        }
        if (!closed) reason = why
        closed = true
        conn.close()
        for (s in streams.values) s.end(reason)
        streams.clear()
    }

    /** A new stream opened with `opener` (PROTOCOL.md 4.3). The id is taken and the opener sent under
     *  the send lock: the device takes a new stream only with an id above the last one it saw, so
     *  openers of parallel requests must go out in id order. */
    fun stream(opener: JSONObject): Stream = synchronized(sendLock) {
        val s = Stream(this, ids.incrementAndGet())
        streams[s.id] = s
        lastUse = System.currentTimeMillis()
        try {
            s.sendJson(opener)
        } catch (e: IOException) {
            streams.remove(s.id)
            throw e
        }
        s
    }

    internal fun drop(id: Long) {
        streams.remove(id)
        lastUse = System.currentTimeMillis()
    }

    fun close(why: String = "closed by this side") {
        if (closed) return
        reason = why
        closed = true
        conn.close()
    }

    // ------------------------------------------------------------------ one-shot requests
    /** The device's listening TCP ports: [{port, addr, proc}]. */
    fun ports(timeoutMs: Long = 20_000): JSONArray {
        val s = stream(JSONObject().put("t", "ports"))
        try {
            val e = s.next(timeoutMs)
            if (e is Stream.Event.Json && e.t == "ports") return e.msg.optJSONArray("ports") ?: JSONArray()
            throw ViewerError(why(e))
        } finally {
            s.close()
        }
    }

    /** The head of a GET: does anything answer HTTP on that port? -> {status, reason, headers}.
     *  The body is not fetched (the stream is cancelled). Throws ViewerError with the device's reason. */
    fun probe(port: Int, path: String = "/", headers: List<Pair<String, String>> = emptyList(),
              timeoutMs: Long = 20_000): JSONObject {
        val hs = JSONArray()
        for ((k, v) in headers) hs.put(JSONArray().put(k).put(v))
        val s = stream(JSONObject().put("t", "http").put("method", "GET").put("port", port).put("path", path)
            .put("headers", hs).put("body", false))
        try {
            val e = s.next(timeoutMs)
            if (e is Stream.Event.Json && e.t == "res") return e.msg
            throw ViewerError(why(e))
        } finally {
            s.cancel()
        }
    }
}
