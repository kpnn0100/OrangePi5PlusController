package com.arstro.arstro_remote_client.core

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
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.InetAddress
import java.security.KeyStore
import java.security.SecureRandom
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/** The phone's own web server (PHN-20..33): login, sessions, the lockout, the router behind it,
 *  WebSockets, HTTPS with the phone's self-signed certificate. A real socket on a free port. */
class LocalServerTest {
    private var pw = "correct horse"
    private lateinit var server: LocalServer
    private lateinit var echo: EchoFeature
    private var port = 0
    private val loopback = InetAddress.getLoopbackAddress()
    private val http = OkHttpClient.Builder().followRedirects(false).readTimeout(10, TimeUnit.SECONDS).build()

    @Before fun up() {
        val (router, e) = testRouter()
        echo = e
        server = LocalServer(router, { pw })
        server.start(0, bind = loopback)
        port = server.ports.single()
    }

    @After fun down() = server.stop()

    private fun url(p: String) = "http://127.0.0.1:$port$p"
    private fun get(p: String, token: String? = null, cookie: String? = null): HttpResponse =
        http.newCall(HttpRequest.Builder().url(url(p)).apply {
            token?.let { header("Authorization", "Bearer $it") }
            cookie?.let { header("Cookie", it) }
        }.build()).execute()

    private fun post(p: String, body: String, type: String = "application/json", token: String? = null): HttpResponse =
        http.newCall(HttpRequest.Builder().url(url(p)).post(body.toRequestBody(type.toMediaType())).apply {
            token?.let { header("Authorization", "Bearer $it") }
        }.build()).execute()

    private fun login(): String = post("/api/login", JSONObject().put("password", pw).toString()).use {
        assertEquals(200, it.code)
        JSONObject(it.body!!.string()).getString("token")
    }

    // covers: PHN-21
    @Test fun everythingButTheLoginNeedsTheLogin() {
        get("/").use { assertEquals(303, it.code); assertEquals("login", it.header("Location")) }
        get("/api/info").use { assertEquals(401, it.code) }
        post("/api/op/echo.text", "{}").use { assertEquals(401, it.code) }
        get("/login").use {
            assertEquals(200, it.code)
            val html = it.body!!.string()
            assertTrue(html.contains("type=\"password\""))
            assertTrue(it.header("Content-Security-Policy")!!.contains("default-src 'none'"))
            assertTrue("plain http names the secure address", html.contains("8443"))
        }
        get("/api/ping").use { assertEquals(false, JSONObject(it.body!!.string()).getBoolean("login")) }
        post("/api/login", "{\"password\":\"nope\"}").use { assertEquals(401, it.code) }
        val t = login()
        get("/api/info", token = t).use { assertEquals("Test phone", JSONObject(it.body!!.string()).getString("name")) }
        post("/api/op/echo.say", "{\"a\":1}", token = t).use {
            val d = JSONObject(it.body!!.string()).getJSONObject("data")
            assertTrue(d.getBoolean("echoed"))
        }
        // the router's own refusals pass through
        post("/api/op/off.do", "{}", token = t).use { assertEquals(403, it.code) }
        get("/api/echo/big?n=300000", token = t).use { assertEquals(300000, it.body!!.bytes().size) }
        get("/api/echo/stream?n=200001", token = t).use {
            assertEquals("chunked", it.header("Transfer-Encoding"))
            assertEquals(200001, it.body!!.bytes().size)
        }
        post("/api/echo/upload?q=x", "a".repeat(100_000), "application/octet-stream", token = t).use {
            val d = JSONObject(it.body!!.string())
            assertEquals(100_000, d.getInt("size"))
            assertEquals("x", d.getString("q"))
        }
        // logged out: the token is gone
        post("/api/logout", "", token = t).use { assertEquals(200, it.code) }
        get("/api/info", token = t).use { assertEquals(401, it.code) }
    }

    // covers: PHN-21
    @Test fun theLoginFormSetsAStrictHttpOnlyCookie() {
        post("/login", "password=wrong", "application/x-www-form-urlencoded").use {
            assertEquals(401, it.code)
            assertTrue(it.body!!.string().contains("Wrong password"))
        }
        val r = post("/login", "password=" + java.net.URLEncoder.encode(pw, "UTF-8"), "application/x-www-form-urlencoded")
        r.use {
            assertEquals(303, it.code)
            assertEquals("./", it.header("Location"))
        }
        val set = r.header("Set-Cookie")!!
        assertTrue(set, set.contains("HttpOnly") && set.contains("SameSite=Strict") && !set.contains("Secure"))
        val cookie = set.substringBefore(';')
        get("/", cookie = cookie).use { assertEquals(200, it.code); assertTrue(it.body!!.string().contains("<title>Phone</title>")) }
        get("/login", cookie = cookie).use { assertEquals(303, it.code) }
        // a new password logs everybody out
        server.dropSessions()
        get("/", cookie = cookie).use { assertEquals(303, it.code) }
    }

    // covers: PHN-21
    @Test fun wrongPasswordsAreSlowedDown() {
        repeat(5) { post("/api/login", "{\"password\":\"x$it\"}").use { r -> assertEquals(401, r.code) } }
        post("/api/login", JSONObject().put("password", pw).toString()).use {
            assertEquals("even the right one waits", 429, it.code)
            assertNotNull(it.header("Retry-After"))
            assertTrue(JSONObject(it.body!!.string()).getString("error").contains("Too many"))
        }
    }

    // covers: PHN-21
    @Test fun noPasswordNoLogin() {
        pw = ""
        post("/api/login", "{\"password\":\"\"}").use {
            assertEquals(401, it.code)
            assertTrue(JSONObject(it.body!!.string()).getString("error").contains("No password"))
        }
    }

    private class Recorder : WebSocketListener() {
        val got = LinkedBlockingQueue<Any>()
        override fun onMessage(webSocket: WebSocket, text: String) { got.add(text) }
        override fun onMessage(webSocket: WebSocket, bytes: ByteString) { got.add(bytes) }
        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { got.add("closing $code $reason"); webSocket.close(1000, null) }
        override fun onFailure(webSocket: WebSocket, t: Throwable, response: HttpResponse?) { got.add("failure ${response?.code}") }
        fun next(): Any = got.poll(5, TimeUnit.SECONDS) ?: "timeout"
    }

    private fun ws(path: String, token: String?, origin: String? = null): Recorder {
        val rec = Recorder()
        http.newWebSocket(HttpRequest.Builder().url("ws://127.0.0.1:$port$path").apply {
            token?.let { header("Authorization", "Bearer $it") }
            origin?.let { header("Origin", it) }
        }.build(), rec)
        return rec
    }

    // covers: PHN-20, PHN-21
    @Test fun webSocketsReachTheFeatures() {
        assertEquals("failure 401", ws("/ws/echo", null).next())
        val t = login()
        assertEquals("failure 403", ws("/ws/echo", t, origin = "http://evil.example").next())
        val rec = ws("/ws/echo?who=me", t, origin = "http://127.0.0.1:$port")
        assertEquals("{\"hello\":\"me\"}", rec.next())
        assertEquals(1, server.viewers)
        val big = ByteArray(200_000) { it.toByte() }
        // through a second socket: the binary echo, a big one (64-bit length frames)
        val sock = LinkedBlockingQueue<WebSocket>()
        val rec2 = Recorder()
        sock.add(http.newWebSocket(HttpRequest.Builder().url("ws://127.0.0.1:$port/ws/echo").header("Authorization", "Bearer $t").build(), rec2))
        rec2.next()
        val w = sock.take()
        w.send("text one")
        assertEquals("text one", rec2.next())
        w.send(big.toByteString())
        assertEquals(big.toByteString(), rec2.next())
        w.send("close")
        assertEquals("closing 4001 bye", rec2.next())
        // refused sockets close at once with 4000 + status
        assertEquals("closing 4403 Off thing is turned off on the phone", ws("/ws/off", t).next())
        assertEquals("closing 4404 nothing at /ws/nope", ws("/ws/nope", t).next())
        // a new password closes the open sockets
        server.dropSessions()
        assertEquals("closing 4401 logged out", rec.next())
        val deadline = System.currentTimeMillis() + 3000
        while (server.viewers > 0 && System.currentTimeMillis() < deadline) Thread.sleep(20)
        assertEquals(0, server.viewers)
    }

    // covers: PHN-22
    @Test fun httpsWithThePhonesOwnCertificate() {
        val id = SelfSigned.create("Test phone")
        assertEquals("CN=Test phone", id.cert.subjectX500Principal.name)
        id.cert.verify(id.cert.publicKey)
        assertEquals(32, id.fingerprint.split(':').size)
        // kept: the same bytes give the same certificate
        assertEquals(id.fingerprint, SelfSigned.Identity(id.keyPkcs8, id.certDer).fingerprint)
        server.stop()
        server.start(0, 0, id.serverSocketFactory(), loopback)
        val (plain, tls) = server.ports
        val ks = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null, null); setCertificateEntry("phone", id.cert) }
        val tm = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(ks) }.trustManagers[0] as X509TrustManager
        val ctx = SSLContext.getInstance("TLS").apply { init(null, arrayOf(tm), SecureRandom()) }
        val https = http.newBuilder().sslSocketFactory(ctx.socketFactory, tm).hostnameVerifier { _, _ -> true }.build()
        https.newCall(HttpRequest.Builder().url("https://127.0.0.1:$tls/login").build()).execute().use {
            assertEquals(200, it.code)
            assertFalse("no https hint on https", it.body!!.string().contains("8443"))
        }
        val r = https.newCall(HttpRequest.Builder().url("https://127.0.0.1:$tls/login")
            .post("password=correct+horse".toRequestBody("application/x-www-form-urlencoded".toMediaType())).build()).execute()
        r.use { assertTrue(it.header("Set-Cookie")!!.contains("; Secure")) }
        http.newCall(HttpRequest.Builder().url("http://127.0.0.1:$plain/api/ping").build()).execute().use { assertEquals(200, it.code) }
    }
}
