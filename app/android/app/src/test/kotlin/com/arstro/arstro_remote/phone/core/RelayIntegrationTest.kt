package com.arstro.arstro_remote.phone.core

import org.json.JSONArray
import org.json.JSONObject
import org.junit.AfterClass
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.BeforeClass
import org.junit.Test
import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.nio.file.Files
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * RelayAgent + Router against the REAL device manager (`python3 -m arstro_relay` from <repo>/relay)
 * and the REAL Python viewer client (`arstro_remote.relay.client`), over WebSockets, over forced HTTP
 * pipes, and through a proxy that refuses WebSockets (the agent falls back to pipes by itself).
 * A Kotlin viewer (E2e viewer side) covers ws streams, tcp / ports and flow control.
 */
class RelayIntegrationTest {
    companion object {
        private const val TOKEN = "join-token-for-tests"
        private lateinit var relay: Process
        private var port = 0
        private lateinit var data: File
        private lateinit var log: File
        private val repo get() = E2eTest.repo

        fun freePort(): Int = ServerSocket(0).use { it.localPort }

        fun base(p: Int = port) = "http://127.0.0.1:$p/arstro/internal/devices"

        @BeforeClass @JvmStatic fun startRelay() {
            port = freePort()
            data = Files.createTempDirectory("relay-data").toFile()
            log = File.createTempFile("relay", ".log")
            val pb = ProcessBuilder("python3", "-m", "arstro_relay").directory(File(repo, "relay"))
                .redirectErrorStream(true).redirectOutput(log)
            pb.environment().keys.removeIf { it.startsWith("RELAY_") }
            pb.environment().putAll(mapOf("RELAY_HOST" to "127.0.0.1", "RELAY_PORT" to "$port", "RELAY_DATA" to data.path,
                                          "RELAY_JOIN_TOKEN" to TOKEN, "RELAY_RATE_LIMIT" to "100000/10",
                                          "PYTHONDONTWRITEBYTECODE" to "1"))
            relay = pb.start()
            val until = System.currentTimeMillis() + 15_000
            while (System.currentTimeMillis() < until) {
                if (!relay.isAlive) error("the relay did not start: ${log.readText()}")
                try {
                    val c = URL(base() + "/api/health").openConnection() as HttpURLConnection
                    if (c.responseCode == 200) return
                } catch (_: Exception) {}
                Thread.sleep(100)
            }
            error("the relay did not answer: ${log.readText()}")
        }

        @AfterClass @JvmStatic fun stopRelay() {
            relay.destroy()
            relay.waitFor(5, TimeUnit.SECONDS)
            data.deleteRecursively()
            log.delete()
            Transports.mode = "auto"
        }
    }

    // ------------------------------------------------------------------ helpers
    class Device(val agent: RelayAgent, val id: String, val key: String, val echo: EchoFeature, val hub: EventHub)

    private fun startDevice(url: String = base(), id: String = "jvm-phone-" + (100000..999999).random()): Device {
        val hub = EventHub()
        val (router, echo) = testRouter(hub)
        val key = E2e.newKey()
        val logs = LinkedBlockingQueue<String>()
        val agent = RelayAgent(router, log = { _, m -> logs.add(m) })
        agent.configure(AgentConfig(url = url, token = TOKEN, id = id, name = "JVM phone", secret = E2e.b64e(E2e.randomBytes(32)),
                                    keyRaw = E2e.parseKey(key), version = "test", hostname = "jvm", platform = "Android test"))
        agent.start()
        waitFor("online") { agent.state == "online" }
        return Device(agent, id, key, echo, hub)
    }

    private fun waitFor(what: String, ms: Long = 20_000, cond: () -> Boolean) {
        val until = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < until) {
            if (cond()) return
            Thread.sleep(50)
        }
        fail("timed out waiting for $what")
    }

    class Run(val code: Int, val out: ByteArray, val err: String) {
        val text get() = String(out)
        override fun toString() = "exit $code, out ${text.take(300)}, err ${err.take(500)}"
    }

    /** The Python viewer client: python3 -m arstro_remote.relay.client <args>. */
    private fun client(dev: Device, transport: String, url: String = base(), key: String = dev.key, vararg args: String): Run {
        val pb = ProcessBuilder(listOf("python3", "-m", "arstro_remote.relay.client") + args).directory(File(repo, "server"))
        pb.environment().putAll(mapOf("ARSTRO_RELAY_URL" to url, "ARSTRO_RELAY_KEY" to key, "ARSTRO_RELAY_TRANSPORT" to transport,
                                      "PYTHONDONTWRITEBYTECODE" to "1"))
        val p = pb.start()
        val out = java.io.ByteArrayOutputStream()
        val errText = StringBuilder()
        val t1 = Thread { p.inputStream.copyTo(out) }.apply { start() }
        val t2 = Thread { errText.append(p.errorStream.bufferedReader().readText()) }.apply { start() }
        if (!p.waitFor(90, TimeUnit.SECONDS)) { p.destroyForcibly(); fail("the client hung") }
        t1.join(); t2.join()
        return Run(p.exitValue(), out.toByteArray(), errText.toString())
    }

    /** Everything the Python client can check, through one transport. */
    private fun clientChecks(dev: Device, transport: String, url: String = base()) {
        val check = client(dev, transport, url, dev.key, "check", dev.id)
        assertEquals("$check", 0, check.code)
        assertTrue("$check", check.text.contains("key ok: JVM phone (jvm)"))
        if (transport == "http") assertTrue("$check", check.text.contains("over HTTP"))

        val info = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/api/info")
        assertEquals("$info", 0, info.code)
        val j = JSONObject(info.text)
        assertEquals("Test phone", j.getString("name"))
        assertEquals(3, j.getJSONArray("features").length())

        val say = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/api/op/echo.say", "-X", "POST", "-d", "{\"x\":\"é\"}")
        assertEquals("$say", 0, say.code)
        assertEquals("é", JSONObject(say.text).getJSONObject("data").getString("x"))

        val off = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/api/op/off.do", "-X", "POST", "-d", "{}")
        assertEquals("$off", 1, off.code)
        assertEquals("Off thing is turned off on the phone", JSONObject(off.text).getString("error"))
        val needy = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/api/op/needy.do", "-X", "POST", "-d", "{}")
        assertEquals("Needy needs Display over other apps - allow it in the app", JSONObject(needy.text).getString("error"))
        val fail = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/api/op/echo.fail", "-X", "POST", "-d", "{}", "-i")
        assertTrue("$fail", fail.text.startsWith("HTTP 422"))

        val page = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/", "-i")
        assertEquals("$page", 0, page.code)
        assertTrue(page.text.contains("Content-Security-Policy: default-src 'self'"))
        assertTrue(page.text.contains("<title>Phone</title>"))

        // 3 MB down (flow control: the window is 512 KiB) and 1.5 MB up
        val n = 3 * 1024 * 1024 + 17
        val big = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/api/echo/big?n=$n")
        assertEquals("${big.code} ${big.err}", n, big.out.size)
        assertEquals(sha256(pattern(n)), sha256(big.out))
        val up = File.createTempFile("upload", ".bin")
        val upData = pattern(1_500_000)
        up.writeBytes(upData)
        val upr = client(dev, transport, url, dev.key, "http", dev.id, "8080", "/api/echo/upload", "-d", "@${up.path}", "-H", "Content-Type: application/octet-stream")
        up.delete()
        assertEquals("$upr", 0, upr.code)
        val uj = JSONObject(upr.text)
        assertEquals(upData.size, uj.getInt("size"))
        assertEquals(sha256(upData), uj.getString("sha256"))
        assertEquals(upData.size.toLong(), uj.getLong("length"))

        val ports = client(dev, transport, url, dev.key, "ports", dev.id, "--json")
        assertEquals("$ports", 0, ports.code)
        val pj = JSONArray(ports.text).getJSONObject(0)
        assertEquals(8080, pj.getInt("port"))
        assertEquals("in-app", pj.getString("addr"))
        assertEquals("Arstro", pj.getString("proc"))

        val other = client(dev, transport, url, dev.key, "http", dev.id, "8081", "/")
        assertEquals("$other", 2, other.code)
        assertTrue(other.err.contains("port 8081 is not allowed on this device"))
    }

    // ------------------------------------------------------------------ tests
    @Test fun overWebSockets() {
        Transports.mode = "ws"
        val dev = startDevice()
        try {
            assertEquals("ws", dev.agent.transport)
            clientChecks(dev, "ws")
            viewerChecks(dev)
            wrongKeys(dev, "ws")
        } finally {
            dev.agent.stop()
        }
        assertEquals("offline", dev.agent.state)
    }

    @Test fun overHttpPipes() {
        Transports.mode = "http"
        val dev = startDevice()
        try {
            assertEquals("http", dev.agent.transport)
            clientChecks(dev, "http")
            viewerChecks(dev)
        } finally {
            dev.agent.stop()
            Transports.mode = "auto"
        }
    }

    /** A proxy that answers every WebSocket handshake with 426 (like nginx without the Upgrade lines):
     *  the agent falls back to pipes by itself and remembers it. */
    @Test fun autoFallsBackToPipes() {
        Transports.mode = "auto"
        NoWebSocketProxy(port).use { proxy ->
            val url = base(proxy.port)
            val dev = startDevice(url)
            try {
                assertEquals("http", dev.agent.transport)
                assertTrue(proxy.refused > 0)
                val check = client(dev, "auto", url, dev.key, "check", dev.id)
                assertEquals("$check", 0, check.code)
                assertTrue("$check", check.text.contains("over HTTP"))
                val info = client(dev, "auto", url, dev.key, "http", dev.id, "8080", "/api/info")
                assertEquals("Test phone", JSONObject(info.text).getString("name"))
            } finally {
                dev.agent.stop()
            }
        }
    }

    @Test fun reconnectsAfterTheRelayDropsIt() {
        Transports.mode = "ws"
        val dev = startDevice()
        try {
            // a second control connection with the same id and secret replaces the first (bye) ...
            val cfg = dev.agent.cfg!!
            val twin = RelayAgent(testRouter().first)
            twin.configure(cfg)
            twin.start()
            waitFor("the twin online") { twin.state == "online" }
            waitFor("the first one notices") { dev.agent.state != "online" }
            twin.stop()
            // ... and the first one comes back by itself (back-off 1 s)
            waitFor("back online", 30_000) { dev.agent.state == "online" }
            assertEquals(0, client(dev, "ws", base(), dev.key, "check", dev.id).code)
        } finally {
            dev.agent.stop()
        }
    }

    @Test fun refusedWithAWrongJoinToken() {
        Transports.mode = "ws"
        val agent = RelayAgent(testRouter().first)
        agent.configure(AgentConfig(url = base(), token = "not-the-token-x7q", id = "jvm-phone-badtok", name = "x", secret = E2e.b64e(E2e.randomBytes(32)),
                                    keyRaw = E2e.randomBytes(32), version = "t", hostname = "h", platform = "p"))
        agent.start()
        try {
            waitFor("error") { agent.state == "error" }
            assertTrue(agent.error!!, agent.error!!.contains("refused this device"))
            assertTrue(!agent.error!!.contains("x7q"))         // the token is never shown
        } finally {
            agent.stop()
        }
    }

    private fun wrongKeys(dev: Device, transport: String) {
        val before = dev.agent.wrongTotal
        val bad = client(dev, transport, base(), E2e.newKey(), "check", dev.id)
        assertEquals("$bad", 3, bad.code)
        assertTrue(bad.err.contains("wrong access key"))
        waitFor("the wrong key counted") { dev.agent.wrongTotal == before + 1 }
        // more than 5 in a minute: new channels are refused for a minute, even with the right key
        repeat(5) { client(dev, transport, base(), E2e.newKey(), "check", dev.id) }
        waitFor("locked") { dev.agent.wrongTotal >= before + 6 }
        val locked = client(dev, transport, base(), dev.key, "check", dev.id)
        assertEquals("$locked", 2, locked.code)
        assertTrue(locked.err, locked.err.contains("too many wrong keys - wait a minute"))
    }

    /** The Kotlin viewer: ws streams (echo, fragments, close codes, refusals), tcp, ports, ping. */
    private fun viewerChecks(dev: Device) {
        // (a Python client that exited on an error left its pipe open: the relay keeps it until PIPE_IDLE)
        val before = dev.agent.viewers().size
        TestViewer(base(), dev.id, dev.key).use { v ->
            assertEquals("ready", v.ready.getString("t"))
            assertEquals(dev.id, v.ready.getString("id"))
            assertEquals("phone", v.ready.getString("slot"))
            assertEquals(8080, v.ready.getJSONArray("ports").getInt(0))
            waitFor("viewer listed") { dev.agent.viewers().size == before + 1 }

            // ping on stream 0
            v.send(0, E2e.K_JSON, "{\"t\":\"ping\"}".toByteArray())
            assertEquals("pong", v.json(0).getString("t"))

            // tcp is refused
            val t = v.open(JSONObject().put("t", "tcp").put("port", 8080))
            assertEquals("tcp is not available on a phone", v.json(t).getString("error"))

            // a WebSocket: hello, text echo, a 300 KB binary (several CHUNK frames), close from the phone
            val s = v.open(JSONObject().put("t", "ws").put("port", 8080).put("path", "/ws/echo?who=jvm")
                .put("headers", JSONArray()).put("protocols", JSONArray()))
            assertEquals("open", v.json(s).getString("t"))
            assertEquals("{\"hello\":\"jvm\"}", String(v.message(s).second))
            v.sendMessage(s, "héllo".toByteArray(), text = true)
            v.message(s).let { assertTrue(it.first); assertEquals("héllo", String(it.second)) }
            val blob = pattern(300_000)
            v.sendMessage(s, blob, text = false)
            v.message(s).let { assertTrue(!it.first); assertArrayEquals(blob, it.second) }
            v.sendMessage(s, "close".toByteArray(), text = true)
            val close = v.json(s)
            assertEquals("close", close.getString("t"))
            assertEquals(4001, close.getInt("code"))
            assertEquals("bye", close.getString("reason"))
            assertEquals(4001 to "bye", dev.echo.closes.poll(5, TimeUnit.SECONDS))

            // the viewer closes a socket: the feature hears it
            val s2 = v.open(JSONObject().put("t", "ws").put("port", 8080).put("path", "/ws/echo"))
            assertEquals("open", v.json(s2).getString("t"))
            v.message(s2)
            v.send(s2, E2e.K_JSON, JSONObject().put("t", "close").put("code", 1000).put("reason", "done").toString().toByteArray())
            assertEquals(1000 to "done", dev.echo.closes.poll(5, TimeUnit.SECONDS))

            // a socket of a feature that is off: opens and closes with 4403 and the reason
            val s3 = v.open(JSONObject().put("t", "ws").put("port", 8080).put("path", "/ws/off"))
            assertEquals("open", v.json(s3).getString("t"))
            v.json(s3).let {
                assertEquals("close", it.getString("t"))
                assertEquals(4403, it.getInt("code"))
                assertEquals("Off thing is turned off on the phone", it.getString("reason"))
            }

            // events: retained topics on connect
            dev.hub.publish("status", JSONObject().put("battery", 55))
            val e = v.open(JSONObject().put("t", "ws").put("port", 8080).put("path", "/ws"))
            assertEquals("open", v.json(e).getString("t"))
            val ev = JSONObject(String(v.message(e).second))
            assertEquals("state", ev.getString("ev"))
            assertEquals(55, ev.getJSONObject("data").getInt("battery"))

            // an unknown length body arrives whole (2 MB, uneven pieces)
            val h = v.open(JSONObject().put("t", "http").put("method", "GET").put("port", 8080)
                .put("path", "/api/echo/stream?n=2000000").put("headers", JSONArray()).put("body", false))
            val res = v.json(h)
            assertEquals(200, res.getInt("status"))
            assertEquals(sha256(pattern(2_000_000)), sha256(v.body(h)))

            // cancel: the stream goes, the channel stays
            val c = v.open(JSONObject().put("t", "http").put("method", "GET").put("port", 8080)
                .put("path", "/api/echo/big?n=5000000").put("headers", JSONArray()).put("body", false))
            assertEquals(200, v.json(c).getInt("status"))
            v.send(c, E2e.K_JSON, "{\"t\":\"cancel\"}".toByteArray())
            v.send(0, E2e.K_JSON, "{\"t\":\"ping\"}".toByteArray())
            assertEquals("pong", v.json(0).getString("t"))
        }
        waitFor("viewer gone") { dev.agent.viewers().size <= before }
    }

    // ------------------------------------------------------------------ a viewer in Kotlin
    class TestViewer(relayBase: String, deviceId: String, key: String) : AutoCloseable {
        private val conn = Transports.connect(Transports.wsBase(relayBase) + "/api/connect/$deviceId")
        private val secure: Secure
        val ready: JSONObject
        private val lock = Object()
        private var nextSid = 1L
        private val queues = HashMap<Long, LinkedBlockingQueue<Frame>>()
        private val unacked = HashMap<Long, Long>()

        init {
            val first = JSONObject(conn.recv(10_000).string())
            check(first.getBoolean("ok")) { "the relay refused: $first" }
            val (cn, hello) = E2e.viewerHello()
            conn.sendText(hello.toString())
            val (sec, proof) = E2e.viewerCheck(E2e.parseKey(key), cn, JSONObject(conn.recv(10_000).string()))
            secure = sec
            conn.sendText(proof.toString())
            val f = secure.open(conn.recv(10_000).data)
            ready = E2e.decodeJson(f.payload)
            Thread {
                try {
                    while (true) {
                        val fr = secure.open(conn.recv().data)
                        queue(fr.stream).add(fr)
                    }
                } catch (_: Exception) {}
            }.apply { isDaemon = true; start() }
        }

        private fun queue(sid: Long) = synchronized(lock) { queues.getOrPut(sid) { LinkedBlockingQueue() } }

        fun send(sid: Long, kind: Int, payload: ByteArray, flags: Int = 0) = synchronized(lock) {
            conn.sendBinary(secure.seal(kind, sid, payload, flags))
        }

        fun open(opener: JSONObject): Long {
            val sid = synchronized(lock) { nextSid++ }
            queue(sid)
            send(sid, E2e.K_JSON, opener.toString().toByteArray())
            return sid
        }

        fun next(sid: Long): Frame {
            val f = queue(sid).poll(20, TimeUnit.SECONDS) ?: throw AssertionError("no frame on stream $sid")
            if (f.kind == E2e.K_DATA) {
                val n = (unacked[sid] ?: 0L) + f.payload.size
                if (n >= E2e.WINDOW / 4) {
                    send(sid, E2e.K_JSON, "{\"t\":\"ack\",\"n\":$n}".toByteArray())
                    unacked[sid] = 0
                } else unacked[sid] = n
            }
            return f
        }

        fun json(sid: Long): JSONObject {
            while (true) {
                val f = next(sid)
                if (f.kind == E2e.K_JSON) {
                    val j = E2e.decodeJson(f.payload)
                    if (j.getString("t") != "ack") return j
                }
            }
        }

        /** One whole ws message -> (text?, bytes). */
        fun message(sid: Long): Pair<Boolean, ByteArray> {
            val out = java.io.ByteArrayOutputStream()
            var text = false
            var first = true
            while (true) {
                val f = next(sid)
                if (f.kind != E2e.K_DATA) {
                    val j = E2e.decodeJson(f.payload)
                    if (j.getString("t") == "ack") continue
                    fail("expected data, got $j")
                }
                if (first) { text = f.flags and E2e.F_TEXT != 0; first = false }
                out.write(f.payload)
                if (f.flags and E2e.F_FIN != 0) return text to out.toByteArray()
            }
        }

        /** An http response body up to `end`. */
        fun body(sid: Long): ByteArray {
            val out = java.io.ByteArrayOutputStream()
            while (true) {
                val f = next(sid)
                if (f.kind == E2e.K_DATA) { out.write(f.payload); continue }
                val j = E2e.decodeJson(f.payload)
                when (j.getString("t")) { "end" -> return out.toByteArray(); "ack" -> {}; else -> fail("unexpected $j") }
            }
        }

        /** A ws message in CHUNK frames (waits for acks like a real viewer would not need to here). */
        fun sendMessage(sid: Long, data: ByteArray, text: Boolean) {
            var i = 0
            val base = if (text) E2e.F_TEXT else 0
            do {
                val end = minOf(data.size, i + E2e.CHUNK)
                send(sid, E2e.K_DATA, data.copyOfRange(i, end), if (end >= data.size) base or E2e.F_FIN else base)
                i = end
            } while (i < data.size)
        }

        override fun close() = conn.close()
    }

    // ------------------------------------------------------------------ a proxy without WebSockets
    class NoWebSocketProxy(private val upstream: Int) : AutoCloseable {
        private val server = ServerSocket().apply { bind(InetSocketAddress("127.0.0.1", 0)) }
        val port: Int = server.localPort
        @Volatile var refused = 0

        init {
            Thread {
                while (!server.isClosed) {
                    val s = try { server.accept() } catch (_: Exception) { break }
                    Thread { serve(s) }.apply { isDaemon = true; start() }
                }
            }.apply { isDaemon = true; start() }
        }

        private fun serve(client: Socket) {
            try {
                val input = client.getInputStream()
                val head = java.io.ByteArrayOutputStream()
                while (!head.toString(Charsets.ISO_8859_1.name()).contains("\r\n\r\n")) {
                    val b = input.read()
                    if (b < 0) { client.close(); return }
                    head.write(b)
                }
                if (head.toString(Charsets.ISO_8859_1.name()).lowercase().contains("upgrade: websocket")) {
                    refused++
                    client.getOutputStream().write("HTTP/1.1 426 Upgrade Required\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".toByteArray())
                    client.close()
                    return
                }
                val up = Socket("127.0.0.1", upstream)
                up.getOutputStream().write(head.toByteArray())
                pump(input, up.getOutputStream(), up)
                pump(up.getInputStream(), client.getOutputStream(), client)
            } catch (_: Exception) {
                client.close()
            }
        }

        private fun pump(from: InputStream, to: OutputStream, closeAfter: Socket) {
            Thread {
                try { from.copyTo(to) } catch (_: Exception) {}
                try { closeAfter.shutdownOutput() } catch (_: Exception) {}
            }.apply { isDaemon = true; start() }
        }

        override fun close() = server.close()
    }
}
