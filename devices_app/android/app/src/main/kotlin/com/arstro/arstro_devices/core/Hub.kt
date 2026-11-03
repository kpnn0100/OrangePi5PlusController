package com.arstro.arstro_devices.core

import android.content.Context
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/** A refusal for the UI: `code` lets it react (ask for the key, the password), `message` is the sentence. */
class HubError(val code: String, message: String) : Exception(message)

/*
 * The app's one place for the device managers and the devices (MGR-01..06): the device list, the
 * access keys, one channel per device (opened on demand, closed when idle) and the local gateways
 * the WebView pages use. Everything here may block: the caller runs it off the main thread.
 */
object Hub {
    private const val TAG = "ArstroDevices"
    private const val IDLE_MS = 120_000L

    lateinit var store: Store
        private set
    private lateinit var tmp: File
    private val http: OkHttpClient by lazy {
        Transports.client.newBuilder().readTimeout(20, TimeUnit.SECONDS).pingInterval(0, TimeUnit.SECONDS).build()
    }
    private val channels = ConcurrentHashMap<String, Channel>()
    private val opening = ConcurrentHashMap<String, Any>()
    private class Open(val gateway: Gateway, var refs: Int)
    private val gateways = HashMap<String, Open>()

    @Synchronized fun init(context: Context) {
        if (::store.isInitialized) return
        store = Store(context.applicationContext)
        tmp = File(context.cacheDir, "uploads").apply { mkdirs(); listFiles()?.forEach { it.delete() } }
        Thread({ reap() }, "hub-reaper").apply { isDaemon = true; start() }
    }

    private fun server(id: String): JSONObject = store.server(id) ?: throw HubError("no_server", "that device manager is not in the app any more")

    // ------------------------------------------------------------------ device managers
    private fun getJson(url: String, password: String?): Pair<Int, JSONObject?> {
        val rb = Request.Builder().url(url).header("Accept", "application/json")
        if (!password.isNullOrEmpty()) rb.header("Authorization", "Bearer $password")
        http.newCall(rb.build()).execute().use { r ->
            val body = r.body?.string() ?: ""
            return r.code to (try { JSONObject(body) } catch (e: Exception) { null })
        }
    }

    /** What the user typed -> {url, name, version, auth, authorized} of the first candidate that is a
     *  device manager. Throws HubError("password") when it needs a (right) viewer password. */
    fun probe(input: String, password: String?): JSONObject {
        val candidates = try { Urls.candidates(input) } catch (e: IllegalArgumentException) {
            throw HubError("bad_input", e.message ?: "not an address")
        }
        var last = "no answer"
        for (base in candidates) {
            val info = try {
                val (code, j) = getJson("$base/api/info", null)
                if (code != 200 || j == null || !j.has("auth")) { last = "$base answered $code - not a device manager"; continue }
                j
            } catch (e: IOException) {
                last = "${Urls.label(base)}: ${describe(e)}"
                continue
            } catch (e: IllegalArgumentException) {
                last = e.message ?: "bad address"
                continue
            }
            if (info.optString("auth") == "password") {
                if (password.isNullOrEmpty()) throw HubError("password", "This device manager has a viewer password.")
                val (code, _) = try { getJson("$base/api/devices", password) } catch (e: IOException) {
                    throw HubError("unreachable", describe(e))
                }
                if (code == 401) throw HubError("password", "Wrong viewer password.")
            }
            return JSONObject().put("url", base).put("name", info.optString("name"))
                .put("version", info.optString("version")).put("auth", info.optString("auth"))
        }
        throw HubError("unreachable", "No device manager found: $last")
    }

    fun saveServer(id: String?, url: String, name: String, version: String, password: String?): String {
        val sid = id ?: ("s" + E2e.b64e(E2e.randomBytes(6)).lowercase().replace(Regex("[^a-z0-9]"), "x"))
        store.putServer(JSONObject().put("id", sid).put("url", url).put("name", name).put("version", version))
        if (password != null) store.setPassword(sid, password)
        if (store.current() == null || id == null) store.setCurrent(sid)
        if (id != null) closeServer(sid)                // the URL or password may have changed
        return sid
    }

    fun removeServer(id: String) {
        closeServer(id)
        store.removeServer(id)
    }

    fun servers(): JSONObject {
        val a = store.servers()
        for (i in 0 until a.length()) {
            val s = a.getJSONObject(i)
            s.put("hasPassword", store.password(s.optString("id")) != null).put("label", Urls.label(s.optString("url")))
        }
        return JSONObject().put("current", store.current() ?: JSONObject.NULL).put("servers", a)
    }

    fun select(id: String) { server(id); store.setCurrent(id) }

    /** The device list of a device manager, each with hasKey / keyId. */
    fun devices(sid: String): JSONObject {
        val s = server(sid)
        val (code, j) = try { getJson(s.optString("url") + "/api/devices", store.password(sid)) } catch (e: IOException) {
            throw HubError("unreachable", "Can't reach ${Urls.label(s.optString("url"))}: ${describe(e)}")
        }
        if (code == 401) throw HubError("password", "The device manager wants its viewer password.")
        val list = j?.optJSONArray("devices") ?: throw HubError("unreachable", "The device manager answered $code.")
        for (i in 0 until list.length()) {
            val d = list.getJSONObject(i)
            val id = d.optString("id")
            d.put("hasKey", store.keyId(sid, id) != null).put("keyId", store.keyId(sid, id) ?: JSONObject.NULL)
        }
        return JSONObject().put("devices", list).put("at", System.currentTimeMillis())
    }

    // ------------------------------------------------------------------ keys and channels
    fun setKey(sid: String, device: String, text: String): String {
        server(sid)
        val raw = try { E2e.parseKey(text) } catch (e: IllegalArgumentException) {
            throw HubError("bad_key", e.message ?: "not an access key")
        }
        val clean = text.filterNot { it.isWhitespace() }
        store.setKey(sid, device, clean)
        dropChannel(sid, device)
        return E2e.keyId(raw)
    }

    fun forgetKey(sid: String, device: String) {
        store.setKey(sid, device, null)
        dropChannel(sid, device)
    }

    private fun dropChannel(sid: String, device: String) {
        channels.remove("$sid/$device")?.close("the key changed")
    }

    private fun closeServer(sid: String) {
        for (k in channels.keys.filter { it.startsWith("$sid/") }) channels.remove(k)?.close("settings changed")
    }

    /** The device's channel: the open one, or a new one (one handshake at a time per device). */
    fun channel(sid: String, device: String): Channel {
        val k = "$sid/$device"
        channels[k]?.let { if (!it.closed) return it }
        synchronized(opening.getOrPut(k) { Any() }) {
            channels[k]?.let { if (!it.closed) return it }
            val s = server(sid)
            val key = store.key(sid, device) ?: throw HubError("no_key", "No access key for this device yet.")
            val ch = try {
                Channel.open(s.optString("url"), device, E2e.parseKey(key), store.password(sid))
            } catch (e: WrongKey) {
                throw HubError("wrong_key", "The access key is wrong for this device.")
            } catch (e: ViewerError) {
                val m = e.message ?: "unreachable"
                throw HubError(if ("password" in m) "password" else "refused", m)
            }
            channels[k] = ch
            Log.i(TAG, "channel to $device open over ${ch.transport}")
            return ch
        }
    }

    /** Opens a channel and returns the device's own description (MGR-02 "Check"). */
    fun check(sid: String, device: String): JSONObject {
        val ch = channel(sid, device)
        return JSONObject(ch.info.toString()).put("transport", ch.transport)
    }

    fun ports(sid: String, device: String): JSONArray = try {
        channel(sid, device).ports()
    } catch (e: ViewerError) {
        throw HubError("refused", e.message ?: "refused")
    }

    // ------------------------------------------------------------------ opening a port
    /** MGR-04/05: does the port answer HTTP? If it does, remember it as a recent port and return the
     *  local URL the WebView loads (the gateway is held until release()). */
    fun open(sid: String, device: String, name: String, port: Int): JSONObject {
        if (port !in 1..65535) throw HubError("bad_input", "A port is a number from 1 to 65535.")
        val res = try {
            channel(sid, device).probe(port)
        } catch (e: ViewerError) {
            throw HubError("refused", e.message ?: "refused")
        }
        val status = res.optInt("status")
        store.addRecent(sid, device, name, port, status)
        val g = acquire(sid, device, port)
        return JSONObject().put("url", g.url).put("host", g.host).put("status", status)
    }

    @Synchronized private fun acquire(sid: String, device: String, port: Int): Gateway {
        val k = "$sid/$device/$port"
        gateways[k]?.let { if (!it.gateway.stopped) { it.refs++; return it.gateway } }
        val (label, last) = store.origin(sid, device, port)
        var lastTouch = 0L
        val provider = {
            try { channel(sid, device) } catch (e: HubError) { throw ViewerError(e.message ?: "refused") }
        }
        val g = Gateway(label, port, provider, tmp, object : Gateway.Listener {
            override fun responded(status: Int) {
                val now = System.currentTimeMillis()
                if (now - lastTouch > 60_000) { lastTouch = now; store.touchRecent(sid, device, port) }
            }
            override fun failed(why: String) { Log.i(TAG, "port $port of $device: $why") }
        })
        val local = g.start(last)
        if (local != last) store.setOriginPort(sid, device, port, local)
        gateways[k] = Open(g, 1)
        return g
    }

    /** The page of that port was closed. */
    @Synchronized fun release(sid: String, device: String, port: Int) {
        val k = "$sid/$device/$port"
        val o = gateways[k] ?: return
        if (--o.refs <= 0) {
            o.gateway.stop()
            gateways.remove(k)
        }
    }

    @Synchronized private fun inUse(sid: String, device: String) = gateways.keys.any { it.startsWith("$sid/$device/") }

    /** A download of the page: GET through the channel -> (the response head, the stream with the body). */
    fun fetch(sid: String, device: String, port: Int, path: String, headers: List<Pair<String, String>>): Pair<JSONObject, Stream> {
        val hs = JSONArray()
        for ((k, v) in headers) hs.put(JSONArray().put(k).put(v))
        val s = channel(sid, device).stream(JSONObject().put("t", "http").put("method", "GET").put("port", port)
            .put("path", path).put("headers", hs).put("body", false))
        val e = s.next(60_000)
        if (e !is Stream.Event.Json || e.t != "res") {
            s.close()
            throw HubError("refused", why(e))
        }
        return e.msg to s
    }

    fun recents(sid: String): JSONArray = store.recents(sid)
    fun removeRecent(sid: String, device: String, port: Int) = store.removeRecent(sid, device, port)
    fun titleRecent(sid: String, device: String, port: Int, title: String) = store.titleRecent(sid, device, port, title)

    /** Channels nobody uses close after IDLE_MS (the device manager keeps a slot per channel). */
    private fun reap() {
        while (true) {
            try {
                Thread.sleep(30_000)
                val now = System.currentTimeMillis()
                for ((k, ch) in channels) {
                    val (sid, device) = k.split('/', limit = 2)
                    if (ch.closed || (ch.openStreams == 0 && now - ch.lastUse > IDLE_MS && !inUse(sid, device))) {
                        channels.remove(k, ch)
                        ch.close("idle")
                    }
                }
            } catch (_: InterruptedException) {
                return
            } catch (e: Exception) {
                Log.w(TAG, "reaper", e)
            }
        }
    }
}
