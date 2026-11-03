package com.arstro.arstro_devices.core

import okhttp3.Dns
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.BufferedSink
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import org.junit.AfterClass
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.BeforeClass
import org.junit.Test
import java.io.File
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.nio.file.Files
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * The viewer channel and the local gateway (MGR-03, MGR-04) against the REAL device manager
 * (`python3 -m arstro_relay`) and a REAL device (the Python relay agent with a small web app,
 * devices_app/tool/fake_device.py), over WebSockets and over HTTP pipes (the transport the
 * production device manager needs, its proxy passes no WebSockets).
 */
class GatewayTest {
    companion object {
        private const val TOKEN = "join-token-for-tests"
        private lateinit var relay: Process
        private lateinit var device: Process
        private var relayPort = 0
        private lateinit var data: File
        private lateinit var log: File
        lateinit var info: JSONObject
        private val repo get() = E2eTest.repo

        fun base() = "http://127.0.0.1:$relayPort/arstro/internal/devices"

        @BeforeClass @JvmStatic fun start() {
            relayPort = ServerSocket(0).use { it.localPort }
            data = Files.createTempDirectory("relay-data").toFile()
            log = File.createTempFile("relay", ".log")
            val pb = ProcessBuilder("python3", "-m", "arstro_relay").directory(File(repo, "relay"))
                .redirectErrorStream(true).redirectOutput(log)
            pb.environment().keys.removeIf { it.startsWith("RELAY_") }
            pb.environment().putAll(mapOf("RELAY_HOST" to "127.0.0.1", "RELAY_PORT" to "$relayPort", "RELAY_DATA" to data.path,
                                          "RELAY_JOIN_TOKEN" to TOKEN, "RELAY_RATE_LIMIT" to "100000/10",
                                          "PYTHONDONTWRITEBYTECODE" to "1"))
            relay = pb.start()
            val until = System.currentTimeMillis() + 15_000
            while (true) {
                if (!relay.isAlive) error("the relay did not start: ${log.readText()}")
                try {
                    if ((URL(base() + "/api/health").openConnection() as HttpURLConnection).responseCode == 200) break
                } catch (_: Exception) {}
                if (System.currentTimeMillis() > until) error("the relay did not answer: ${log.readText()}")
                Thread.sleep(100)
            }
            val dp = ProcessBuilder("python3", File(repo, "devices_app/tool/fake_device.py").path, "--relay", base(),
                                    "--token", TOKEN, "--id", "fake-jvm-" + (10000..99999).random())
            dp.environment()["PYTHONDONTWRITEBYTECODE"] = "1"
            device = dp.redirectError(ProcessBuilder.Redirect.INHERIT).start()
            val line = device.inputStream.bufferedReader().readLine() ?: error("the fake device did not start")
            info = JSONObject(line)
            if (info.has("error")) error("fake device: ${info.getString("error")}")
        }

        @AfterClass @JvmStatic fun stop() {
            device.destroy()
            relay.destroy()
            device.waitFor(5, TimeUnit.SECONDS)
            relay.waitFor(5, TimeUnit.SECONDS)
            data.deleteRecursively()
            log.delete()
            Transports.mode = "auto"
        }
    }

    private val id get() = info.getString("id")
    private val port get() = info.getInt("port")
    private fun key() = E2e.parseKey(info.getString("key"))

    /** Resolves *.localhost to 127.0.0.1, like Chromium (the WebView) does. */
    private val client = OkHttpClient.Builder()
        .dns(object : Dns {
            override fun lookup(hostname: String): List<InetAddress> =
                if (hostname.endsWith(".localhost")) listOf(InetAddress.getByName("127.0.0.1")) else Dns.SYSTEM.lookup(hostname)
        })
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private fun channel(transport: String): Channel {
        Transports.mode = transport
        return Channel.open(base(), id, key()).also { if (transport != "auto") assertEquals(transport, it.transport) }
    }

    private class Seen : Gateway.Listener {
        val statuses = CopyOnWriteArrayList<Int>()
        val failures = CopyOnWriteArrayList<String>()
        override fun responded(status: Int) { statuses.add(status) }
        override fun failed(why: String) { failures.add(why) }
    }

    private fun get(g: Gateway, path: String, headers: Map<String, String> = emptyMap()): Response {
        val rb = Request.Builder().url(g.url.trimEnd('/') + path)
        for ((k, v) in headers) rb.header(k, v)
        return client.newCall(rb.build()).execute()
    }

    // covers: MGR-03

    @Test fun viewerOverWebSocketAndPipes() {
        for (t in listOf("ws", "http")) {
            val ch = channel(t)
            try {
                assertEquals("Fake device", ch.info.getString("name"))
                val ports = ch.ports()
                assertTrue("$t: ports $ports", (0 until ports.length()).any { ports.getJSONObject(it).getInt("port") == port })
                assertEquals(200, ch.probe(port).getInt("status"))
                assertEquals(404, ch.probe(port, "/nothing-here").getInt("status"))
                try {
                    ch.probe(info.getInt("closed_port"))
                    fail("$t: a closed port answered")
                } catch (e: ViewerError) {
                    assertTrue("$t: ${e.message}", "nothing listens" in (e.message ?: ""))
                }
                assertEquals(0, ch.openStreams)
            } finally {
                ch.close()
            }
        }
    }

    // covers: MGR-03

    @Test fun wrongKeyIsReportedBeforeAnythingIsSent() {
        Transports.mode = "auto"
        try {
            Channel.open(base(), id, E2e.parseKey(E2e.newKey()))
            fail("a wrong key opened a channel")
        } catch (e: WrongKey) {
            assertEquals("wrong access key for this device", e.message)
        }
        try {
            Channel.open(base(), "no-such-device", key())
            fail("an unknown device opened a channel")
        } catch (e: ViewerError) {
            assertTrue(e.message, e.message!!.isNotEmpty())
        }
    }

    // covers: MGR-04

    @Test fun gatewayServesPagesBodiesCookiesAndHeaders() {
        for (t in listOf("ws", "http")) {
            val ch = channel(t)
            val seen = Seen()
            val g = Gateway(Gateway.newLabel(), port, { ch }, null, seen)
            g.start()
            try {
                get(g, "/").use { r ->
                    assertEquals(200, r.code)
                    assertTrue(r.body!!.string().contains("<title>Fake device</title>"))
                }
                assertTrue(seen.statuses.contains(200))
                // uploads: a known length, and chunked (the gateway gives the device a length)
                val up = ByteArray(3 shl 20) { (it * 7).toByte() }
                client.newCall(Request.Builder().url(g.url + "echo").post(up.toRequestBody("application/octet-stream".toMediaType())).build())
                    .execute().use { r ->
                        assertEquals("$t upload", 200, r.code)
                        assertEquals(up.size.toString(), r.header("X-Got"))
                        assertArrayEquals(up, r.body!!.bytes())
                    }
                val chunked = object : RequestBody() {
                    override fun contentType() = "application/octet-stream".toMediaType()
                    override fun contentLength() = -1L
                    override fun writeTo(sink: BufferedSink) { sink.write(up, 0, 300_000) }
                }
                client.newCall(Request.Builder().url(g.url + "echo").post(chunked).build()).execute().use { r ->
                    assertEquals(200, r.code)
                    assertEquals("300000", r.header("X-Got"))
                }
                // a body without a length comes out chunked, byte-exact
                get(g, "/big?n=5000000").use { r ->
                    assertEquals(200, r.code)
                    val b = r.body!!.bytes()
                    assertEquals(5_000_000, b.size)
                    for (i in b.indices step 997) assertEquals((i % 256).toByte(), b[i])
                    assertEquals((4_999_999 % 256).toByte(), b[4_999_999])
                }
                // cookies go to the device as they are; Host and Origin are the device's own (PROTOCOL 4.4)
                get(g, "/cookie/show", mapOf("Cookie" to "fake=42")).use { assertEquals("fake=42", it.body!!.string()) }
                get(g, "/headers", mapOf("Origin" to "http://${g.host}")).use { r ->
                    val h = JSONObject(r.body!!.string())
                    assertEquals("127.0.0.1:$port", h.getString("host"))
                    assertEquals("http://127.0.0.1:$port", h.getString("origin"))
                    assertEquals("identity", h.getString("accept-encoding"))
                }
                // a port where nothing listens: a page that says so
                val other = Gateway(Gateway.newLabel(), info.getInt("closed_port"), { ch }, null, seen)
                other.start()
                try {
                    get(other, "/").use { r ->
                        assertEquals(502, r.code)
                        assertTrue(r.body!!.string().contains("nothing listens"))
                    }
                } finally {
                    other.stop()
                }
                assertTrue(seen.failures.any { "nothing listens" in it })
            } finally {
                g.stop()
                ch.close()
            }
        }
    }

    // covers: MGR-03, MGR-04

    @Test fun manyRequestsAtOnce() {
        // a page loads its scripts in parallel: every request must be answered (stream ids go out in order)
        for (t in listOf("ws", "http")) {
            val ch = channel(t)
            val g = Gateway(Gateway.newLabel(), port, { ch }, null)
            g.start()
            try {
                val pool = java.util.concurrent.Executors.newFixedThreadPool(12)
                val jobs = (1..60).map { i ->
                    pool.submit<Int> { get(g, if (i % 3 == 0) "/big?n=${20_000 + i}" else "/headers").use { r -> r.body!!.bytes(); r.code } }
                }
                for (j in jobs) assertEquals("$t: a parallel request", 200, j.get(60, TimeUnit.SECONDS))
                pool.shutdown()
            } finally {
                g.stop()
                ch.close()
            }
        }
    }

    // covers: MGR-04

    @Test fun gatewayRefusesOtherHostNames() {
        val ch = channel("auto")
        val g = Gateway(Gateway.newLabel(), port, { ch }, null)
        g.start()
        try {
            for (hostHeader in listOf("127.0.0.1:${g.localPort}", "localhost:${g.localPort}", "dother.localhost:${g.localPort}", "")) {
                Socket("127.0.0.1", g.localPort).use { s ->
                    s.soTimeout = 10_000
                    val head = "GET / HTTP/1.1\r\n" + (if (hostHeader.isEmpty()) "" else "Host: $hostHeader\r\n") + "\r\n"
                    s.getOutputStream().write(head.toByteArray())
                    val answer = s.getInputStream().bufferedReader().readLine()
                    assertEquals("Host '$hostHeader'", "HTTP/1.1 403 Forbidden", answer)
                }
            }
            get(g, "/").use { assertEquals(200, it.code) }
        } finally {
            g.stop()
            ch.close()
        }
    }

    // covers: MGR-04

    @Test fun gatewayWebSockets() {
        for (t in listOf("ws", "http")) {
            val ch = channel(t)
            val g = Gateway(Gateway.newLabel(), port, { ch }, null)
            g.start()
            val events = LinkedBlockingQueue<Any>()
            try {
                val ws = client.newWebSocket(Request.Builder().url("ws://${g.host}/ws").build(), object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) { events.add("open") }
                    override fun onMessage(webSocket: WebSocket, text: String) { events.add(text) }
                    override fun onMessage(webSocket: WebSocket, bytes: ByteString) { events.add(bytes) }
                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { events.add("closing $code $reason"); webSocket.close(code, null) }
                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { events.add("failure ${t.message}") }
                })
                assertEquals("$t open", "open", events.poll(20, TimeUnit.SECONDS))
                ws.send("hello")
                assertEquals("hello", events.poll(20, TimeUnit.SECONDS))
                val big = ByteArray(300_000) { (it % 251).toByte() }            // more than one DATA frame each way
                ws.send(big.toByteString())
                assertEquals(big.toByteString(), events.poll(20, TimeUnit.SECONDS))
                ws.send("é".repeat(70_000))
                assertEquals("é".repeat(70_000), events.poll(20, TimeUnit.SECONDS))
                // the device closes it: the code and reason reach the page
                ws.send("close-me")
                assertEquals("closing 4001 asked to close", events.poll(20, TimeUnit.SECONDS))
                // the page closes one: the stream ends on both sides
                val ws2 = client.newWebSocket(Request.Builder().url("ws://${g.host}/ws").build(), object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) { events.add("open2") }
                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { events.add("closed2 $code") }
                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { events.add("failure2 ${t.message}") }
                })
                var e = events.poll(20, TimeUnit.SECONDS)
                while (e != null && e != "open2") e = events.poll(20, TimeUnit.SECONDS)
                assertEquals("open2", e)
                ws2.close(1000, "bye")
                val until = System.currentTimeMillis() + 10_000
                while (ch.openStreams > 0 && System.currentTimeMillis() < until) Thread.sleep(50)
                assertEquals("$t: streams left open", 0, ch.openStreams)
            } finally {
                g.stop()
                ch.close()
            }
        }
    }

    // covers: MGR-03

    @Test fun gatewayReopensALostChannel() {
        Transports.mode = "auto"
        var ch = Channel.open(base(), id, key())
        val g = Gateway(Gateway.newLabel(), port, {
            synchronized(this) { if (ch.closed) ch = Channel.open(base(), id, key()); ch }
        }, null)
        g.start()
        try {
            get(g, "/").use { assertEquals(200, it.code) }
            ch.close("test: the network went away")
            get(g, "/").use { assertEquals(200, it.code) }
            assertTrue(!ch.closed)
        } finally {
            g.stop()
            ch.close()
        }
    }

    // covers: MGR-06

    @Test fun copiesOfTheSharedCodeAreTheSame() {
        // E2e.kt and Transport.kt are client_app's (the device side): only the package line differs
        for (f in listOf("E2e.kt", "Transport.kt")) {
            val a = File(repo, "client_app/android/app/src/main/kotlin/com/arstro/arstro_remote_client/core/$f").readLines().drop(1)
            val b = File(repo, "devices_app/android/app/src/main/kotlin/com/arstro/arstro_devices/core/$f").readLines().drop(1)
            assertEquals("$f differs from client_app's copy - change both", a, b)
        }
    }

    // covers: MGR-01

    @Test fun addressesTheUserTypes() {
        assertEquals(listOf("https://arstro.example.com/arstro/internal/devices", "https://arstro.example.com",
                            "http://arstro.example.com/arstro/internal/devices", "http://arstro.example.com"),
                     Urls.candidates(" arstro.example.com "))
        assertEquals(listOf("http://192.0.2.10:8090/arstro/internal/devices", "http://192.0.2.10:8090",
                            "https://192.0.2.10:8090/arstro/internal/devices", "https://192.0.2.10:8090"),
                     Urls.candidates("192.0.2.10:8090"))
        assertEquals(listOf("https://h.example/arstro/internal/devices"),
                     Urls.candidates("https://h.example/arstro/internal/devices/d/pi-a-1/8080/files"))
        assertEquals(listOf("https://h.example/devices"), Urls.candidates("https://h.example/devices/"))
        assertEquals("h.example", Urls.label("https://h.example/arstro/internal/devices"))
        for (bad in listOf("", "ftp://x.example", "https://")) {
            try { Urls.candidates(bad); fail("'$bad' was taken") } catch (_: IllegalArgumentException) {}
        }
    }
}
