package com.arstro.arstro_remote_client.core

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.util.concurrent.TimeUnit

/** The in-process router without the relay: static files, info, ops, routes, sockets, events. */
class RouterTest {
    private fun req(method: String, target: String, body: String? = null, headers: Map<String, String> = emptyMap()): Request {
        val (path, query) = splitTarget(target)
        val b = body?.toByteArray()
        return Request(method, path, query, headers, b?.let { ByteArrayInputStream(it) }, b?.size?.toLong() ?: 0)
    }

    private fun Response.text() = body?.readBytes()?.let { String(it) } ?: ""
    private fun Response.json() = JSONObject(text())
    private fun Response.header(name: String) = headers.firstOrNull { it.first.equals(name, true) }?.second

    @Test fun staticFiles() {
        val (r, _) = testRouter()
        val index = r.handle(req("GET", "/"))
        assertEquals(200, index.status)
        assertTrue(index.header("Content-Type")!!.startsWith("text/html"))
        assertEquals(Router.CSP, index.header("Content-Security-Policy"))
        assertEquals("no-cache", index.header("Cache-Control"))
        assertTrue(index.text().contains("<title>Phone</title>"))
        val js = r.handle(req("GET", "/static/app.js"))
        assertEquals(200, js.status)
        assertEquals("text/javascript; charset=utf-8", js.header("Content-Type"))
        assertNull(js.header("Content-Security-Policy"))
        assertEquals("text/css; charset=utf-8", r.handle(req("GET", "/static/style.css")).header("Content-Type"))
        for (bad in listOf("/static/../index.html", "/static/%2e%2e/x", "/static//etc/passwd", "/static/", "/static/nope.js")) {
            assertEquals(bad, 404, r.handle(req("GET", bad)).status)
        }
        assertEquals(404, r.handle(req("GET", "/nothing/here")).status)
    }

    /** The real web UI (client_app/android/app/src/main/assets/phone/): every file is served with its type. */
    @Test fun realAssets() {
        val dir = File(E2eTest.repo, "client_app/android/app/src/main/assets/phone")
        assertTrue("web UI not found at $dir", File(dir, "index.html").isFile)
        val gate = FakeGate()
        val r = Router(FakeContext(EventHub(), gate), { emptyList() }, gate,
                       AssetSource { p -> File(dir, p).takeIf { it.isFile }?.inputStream() }, { JSONObject() }, EventHub())
        val index = r.handle(req("GET", "/"))
        assertEquals(200, index.status)
        assertEquals(Router.CSP, index.header("Content-Security-Policy"))
        val static = File(dir, "static")
        val files = static.walkTopDown().filter { it.isFile }.toList()
        assertTrue(files.isNotEmpty())
        for (f in files) {
            val rel = f.relativeTo(static).invariantSeparatorsPath
            val res = r.handle(req("GET", "/static/$rel"))
            assertEquals(rel, 200, res.status)
            assertEquals(rel, f.length(), res.length)
            if (rel.endsWith(".js")) assertEquals(rel, "text/javascript; charset=utf-8", res.header("Content-Type"))
            if (rel.endsWith(".css")) assertEquals(rel, "text/css; charset=utf-8", res.header("Content-Type"))
        }
        // every static/... reference in index.html resolves
        Regex("(?:src|href)=\"(static/[^\"]+)\"").findAll(index.text()).forEach {
            assertEquals(it.groupValues[1], 200, r.handle(req("GET", "/" + it.groupValues[1])).status)
        }
    }

    @Test fun info() {
        val (r, _) = testRouter()
        val res = r.handle(req("GET", "/api/info"))
        assertEquals(200, res.status)
        val j = res.json()
        assertEquals("Test phone", j.getString("name"))
        val f = j.getJSONArray("features")
        assertEquals(3, f.length())
        val echo = f.getJSONObject(0)
        assertEquals("echo", echo.getString("id"))
        assertTrue(echo.getBoolean("on") && echo.getBoolean("ready"))
        assertEquals("all good", echo.getString("note"))
        val off = f.getJSONObject(1)
        assertTrue(!off.getBoolean("on") && !off.getBoolean("ready"))
        val needy = f.getJSONObject(2)
        assertTrue(needy.getBoolean("on") && !needy.getBoolean("ready"))
        assertEquals("Display over other apps", needy.getJSONArray("missing").getString(0))
    }

    @Test fun ops() {
        val (r, _) = testRouter()
        fun op(name: String, body: String? = "{}") = r.handle(req("POST", "/api/op/$name", body))
        op("echo.say", "{\"x\":1}").let {
            assertEquals(200, it.status)
            val j = it.json()
            assertTrue(j.getBoolean("ok"))
            assertEquals(1, j.getJSONObject("data").getInt("x"))
            assertTrue(j.getJSONObject("data").getBoolean("echoed"))
        }
        assertEquals("hello", op("echo.text").json().getString("data"))
        assertEquals(42, op("echo.number").json().getInt("data"))
        assertTrue(op("echo.null").json().isNull("data"))
        assertEquals("two", op("echo.list").json().getJSONArray("data").getString(1))
        assertEquals(200, op("echo.say", null).status)          // no body = {}
        assertEquals(200, op("echo.say", "").status)
        op("echo.say", "[1,2]").let { assertEquals(400, it.status); assertEquals("the body must be a JSON object", it.json().getString("error")) }
        op("echo.fail").let {
            assertEquals(422, it.status)
            val j = it.json()
            assertTrue(!j.getBoolean("ok"))
            assertEquals("that is not possible now", j.getString("error"))
        }
        op("echo.crash").let { assertEquals(500, it.status); assertEquals("echo.crash failed: boom", it.json().getString("error")) }
        op("off.do").let { assertEquals(403, it.status); assertEquals("Off thing is turned off on the phone", it.json().getString("error")) }
        op("needy.do").let {
            assertEquals(409, it.status)
            assertEquals("Needy needs Display over other apps - allow it in the app", it.json().getString("error"))
        }
        op("nope.nothing").let { assertEquals(404, it.status) }
        assertEquals(405, r.handle(req("GET", "/api/op/echo.say")).status)
        val list = op("features.list").json().getJSONArray("data")
        assertEquals(listOf("echo", "off", "needy"), (0 until list.length()).map { list.getJSONObject(it).getString("id") })
    }

    @Test fun routes() {
        val (r, _) = testRouter()
        val big = r.handle(req("GET", "/api/echo/big?n=1000"))
        assertEquals(1000L, big.length)
        assertEquals(sha256(pattern(1000)), sha256(big.body!!.readBytes()))
        val up = r.handle(req("POST", "/api/echo/upload?q=a%20b", "abc", mapOf("content-type" to "text/plain"))).json()
        assertEquals(3, up.getInt("size"))
        assertEquals("a b", up.getString("q"))
        assertEquals("text/plain", up.getString("type"))
        assertEquals(405, r.handle(req("PUT", "/api/echo/upload", "x")).status)
        assertEquals(409, r.handle(req("GET", "/api/needy")).status)
    }

    @Test fun socketsAndEvents() {
        val hub = EventHub()
        val (r, echo) = testRouter(hub)
        // events: retained topics on connect, then live
        hub.publish("status", JSONObject().put("battery", 80))
        r.publishFeatures()
        val ev = RecordingSession("/ws")
        val h = r.openSocket(ev)
        val first = (0 until 2).map { JSONObject(ev.texts.poll(1, TimeUnit.SECONDS)!!) }
        assertEquals(setOf("status", "features"), first.map { it.getString("topic") }.toSet())
        assertTrue(first.all { it.getString("ev") == "state" })
        assertEquals(80, first.first { it.getString("topic") == "status" }.getJSONObject("data").getInt("battery"))
        assertEquals(1, hub.watchers)
        r.handle(req("POST", "/api/op/echo.publish", "{\"a\":2}"))
        val pub = JSONObject(ev.texts.poll(1, TimeUnit.SECONDS)!!)
        assertEquals("echo", pub.getString("topic"))
        assertEquals(2, pub.getJSONObject("data").getInt("a"))
        val one = JSONObject(ev.texts.poll(1, TimeUnit.SECONDS)!!)
        assertEquals("ping", one.getString("ev"))
        assertEquals(1, one.getInt("n"))
        // a late viewer gets the newest retained value
        val ev2 = RecordingSession("/ws")
        r.openSocket(ev2)
        val got = (0 until 3).map { JSONObject(ev2.texts.poll(1, TimeUnit.SECONDS)!!) }
        assertEquals(2, got.first { it.getString("topic") == "echo" }.getJSONObject("data").getInt("a"))
        h.onClose(ev, 1000, "")
        assertEquals(1, hub.watchers)

        // feature sockets
        val s = RecordingSession("/ws/echo", mapOf("who" to listOf("me")))
        val eh = r.openSocket(s)
        assertEquals("{\"hello\":\"me\"}", s.texts.poll(1, TimeUnit.SECONDS))
        eh.onText(s, "hey")
        assertEquals("hey", s.texts.poll(1, TimeUnit.SECONDS))
        eh.onText(s, "close")
        assertEquals(4001 to "bye", s.closed)
        try { r.openSocket(RecordingSession("/ws/off")); fail() } catch (e: OpError) { assertEquals(403, e.status) }
        try { r.openSocket(RecordingSession("/ws/none")); fail() } catch (e: OpError) { assertEquals(404, e.status) }
        assertEquals(0, echo.started)
    }

    @Test fun responseJsonHelpers() {
        assertEquals("\"x\"", String(Response.json("x").body!!.readBytes()))
        assertEquals("null", String(Response.json(null).body!!.readBytes()))
        assertEquals("[1]", String(Response.json(JSONArray().put(1)).body!!.readBytes()))
    }
}
