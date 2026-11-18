package com.arstro.arstro_remote_client.core

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayInputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.net.URLDecoder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import javax.net.ServerSocketFactory

/*
 * The phone's own web server (PHONE.md "On the network", PHN-20..): the same Router the device manager
 * reaches, on the phone's addresses - `http://<phone>:8080/` like an Arstro Remote machine, and
 * `https://<phone>:8443/` (a self-signed certificate, see SelfSigned) where browsers allow live video,
 * the browser's microphone and the clipboard (secure-context APIs).
 *
 * Everything but the login page needs a login: the password set in the app, then a session cookie
 * (`arstro_phone`, HttpOnly, SameSite=Strict) or `Authorization: Bearer <token>` from POST /api/login
 * for agents. Wrong passwords are slowed down per address (5 free tries, then 30 s doubling up to 15
 * min). WebSockets must come from the page's own origin. Pure JVM: the JVM tests run it on a free port.
 */
class LocalServer(
    private val handler: RequestHandler,
    /** The password now (the app may change it at any time); empty = nobody can log in. */
    private val password: () -> String,
    /** Session tokens that survive a restart of the service: load / save their hashes. */
    private val sessions: SessionStore = SessionStore.Memory(),
    private val log: (Int, String) -> Unit = { _, _ -> },
) {
    companion object {
        const val COOKIE = "arstro_phone"
        const val SESSION_DAYS = 30L
        private const val MAX_HEADER = 16 * 1024
        private const val MAX_WS_MESSAGE = 16 shl 20
        private const val IDLE_MS = 120_000
        private const val WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        private const val FREE_TRIES = 5
        private val random = SecureRandom()

        fun newToken(): String = ByteArray(24).also { random.nextBytes(it) }.let { Base64.getUrlEncoder().withoutPadding().encodeToString(it) }
        fun sha256(s: String): String = MessageDigest.getInstance("SHA-256").digest(s.toByteArray()).joinToString("") { "%02x".format(it) }

        fun parseQuery(q: String?): Map<String, List<String>> {
            if (q.isNullOrEmpty()) return emptyMap()
            val m = LinkedHashMap<String, MutableList<String>>()
            for (part in q.split('&')) {
                if (part.isEmpty()) continue
                val k = dec(part.substringBefore('='))
                val v = if ('=' in part) dec(part.substringAfter('=')) else ""
                m.getOrPut(k) { mutableListOf() }.add(v)
            }
            return m
        }

        private fun dec(s: String): String = try { URLDecoder.decode(s, "UTF-8") } catch (e: Exception) { s }

        private val REASONS = mapOf(200 to "OK", 204 to "No Content", 206 to "Partial Content", 301 to "Moved Permanently",
            303 to "See Other", 304 to "Not Modified", 400 to "Bad Request", 401 to "Unauthorized", 403 to "Forbidden",
            404 to "Not Found", 405 to "Method Not Allowed", 409 to "Conflict", 411 to "Length Required",
            413 to "Payload Too Large", 416 to "Range Not Satisfiable", 429 to "Too Many Requests",
            431 to "Request Header Fields Too Large", 500 to "Internal Server Error", 503 to "Service Unavailable")
    }

    /** Where the session tokens live (hashes only). */
    interface SessionStore {
        fun load(): Map<String, Long>            // sha256(token) -> expiry ms
        fun save(sessions: Map<String, Long>)
        class Memory : SessionStore {
            private var m: Map<String, Long> = emptyMap()
            override fun load() = m
            override fun save(sessions: Map<String, Long>) { m = HashMap(sessions) }
        }
    }

    private class Listener(val socket: ServerSocket, val tls: Boolean, val thread: Thread)

    private val listeners = mutableListOf<Listener>()
    private val clients = ConcurrentHashMap.newKeySet<Socket>()
    private val tokens = ConcurrentHashMap<String, Long>().apply { putAll(sessions.load()) }
    private val fails = ConcurrentHashMap<String, IntArray>()       // address -> [failures]
    private val blockedUntil = ConcurrentHashMap<String, Long>()
    private val sockets = ConcurrentHashMap.newKeySet<Ws>()
    @Volatile private var running = false
    /** Open WebSockets (viewers) now; the service keeps the CPU awake while there are some. */
    val viewers: Int get() = sockets.size
    @Volatile var onViewers: (Int) -> Unit = {}
    /** Last error of a listener (port in use, ...) or null. */
    @Volatile var error: String? = null
        private set
    /** The ports that listen now. */
    val ports: List<Int> get() = synchronized(listeners) { listeners.map { it.socket.localPort } }

    /** Listen on `port` (plain HTTP) and, when `tls` is given, on `tlsPort`. Port 0 = any free port (tests). */
    fun start(port: Int, tlsPort: Int = -1, tls: ServerSocketFactory? = null, bind: InetAddress? = null) {
        stop()
        running = true
        error = null
        open(port, false, ServerSocketFactory.getDefault(), bind)
        if (tls != null && tlsPort >= 0) open(tlsPort, true, tls, bind)
    }

    private fun open(port: Int, tls: Boolean, factory: ServerSocketFactory, bind: InetAddress?) {
        val ss = try {
            factory.createServerSocket().apply { reuseAddress = true; bind(java.net.InetSocketAddress(bind, port), 50) }
        } catch (e: IOException) {
            error = "port $port: ${e.message ?: e.javaClass.simpleName}"
            log(LogLevel.ERROR, "web: cannot listen on port $port (${e.javaClass.simpleName})")
            return
        }
        val t = Thread({ accept(ss, tls) }, "web-accept-${ss.localPort}")
        t.isDaemon = true
        synchronized(listeners) { listeners.add(Listener(ss, tls, t)) }
        t.start()
        log(LogLevel.INFO, "web: listening on port ${ss.localPort}${if (tls) " (https)" else ""}")
    }

    fun stop() {
        running = false
        val ls = synchronized(listeners) { listeners.toList().also { listeners.clear() } }
        ls.forEach { try { it.socket.close() } catch (_: IOException) {} }
        sockets.toList().forEach { it.close(1001, "the phone stopped sharing") }
        clients.toList().forEach { try { it.close() } catch (_: IOException) {} }
        clients.clear()
    }

    /** Log everybody out (the password changed). */
    fun dropSessions() {
        tokens.clear()
        sessions.save(emptyMap())
        sockets.toList().forEach { it.close(4401, "logged out") }
    }

    private fun accept(ss: ServerSocket, tls: Boolean) {
        while (running && !ss.isClosed) {
            val s = try { ss.accept() } catch (e: IOException) { if (running && !ss.isClosed) log(LogLevel.WARN, "web: accept failed"); break }
            clients.add(s)
            val t = Thread({ serve(s, tls) }, "web-conn")
            t.isDaemon = true
            t.start()
        }
    }

    // ------------------------------------------------------------------------------ HTTP
    private class Head(val method: String, val target: String, val headers: Map<String, String>)

    private fun readHead(input: InputStream): Head? {
        val buf = java.io.ByteArrayOutputStream()
        var last4 = 0
        while (true) {
            val b = input.read()
            if (b < 0) { if (buf.size() == 0) return null else throw EOFException() }
            buf.write(b)
            last4 = (last4 shl 8) or b
            if (last4 == 0x0d0a0d0a) break
            if (buf.size() > MAX_HEADER) throw HttpError(431, "the request headers are too big")
        }
        val lines = String(buf.toByteArray(), Charsets.ISO_8859_1).split("\r\n").filter { it.isNotEmpty() }
        val first = lines.firstOrNull()?.split(' ') ?: throw HttpError(400, "bad request")
        if (first.size != 3 || !first[2].startsWith("HTTP/1.")) throw HttpError(400, "bad request line")
        val h = LinkedHashMap<String, String>()
        for (l in lines.drop(1)) {
            val i = l.indexOf(':')
            if (i <= 0) continue
            val k = l.substring(0, i).trim().lowercase()
            val v = l.substring(i + 1).trim()
            h[k] = if (k in h) h[k] + ", " + v else v
        }
        return Head(first[0].uppercase(), first[1], h)
    }

    private class HttpError(val status: Int, message: String) : IOException(message)

    private fun serve(s: Socket, tls: Boolean) {
        val addr = s.inetAddress?.hostAddress ?: "?"
        try {
            s.soTimeout = IDLE_MS
            s.tcpNoDelay = true
            val input = BufferedInputStream(s.getInputStream(), 16 * 1024)
            val out = s.getOutputStream()
            while (running) {
                val head = try { readHead(input) } catch (e: HttpError) {
                    write(out, Response.error(e.status, e.message ?: "bad request"), false, close = true); return
                } ?: return
                val body = bodyOf(head, input)
                val keep = head.headers["connection"]?.lowercase()?.contains("close") != true
                if (head.headers["upgrade"]?.lowercase() == "websocket") {
                    websocket(s, head, input, out, addr, tls)
                    return
                }
                val resp = try { route(head, body, addr, tls) } catch (e: HttpError) { Response.error(e.status, e.message ?: "refused") }
                write(out, resp, head.method == "HEAD", close = !keep)
                // the rest of a body nobody read (a refused upload) must not be taken for the next request
                if (body != null && !drain(body)) return
                if (!keep) return
            }
        } catch (e: SocketTimeoutException) {
        } catch (e: SocketException) {
        } catch (e: EOFException) {
        } catch (e: javax.net.ssl.SSLException) {
            log(LogLevel.DEBUG, "web: tls from $addr failed: ${e.message?.take(80)}")
        } catch (e: IOException) {
            log(LogLevel.DEBUG, "web: $addr: ${e.javaClass.simpleName}")
        } catch (e: Exception) {
            log(LogLevel.WARN, "web: $addr: ${e.javaClass.simpleName}")
        } finally {
            clients.remove(s)
            try { s.close() } catch (_: IOException) {}
        }
    }

    private fun drain(body: InputStream): Boolean = try {
        val buf = ByteArray(16 * 1024)
        var left = 64L shl 20
        while (left > 0) { val n = body.read(buf); if (n < 0) break; left -= n }
        left > 0
    } catch (e: IOException) { false }

    private fun bodyOf(head: Head, input: InputStream): InputStream? {
        val te = head.headers["transfer-encoding"]?.lowercase()
        if (te != null && te.contains("chunked")) return ChunkedInput(input)
        val len = head.headers["content-length"]?.toLongOrNull() ?: return null
        if (len < 0) throw HttpError(400, "bad Content-Length")
        return if (len == 0L) null else Bounded(input, len)
    }

    private fun route(head: Head, body: InputStream?, addr: String, tls: Boolean): Response {
        val path = head.target.substringBefore('?').substringBefore('#')
        val query = parseQuery(head.target.substringAfter('?', ""))
        if (!path.startsWith("/") || path.contains("/../") || path.endsWith("/..")) return Response.error(400, "bad path")
        val authed = authed(head.headers)
        when (path) {
            "/login" -> return when (head.method) {
                "GET", "HEAD" -> if (authed) redirect("./") else loginPage(null, tls)
                "POST" -> loginForm(head, body, addr, tls)
                else -> Response.error(405, "method not allowed")
            }
            "/api/login" -> return if (head.method == "POST") loginApi(head, body, addr) else Response.error(405, "use POST")
            "/logout", "/api/logout" -> {
                cookieToken(head.headers)?.let { forget(it) }
                bearer(head.headers)?.let { forget(it) }
                return if (path == "/logout") redirect("login", clear = true)
                       else Response.json(JSONObject().put("ok", true), 200).with("Set-Cookie" to clearCookie())
            }
            "/api/ping" -> return Response.json(JSONObject().put("ok", true).put("app", "arstro-phone").put("login", authed))
            "/favicon.ico" -> if (!authed) return Response(204)
        }
        if (!authed) {
            val wantsPage = head.method == "GET" && (path == "/" || path == "/index.html") &&
                head.headers["accept"]?.contains("text/html") != false
            return if (wantsPage) redirect("login") else Response.error(401, "log in first (POST /api/login)")
        }
        // the Router's own CSRF guard is SameSite=Strict on the cookie; a cross-site form post has no cookie
        return handler.handle(Request(head.method, path, query, head.headers, body, head.headers["content-length"]?.toLongOrNull() ?: -1))
    }

    private fun Response.with(h: Pair<String, String>) = Response(status, headers + h, body, length)

    private fun redirect(to: String, clear: Boolean = false): Response {
        val h = mutableListOf("Location" to to, "Cache-Control" to "no-store")
        if (clear) h.add("Set-Cookie" to clearCookie())
        return Response(303, h, ByteArrayInputStream(ByteArray(0)), 0)
    }

    // ------------------------------------------------------------------------------ login
    private fun cookieToken(h: Map<String, String>): String? = h["cookie"]?.split(';')?.map { it.trim() }
        ?.firstOrNull { it.startsWith("$COOKIE=") }?.substringAfter('=')?.takeIf { it.isNotEmpty() }

    private fun bearer(h: Map<String, String>): String? =
        h["authorization"]?.takeIf { it.startsWith("Bearer ", ignoreCase = true) }?.substring(7)?.trim()?.takeIf { it.isNotEmpty() }

    private fun valid(token: String?): Boolean {
        if (token == null) return false
        val k = sha256(token)
        val exp = tokens[k] ?: return false
        if (exp < System.currentTimeMillis()) { tokens.remove(k); sessions.save(HashMap(tokens)); return false }
        return true
    }

    private fun authed(h: Map<String, String>) = valid(cookieToken(h)) || valid(bearer(h))

    private fun forget(token: String) {
        if (tokens.remove(sha256(token)) != null) sessions.save(HashMap(tokens))
    }

    private fun issue(): String {
        val t = newToken()
        val now = System.currentTimeMillis()
        tokens.entries.removeIf { it.value < now }
        tokens[sha256(t)] = now + TimeUnit.DAYS.toMillis(SESSION_DAYS)
        sessions.save(HashMap(tokens))
        return t
    }

    private fun cookie(token: String, tls: Boolean) =
        "$COOKIE=$token; Path=/; Max-Age=${TimeUnit.DAYS.toSeconds(SESSION_DAYS)}; HttpOnly; SameSite=Strict" + (if (tls) "; Secure" else "")

    private fun clearCookie() = "$COOKIE=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"

    /** null = the password is right; else the refusal (wrong, or too many tries from this address). */
    private fun check(given: String, addr: String): Response? {
        val now = System.currentTimeMillis()
        val until = blockedUntil[addr] ?: 0
        if (until > now) {
            val s = (until - now + 999) / 1000
            return Response.error(429, "Too many wrong passwords - try again in $s s").with("Retry-After" to "$s")
        }
        val want = password()
        val ok = want.isNotEmpty() && MessageDigest.isEqual(want.toByteArray(), given.toByteArray())
        if (ok) { fails.remove(addr); blockedUntil.remove(addr); return null }
        val n = fails.getOrPut(addr) { IntArray(1) }.let { it[0]++; it[0] }
        log(LogLevel.WARN, "web: wrong password from $addr ($n)")
        if (n >= FREE_TRIES) {
            val secs = (30L shl (n - FREE_TRIES).coerceAtMost(5)).coerceAtMost(900)
            blockedUntil[addr] = now + secs * 1000
        }
        return Response.error(401, if (want.isEmpty()) "No password is set on the phone yet" else "Wrong password")
    }

    private fun loginApi(head: Head, body: InputStream?, addr: String): Response {
        val text = body?.let { Router.readAll(it, 64 * 1024) }?.let { String(it, Charsets.UTF_8) } ?: ""
        val pw = try { JSONObject(text).optString("password", "") } catch (e: Exception) { return Response.error(400, "send {\"password\": \"...\"}") }
        check(pw, addr)?.let { return it }
        val t = issue()
        log(LogLevel.INFO, "web: $addr logged in (api)")
        return Response.json(JSONObject().put("ok", true).put("token", t).put("expires_days", SESSION_DAYS))
    }

    private fun loginForm(head: Head, body: InputStream?, addr: String, tls: Boolean): Response {
        val text = body?.let { Router.readAll(it, 64 * 1024) }?.let { String(it, Charsets.UTF_8) } ?: ""
        val pw = if (head.headers["content-type"]?.contains("json") == true) {
            try { JSONObject(text).optString("password", "") } catch (e: Exception) { "" }
        } else parseQuery(text)["password"]?.firstOrNull() ?: ""
        val refused = check(pw, addr)
        if (refused != null) {
            val msg = try { JSONObject(String(refused.body!!.readBytes())).optString("error") } catch (e: Exception) { "Wrong password" }
            return loginPage(msg, tls, refused.status)
        }
        log(LogLevel.INFO, "web: $addr logged in")
        return redirect("./").with("Set-Cookie" to cookie(issue(), tls))
    }

    private fun loginPage(error: String?, tls: Boolean, status: Int = 200): Response {
        val err = error?.let { "<p class=\"err\">${esc(it)}</p>" } ?: ""
        val note = if (tls) "" else "<p class=\"note\">Live video, calls and the clipboard need the secure address: " +
            "the same address with <b>https</b> and port <b>8443</b> (accept the certificate warning once).</p>"
        val html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Log in · Arstro phone</title>
<style>
:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0c0e;color:#e8e8ea;
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
form{width:min(340px,calc(100vw - 32px));padding:28px;border:1px solid #23252a;border-radius:12px;background:#121317}
h1{font-size:18px;font-weight:600;margin:0 0 4px}p{margin:0 0 18px;color:#8b8d94;font-size:13px}
label{display:block;font-size:12px;color:#8b8d94;margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em}
input{box-sizing:border-box;width:100%;padding:11px 12px;border-radius:6px;border:1px solid #2c2f35;background:#0b0c0e;color:#e8e8ea;font-size:15px}
button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:6px;background:#e8e8ea;color:#0b0c0e;font-weight:600;font-size:15px}
.err{color:#ff8a80;margin:14px 0 0}.note{margin:16px 0 0}
</style></head><body><form method="post" action="login">
<h1>Arstro phone</h1><p>Enter the password shown in the Arstro Remote Client app.</p>
<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Log in</button>$err$note</form></body></html>"""
        val b = html.toByteArray()
        return Response(status, listOf("Content-Type" to "text/html; charset=utf-8", "Cache-Control" to "no-store",
            "Content-Security-Policy" to "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
            "X-Frame-Options" to "DENY", "Referrer-Policy" to "no-referrer"), ByteArrayInputStream(b), b.size.toLong())
    }

    private fun esc(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")

    // ------------------------------------------------------------------------------ writing
    private fun write(out: OutputStream, r: Response, head: Boolean, close: Boolean) {
        val sb = StringBuilder("HTTP/1.1 ${r.status} ${REASONS[r.status] ?: "Status"}\r\n")
        val names = HashSet<String>()
        for ((k, v) in r.headers) {
            if (k.equals("Content-Length", true) || k.equals("Transfer-Encoding", true) || k.equals("Connection", true)) continue
            names.add(k.lowercase())
            sb.append(k).append(": ").append(v.replace("\r", "").replace("\n", "")).append("\r\n")
        }
        if ("x-frame-options" !in names) sb.append("X-Frame-Options: SAMEORIGIN\r\n")
        val chunked = r.body != null && r.length < 0
        when {
            r.body == null -> sb.append("Content-Length: 0\r\n")
            chunked -> sb.append("Transfer-Encoding: chunked\r\n")
            else -> sb.append("Content-Length: ${r.length}\r\n")
        }
        if (close) sb.append("Connection: close\r\n")
        sb.append("\r\n")
        out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
        val body = r.body
        if (body != null) body.use {
            if (!head) {
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val n = it.read(buf)
                    if (n < 0) break
                    if (n == 0) continue
                    if (chunked) { out.write("${Integer.toHexString(n)}\r\n".toByteArray()); out.write(buf, 0, n); out.write("\r\n".toByteArray()) }
                    else out.write(buf, 0, n)
                }
                if (chunked) out.write("0\r\n\r\n".toByteArray())
            }
        }
        out.flush()
    }

    // ------------------------------------------------------------------------------ WebSocket
    private fun websocket(s: Socket, head: Head, input: InputStream, out: OutputStream, addr: String, tls: Boolean) {
        val path = head.target.substringBefore('?')
        val query = parseQuery(head.target.substringAfter('?', ""))
        val key = head.headers["sec-websocket-key"]
        if (key == null || head.headers["sec-websocket-version"] != "13") {
            write(out, Response.error(400, "not a WebSocket request"), false, true); return
        }
        // a page of another site must not open the phone's sockets with the viewer's login
        val origin = head.headers["origin"]
        val host = head.headers["host"]
        if (origin != null && host != null && origin.substringAfter("://").trimEnd('/') != host) {
            write(out, Response.error(403, "WebSocket from another site"), false, true); return
        }
        if (!authed(head.headers)) { write(out, Response.error(401, "log in first"), false, true); return }
        val accept = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-1").digest((key + WS_GUID).toByteArray()))
        out.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
            "Sec-WebSocket-Accept: $accept\r\n\r\n").toByteArray())
        out.flush()
        s.soTimeout = 0
        val ws = Ws(s, out, path, query)
        val h: WsHandler = try { handler.openSocket(ws) } catch (e: OpError) {
            ws.close(4000 + e.status, e.message ?: "refused"); ws.finish(); return
        } catch (e: Exception) {
            log(LogLevel.WARN, "web: socket $path: ${e.javaClass.simpleName}")
            ws.close(4500, e.message?.take(100) ?: "failed"); ws.finish(); return
        }
        sockets.add(ws)
        onViewers(sockets.size)
        var code = 1006
        var reason = ""
        try {
            val msg = java.io.ByteArrayOutputStream()
            var msgOp = 0
            while (true) {
                val b0 = input.read(); if (b0 < 0) break
                val b1 = input.read(); if (b1 < 0) break
                val fin = b0 and 0x80 != 0
                val op = b0 and 0x0f
                if (b1 and 0x80 == 0) { code = 1002; reason = "unmasked frame"; break }
                var len = (b1 and 0x7f).toLong()
                if (len == 126L) len = readN(input, 2).fold(0L) { a, x -> (a shl 8) or (x.toLong() and 0xff) }
                else if (len == 127L) len = readN(input, 8).fold(0L) { a, x -> (a shl 8) or (x.toLong() and 0xff) }
                if (len < 0 || len > MAX_WS_MESSAGE) { code = 1009; reason = "message too big"; break }
                val mask = readN(input, 4)
                val data = readN(input, len.toInt())
                for (i in data.indices) data[i] = (data[i].toInt() xor mask[i and 3].toInt()).toByte()
                when (op) {
                    0x8 -> {
                        ws.closeReceived = true
                        code = if (data.size >= 2) ((data[0].toInt() and 0xff) shl 8) or (data[1].toInt() and 0xff) else 1005
                        reason = if (data.size > 2) String(data, 2, data.size - 2, Charsets.UTF_8) else ""
                        ws.close(if (code == 1005) 1000 else code.coerceIn(1000, 4999), "")
                        break
                    }
                    0x9 -> ws.pong(data)
                    0xA -> {}
                    0x0, 0x1, 0x2 -> {
                        if (op != 0) { msg.reset(); msgOp = op }
                        msg.write(data)
                        if (msg.size() > MAX_WS_MESSAGE) { code = 1009; reason = "message too big"; break }
                        if (fin) {
                            val m = msg.toByteArray()
                            msg.reset()
                            try {
                                if (msgOp == 1) h.onText(ws, String(m, Charsets.UTF_8)) else h.onBinary(ws, m)
                            } catch (e: Exception) {
                                log(LogLevel.WARN, "web: socket ${ws.path}: ${e.javaClass.simpleName}")
                            }
                        }
                    }
                    else -> { code = 1002; reason = "bad opcode"; break }
                }
            }
        } catch (e: IOException) {
        } finally {
            sockets.remove(ws)
            onViewers(sockets.size)
            try { h.onClose(ws, code, reason) } catch (e: Exception) { log(LogLevel.WARN, "web: close ${ws.path}: ${e.javaClass.simpleName}") }
            if (ws.isOpen) ws.close(if (code in 1000..4999 && code != 1005 && code != 1006) code else 1000, "")
            ws.finish()
            log(LogLevel.DEBUG, "web: socket ${ws.path} from $addr closed ($code)")
        }
    }

    private fun readN(input: InputStream, n: Int): ByteArray {
        val b = ByteArray(n)
        var off = 0
        while (off < n) { val r = input.read(b, off, n - off); if (r < 0) throw EOFException(); off += r }
        return b
    }

    /** One open WebSocket: ordered, thread-safe sends through a writer thread (so `queued` is real). */
    private inner class Ws(val s: Socket, val out: OutputStream, override val path: String,
                           override val query: Map<String, List<String>>) : WsSession {
        private val q = LinkedBlockingQueue<ByteArray>()
        private val pending = AtomicLong()
        @Volatile private var open = true
        @Volatile var closeReceived = false
        private val writer = Thread({ pump() }, "web-ws-out").apply { isDaemon = true; start() }

        override val isOpen get() = open
        override val queued: Long get() = pending.get()
        override fun sendText(text: String) = enqueue(frame(0x1, text.toByteArray()))
        override fun sendBinary(data: ByteArray) = enqueue(frame(0x2, data))

        override fun close(code: Int, reason: String) {
            if (!open) return
            val r = reason.toByteArray().let { if (it.size > 120) it.copyOf(120) else it }
            val p = ByteArray(2 + r.size)
            p[0] = (code shr 8).toByte(); p[1] = code.toByte()
            System.arraycopy(r, 0, p, 2, r.size)
            enqueue(frame(0x8, p))
            open = false
            q.put(ByteArray(0))       // the writer ends after the close frame
        }

        fun pong(payload: ByteArray) = enqueue(frame(0xA, payload), force = true)

        private fun enqueue(f: ByteArray, force: Boolean = false) {
            if (!open && !force) return
            pending.addAndGet(f.size.toLong())
            q.put(f)
        }

        private fun pump() {
            try {
                while (true) {
                    val f = q.take()
                    if (f.isEmpty()) break
                    out.write(f)
                    if (q.isEmpty()) out.flush()
                    pending.addAndGet(-f.size.toLong())
                }
                out.flush()
            } catch (e: Exception) {
                open = false
            } finally {
                q.clear()
                pending.set(0)
            }
        }

        /** Wait a little for the close frame to go out, then shut the socket. */
        fun finish() {
            open = false
            q.put(ByteArray(0))
            try { writer.join(2000) } catch (_: InterruptedException) {}
            try { s.close() } catch (_: IOException) {}
        }

        private fun frame(op: Int, data: ByteArray): ByteArray {
            val n = data.size
            val hdr = when {
                n < 126 -> byteArrayOf((0x80 or op).toByte(), n.toByte())
                n < 65536 -> byteArrayOf((0x80 or op).toByte(), 126, (n shr 8).toByte(), n.toByte())
                else -> ByteArray(10).also { b ->
                    b[0] = (0x80 or op).toByte(); b[1] = 127
                    for (i in 0 until 8) b[9 - i] = (n.toLong() shr (8 * i)).toByte()
                }
            }
            return hdr + data
        }
    }

    // ------------------------------------------------------------------------------ bodies
    private class Bounded(private val input: InputStream, private var left: Long) : InputStream() {
        override fun read(): Int {
            if (left <= 0) return -1
            val b = input.read()
            if (b < 0) throw EOFException("the body ended early") else left--
            return b
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (left <= 0) return -1
            val n = input.read(b, off, minOf(len.toLong(), left).toInt())
            if (n < 0) throw EOFException("the body ended early")
            left -= n
            return n
        }

        override fun available(): Int = minOf(input.available().toLong(), left).toInt()
    }

    private class ChunkedInput(private val input: InputStream) : InputStream() {
        private var left = 0L
        private var done = false

        private fun line(): String {
            val sb = StringBuilder()
            while (true) {
                val c = input.read()
                if (c < 0) throw EOFException()
                if (c == '\n'.code) break
                if (c != '\r'.code) sb.append(c.toChar())
                if (sb.length > 1024) throw IOException("bad chunk")
            }
            return sb.toString()
        }

        private fun next(): Boolean {
            if (done) return false
            if (left == 0L) {
                left = line().substringBefore(';').trim().toLongOrNull(16) ?: throw IOException("bad chunk size")
                if (left == 0L) {
                    while (line().isNotEmpty()) { }
                    done = true
                    return false
                }
            }
            return true
        }

        override fun read(): Int {
            val b = ByteArray(1)
            return if (read(b, 0, 1) < 0) -1 else b[0].toInt() and 0xff
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (!next()) return -1
            val n = input.read(b, off, minOf(len.toLong(), left).toInt())
            if (n < 0) throw EOFException()
            left -= n
            if (left == 0L) line()
            return n
        }
    }
}
