package com.arstro.arstro_devices.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.util.Base64
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ThreadFactory

/*
 * The local door to one port of one device (MGR-04): an HTTP/1.1 server on 127.0.0.1 that turns
 * every request of the WebView into an `http` stream of the device's channel and every WebSocket
 * into a `ws` stream (PROTOCOL.md 4.3) - what the browser viewer's service worker and shim do, but
 * with a real origin, so the device's pages get real cookies, storage and WebSockets.
 *
 * The WebView reaches it as http://<label>.localhost:<port>/ (Chromium resolves *.localhost to
 * the loopback address and treats it as a secure context). <label> is a random secret per device
 * and port: a request whose Host is not exactly that gets 403, so other apps on the phone, which
 * can connect to 127.0.0.1 too, cannot use the channel. Each device and port is its own site, so
 * their cookies and storage stay apart.
 */
class Gateway(
    val label: String,
    val port: Int,
    private val channel: () -> Channel,
    private val tmpDir: File? = null,
    private val listener: Listener = object : Listener {},
) {
    interface Listener {
        /** The device answered a request on this port (any HTTP status). */
        fun responded(status: Int) {}
        /** A request could not reach the port: the reason as a sentence. */
        fun failed(why: String) {}
    }

    companion object {
        private val HOP = setOf("connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                                "proxy-connection", "te", "trailer", "trailers", "transfer-encoding", "upgrade")
        private const val WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        private const val MAX_HEAD = 64 * 1024
        private const val MAX_FRAME = 64L shl 20
        private const val MEMORY_BODY = 4 shl 20          // chunked uploads above this go to a temp file
        private val LOOPBACK: InetAddress = InetAddress.getByName("127.0.0.1")
        private val pool = Executors.newCachedThreadPool(ThreadFactory { r -> Thread(r, "gateway").apply { isDaemon = true } })

        fun newLabel(): String = "d" + E2e.randomBytes(12).joinToString("") { "%02x".format(it) }

        fun acceptKey(key: String): String = Base64.getEncoder().encodeToString(
            MessageDigest.getInstance("SHA-1").digest((key.trim() + WS_GUID).toByteArray()))
    }

    private var server: ServerSocket? = null
    private val sockets = ConcurrentHashMap.newKeySet<Socket>()
    @Volatile var stopped = false
        private set

    val localPort: Int get() = server?.localPort ?: 0
    val host: String get() = "$label.localhost:$localPort"
    val url: String get() = "http://$host/"

    /** Listens on 127.0.0.1:`preferred` (the same origin as last time keeps the pages' storage), or any
     *  free port when that one is taken. Returns the port. */
    fun start(preferred: Int = 0): Int {
        val s = bind(preferred) ?: bind(0) ?: throw IOException("no free local port")
        server = s
        pool.execute { accept(s) }
        return s.localPort
    }

    private fun bind(port: Int): ServerSocket? = try {
        ServerSocket().apply {
            reuseAddress = true
            bind(InetSocketAddress(LOOPBACK, port), 64)
        }
    } catch (e: IOException) {
        null
    }

    fun stop() {
        stopped = true
        try { server?.close() } catch (_: IOException) {}
        for (s in sockets) try { s.close() } catch (_: IOException) {}
        sockets.clear()
    }

    private fun accept(s: ServerSocket) {
        while (!stopped) {
            val sock = try { s.accept() } catch (e: IOException) { break }
            sockets.add(sock)
            pool.execute {
                try {
                    serve(sock)
                } catch (_: IOException) {
                } catch (_: RuntimeException) {
                } finally {
                    sockets.remove(sock)
                    try { sock.close() } catch (_: IOException) {}
                }
            }
        }
    }

    // ------------------------------------------------------------------ HTTP/1.1
    private class Request(val method: String, val target: String, val version: String, val headers: List<Pair<String, String>>) {
        fun header(name: String): String? = headers.firstOrNull { it.first.equals(name, true) }?.second
        fun has(name: String, token: String) = header(name)?.split(',')?.any { it.trim().equals(token, true) } == true
    }

    private fun serve(sock: Socket) {
        sock.tcpNoDelay = true
        sock.soTimeout = 120_000                       // an idle keep-alive connection
        val input = BufferedInputStream(sock.getInputStream(), 64 * 1024)
        val out = sock.getOutputStream()
        while (!stopped) {
            val req = try { readHead(input) } catch (e: SocketTimeoutException) { null } ?: return
            if (!req.header("host").equals(host, true)) {
                simple(out, 403, "Forbidden", "This address belongs to another app.", close = true)
                return
            }
            if (req.method == "GET" && req.has("upgrade", "websocket")) {
                sock.soTimeout = 0
                websocket(req, input, out)
                return
            }
            if (!http(req, input, out)) return
        }
    }

    private fun readLine(input: InputStream, budget: IntArray): String? {
        val b = ByteArrayOutputStream(128)
        while (true) {
            val c = input.read()
            if (c < 0) return if (b.size() == 0) null else throw IOException("connection closed in a header")
            if (--budget[0] < 0) throw IOException("request head too large")
            if (c == '\n'.code) break
            if (c != '\r'.code) b.write(c)
        }
        return b.toString("ISO-8859-1")
    }

    private fun readHead(input: InputStream): Request? {
        val budget = intArrayOf(MAX_HEAD)
        var line = readLine(input, budget) ?: return null
        while (line.isEmpty()) line = readLine(input, budget) ?: return null     // stray CRLF between requests
        val parts = line.split(' ')
        if (parts.size != 3) throw IOException("bad request line")
        val headers = ArrayList<Pair<String, String>>()
        while (true) {
            val h = readLine(input, budget) ?: throw IOException("connection closed in a header")
            if (h.isEmpty()) break
            val i = h.indexOf(':')
            if (i <= 0) throw IOException("bad header")
            headers.add(h.substring(0, i).trim() to h.substring(i + 1).trim())
        }
        return Request(parts[0], parts[1], parts[2], headers)
    }

    /** The request's headers for the device: hop-by-hop (and the ones Connection names), Host and,
     *  for WebSockets, Sec-WebSocket-* dropped (the device sets its own, PROTOCOL.md 4.4). */
    private fun forwardHeaders(req: Request, websocket: Boolean): JSONArray {
        val listed = req.header("connection")?.split(',')?.map { it.trim().lowercase() }?.toSet() ?: emptySet()
        val out = JSONArray()
        for ((k, v) in req.headers) {
            val lk = k.lowercase()
            if (lk in HOP || lk in listed || lk == "host" || lk.startsWith("proxy-")) continue
            if (websocket && lk.startsWith("sec-websocket-")) continue
            out.put(JSONArray().put(k).put(v))
        }
        return out
    }

    private fun path(req: Request): String {
        val t = req.target
        if (t.startsWith("/")) return t
        val m = Regex("^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*(/.*)?$").find(t) ?: throw IOException("bad request target")
        return m.groupValues[1].ifEmpty { "/" }
    }

    private fun write(out: OutputStream, text: String) = out.write(text.toByteArray(Charsets.ISO_8859_1))

    private fun simple(out: OutputStream, status: Int, reason: String, text: String, close: Boolean = false) {
        val body = page(status, reason, text)
        write(out, "HTTP/1.1 $status $reason\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: ${body.size}\r\n" +
                   "Cache-Control: no-store\r\n${if (close) "Connection: close\r\n" else ""}\r\n")
        out.write(body)
        out.flush()
    }

    private fun esc(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    /** The page the WebView shows when the port cannot be reached (the app's colours). */
    private fun page(status: Int, reason: String, text: String): ByteArray = (
        "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" +
        "<title>$status $reason</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;" +
        "background:#0b0c0f;color:#e6e8ec;font:16px/1.5 system-ui,sans-serif}main{max-width:420px;padding:24px}" +
        "h1{font-size:20px;margin:0 0 8px}p{color:#8b93a1;margin:0 0 16px}button{font:inherit;padding:10px 18px;" +
        "border-radius:10px;border:0;background:#7c9cff;color:#0b0e18;font-weight:600}</style></head><body><main>" +
        "<h1>Can't reach port $port</h1><p>${esc(text)}</p><button onclick='location.reload()'>Try again</button>" +
        "</main></body></html>").toByteArray()

    /** One request -> one http stream. Returns whether the connection can take the next request. */
    private fun http(req: Request, input: InputStream, out: OutputStream): Boolean {
        val http10 = req.version == "HTTP/1.0"
        var keep = if (http10) req.has("connection", "keep-alive") else !req.has("connection", "close")
        // the body: Content-Length is streamed; chunked is collected first (the device wants a length)
        val chunked = req.has("transfer-encoding", "chunked")
        val length = req.header("content-length")?.toLongOrNull() ?: 0L
        if (req.has("expect", "100-continue")) { write(out, "HTTP/1.1 100 Continue\r\n\r\n"); out.flush() }
        var spool: File? = null
        var memory: ByteArray? = null
        if (chunked) {
            val (m, f) = readChunked(input)
            memory = m
            spool = f
        }
        val bodyLength = when { memory != null -> memory.size.toLong(); spool != null -> spool.length(); else -> length }
        val headers = forwardHeaders(req, false)
        if (chunked || bodyLength > 0) headers.put(JSONArray().put("Content-Length").put(bodyLength.toString()))
        try {
            val s = try {
                channel().stream(JSONObject().put("t", "http").put("method", req.method).put("port", port)
                    .put("path", path(req)).put("headers", headers).put("body", bodyLength > 0 || chunked))
            } catch (e: IOException) {
                skip(input, if (chunked) 0 else length)
                return fail(out, e, keep)
            } catch (e: WrongKey) {
                skip(input, if (chunked) 0 else length)
                return fail(out, e, keep)
            }
            try {
                if (bodyLength > 0 || chunked) {
                    when {
                        memory != null -> s.sendData(memory)
                        spool != null -> spool.inputStream().use { pumpBody(it, spool.length(), s) }
                        else -> pumpBody(input, length, s)
                    }
                    s.sendJson(JSONObject().put("t", "end"))
                }
                val e = s.next(600_000)
                if (e !is Stream.Event.Json || e.t != "res") {
                    val why = why(e)
                    listener.failed(why)
                    simple(out, 502, "Bad Gateway", why)
                    return keep
                }
                val status = e.msg.optInt("status", 502)
                listener.responded(status)
                val noBody = req.method == "HEAD" || status in 100..199 || status == 204 || status == 304
                val rh = e.msg.optJSONArray("headers") ?: JSONArray()
                val sb = StringBuilder("HTTP/1.1 $status ${e.msg.optString("reason").ifEmpty { "-" }.replace("\r", "").replace("\n", "")}\r\n")
                var hasLength = false
                for (i in 0 until rh.length()) {
                    val h = rh.optJSONArray(i) ?: continue
                    val k = h.optString(0)
                    val v = h.optString(1)
                    val lk = k.lowercase()
                    if (lk in HOP || k.isEmpty() || '\r' in v || '\n' in v || '\r' in k || '\n' in k) continue
                    if (lk == "content-length") hasLength = true
                    sb.append(k).append(": ").append(v).append("\r\n")
                }
                val useChunked = !noBody && !hasLength && !http10
                if (!noBody && !hasLength && http10) keep = false
                if (useChunked) sb.append("Transfer-Encoding: chunked\r\n")
                sb.append(if (keep) "Connection: keep-alive\r\n" else "Connection: close\r\n").append("\r\n")
                out.write(sb.toString().toByteArray(Charsets.UTF_8))
                while (true) {
                    val ev = s.next()
                    when {
                        ev is Stream.Event.Data -> {
                            if (noBody || ev.bytes.isEmpty()) continue
                            if (useChunked) write(out, Integer.toHexString(ev.bytes.size) + "\r\n")
                            out.write(ev.bytes)
                            if (useChunked) write(out, "\r\n")
                            out.flush()
                        }
                        ev is Stream.Event.Json && ev.t == "end" -> {
                            if (useChunked) write(out, "0\r\n\r\n")
                            out.flush()
                            return keep
                        }
                        else -> {                        // broke in the middle: the client sees a cut response
                            listener.failed(why(ev))
                            return false
                        }
                    }
                }
            } catch (e: IOException) {
                s.cancel()
                return false
            } finally {
                s.close()
            }
        } finally {
            spool?.delete()
        }
    }

    private fun fail(out: OutputStream, e: Exception, keep: Boolean): Boolean {
        val why = if (e is WrongKey) "The access key saved in the app is wrong for this device." else e.message ?: "unreachable"
        listener.failed(why)
        simple(out, 502, "Bad Gateway", why)
        return keep
    }

    private fun skip(input: InputStream, n: Long) {
        var left = n
        val buf = ByteArray(16 * 1024)
        while (left > 0) {
            val r = input.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
            if (r < 0) break
            left -= r
        }
    }

    private fun pumpBody(input: InputStream, length: Long, s: Stream) {
        var left = length
        val buf = ByteArray(E2e.CHUNK)
        while (left > 0) {
            val r = input.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
            if (r < 0) throw IOException("the request body ended early")
            s.sendData(buf, 0, r)
            left -= r
        }
    }

    /** A chunked request body -> (bytes in memory, or a temp file when large). */
    private fun readChunked(input: InputStream): Pair<ByteArray?, File?> {
        val mem = ByteArrayOutputStream()
        var file: File? = null
        var fout: OutputStream? = null
        val budget = intArrayOf(MAX_HEAD)
        try {
            while (true) {
                val size = (readLine(input, budget) ?: throw IOException("chunked body ended early"))
                    .substringBefore(';').trim().toLong(16)
                if (size == 0L) {
                    while ((readLine(input, budget) ?: "").isNotEmpty()) {}       // trailers
                    break
                }
                var left = size
                val buf = ByteArray(64 * 1024)
                while (left > 0) {
                    val r = input.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                    if (r < 0) throw IOException("chunked body ended early")
                    if (fout == null && mem.size() + r > MEMORY_BODY && tmpDir != null) {
                        file = File.createTempFile("upload", ".part", tmpDir)
                        fout = file.outputStream()
                        mem.writeTo(fout)
                        mem.reset()
                    }
                    (fout ?: mem).write(buf, 0, r)
                    left -= r
                }
                readLine(input, budget)
                budget[0] = MAX_HEAD
            }
        } catch (e: IOException) {
            fout?.close()
            file?.delete()
            throw e
        }
        fout?.close()
        return if (file != null) null to file else mem.toByteArray() to null
    }

    // ------------------------------------------------------------------ WebSocket
    private fun websocket(req: Request, input: InputStream, out: OutputStream) {
        val key = req.header("sec-websocket-key")
        if (key.isNullOrBlank()) { simple(out, 400, "Bad Request", "Not a WebSocket request.", close = true); return }
        val protocols = JSONArray()
        req.header("sec-websocket-protocol")?.split(',')?.map { it.trim() }?.filter { it.isNotEmpty() }?.forEach { protocols.put(it) }
        val s = try {
            channel().stream(JSONObject().put("t", "ws").put("port", port).put("path", path(req))
                .put("headers", forwardHeaders(req, true)).put("protocols", protocols))
        } catch (e: Exception) {
            if (e !is IOException && e !is WrongKey) throw e
            fail(out, e, false)
            return
        }
        try {
            val e = s.next(30_000)
            if (e !is Stream.Event.Json || e.t != "open") {
                val why = why(e)
                listener.failed(why)
                simple(out, 502, "Bad Gateway", why, close = true)
                return
            }
            listener.responded(101)
            val proto = e.msg.optString("protocol")
            write(out, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
                       "Sec-WebSocket-Accept: ${acceptKey(key)}\r\n" +
                       (if (proto.isNotEmpty()) "Sec-WebSocket-Protocol: $proto\r\n" else "") + "\r\n")
            out.flush()
            val ws = WsLink(input, out, s)
            pool.execute { ws.up() }
            ws.down()
        } catch (_: IOException) {
            s.cancel()
        } finally {
            s.close()
        }
    }

    /** One WebSocket between the WebView and a ws stream. Messages may be split into frames on either side. */
    private class WsLink(private val input: InputStream, private val out: OutputStream, private val s: Stream) {
        @Volatile private var closeSent = false
        @Volatile private var finished = false          // one side ended it: the other only cleans up

        private fun frame(opcode: Int, fin: Boolean, data: ByteArray, off: Int = 0, len: Int = data.size) {
            synchronized(out) {
                if (closeSent) return
                if (opcode == 8) closeSent = true
                val head = ByteArrayOutputStream(10)
                head.write((if (fin) 0x80 else 0) or opcode)
                when {
                    len < 126 -> head.write(len)
                    len < 65536 -> { head.write(126); head.write(len shr 8); head.write(len and 0xff) }
                    else -> { head.write(127); for (i in 7 downTo 0) head.write(((len.toLong() shr (8 * i)) and 0xff).toInt()) }
                }
                out.write(head.toByteArray())
                out.write(data, off, len)
                out.flush()
            }
        }

        private fun closeFrame(code: Int, reason: String) {
            val r = reason.toByteArray().let { if (it.size > 123) it.copyOf(123) else it }
            val c = if (code in 1000..4999 && code !in 1004..1006 && code !in 1015..2999) code else 1000
            try { frame(8, true, byteArrayOf((c shr 8).toByte(), c.toByte()) + r) } catch (_: IOException) {}
        }

        /** device -> WebView (the connection's own thread; when it returns the socket is closed) */
        fun down() {
            try { pump() } finally { finished = true }
        }

        private fun pump() {
            var inMessage = false
            while (true) {
                when (val e = s.next()) {
                    is Stream.Event.Data -> {
                        val fin = e.flags and E2e.F_FIN != 0
                        val op = if (inMessage) 0 else if (e.flags and E2e.F_TEXT != 0) 1 else 2
                        frame(op, fin, e.bytes)
                        inMessage = !fin
                    }
                    is Stream.Event.Json -> {
                        if (e.t == "close") {
                            closeFrame(e.msg.optInt("code", 1000), e.msg.optString("reason"))
                            return
                        }
                        if (e.t == "error") {
                            closeFrame(1011, e.msg.optString("error"))
                            return
                        }
                    }
                    is Stream.Event.Closed -> {
                        if (!finished) closeFrame(1011, "the connection to the device was lost")
                        return
                    }
                }
            }
        }

        private fun exact(n: Int): ByteArray {
            val b = ByteArray(n)
            var i = 0
            while (i < n) {
                val r = input.read(b, i, n - i)
                if (r < 0) throw IOException("closed")
                i += r
            }
            return b
        }

        /** WebView -> device */
        fun up() {
            var text = false
            try {
                while (true) {
                    val h = exact(2)
                    val fin = h[0].toInt() and 0x80 != 0
                    val op = h[0].toInt() and 0x0f
                    val masked = h[1].toInt() and 0x80 != 0
                    var len = (h[1].toInt() and 0x7f).toLong()
                    if (len == 126L) len = exact(2).let { (it[0].toLong() and 0xff shl 8) or (it[1].toLong() and 0xff) }
                    else if (len == 127L) len = exact(8).fold(0L) { a, b -> (a shl 8) or (b.toLong() and 0xff) }
                    if (len > MAX_FRAME) throw IOException("frame too large")
                    val mask = if (masked) exact(4) else null
                    val data = exact(len.toInt())
                    if (mask != null) for (i in data.indices) data[i] = (data[i].toInt() xor mask[i and 3].toInt()).toByte()
                    when (op) {
                        1, 2, 0 -> {
                            if (op != 0) text = op == 1
                            s.sendData(data, flags = (if (text) E2e.F_TEXT else 0) or (if (fin) E2e.F_FIN else 0))
                        }
                        8 -> {
                            val code = if (data.size >= 2) (data[0].toInt() and 0xff shl 8) or (data[1].toInt() and 0xff) else 1000
                            val reason = if (data.size > 2) String(data, 2, data.size - 2, Charsets.UTF_8) else ""
                            try { s.sendJson(JSONObject().put("t", "close").put("code", code).put("reason", reason)) } catch (_: IOException) {}
                            closeFrame(code, reason)
                            finished = true
                            s.end("closed by the page")              // wakes down()
                            return
                        }
                        9 -> frame(10, true, data)
                    }
                }
            } catch (_: IOException) {
                if (finished) return                                   // down() ended it and closed the socket
                finished = true
                try { s.sendJson(JSONObject().put("t", "close").put("code", 1001).put("reason", "the page went away")) } catch (_: IOException) {}
                s.end("the page went away")
            }
        }
    }
}
