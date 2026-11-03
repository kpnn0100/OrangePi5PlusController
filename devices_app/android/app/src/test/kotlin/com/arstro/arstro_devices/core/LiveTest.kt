package com.arstro.arstro_devices.core

import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/**
 * Opt-in: a gateway to a REAL device for a while, to drive it with a browser (Chromium resolves
 * *.localhost like the WebView). Env: ARSTRO_LIVE_RELAY (device manager URL), ARSTRO_LIVE_ID,
 * ARSTRO_LIVE_KEY_FILE, ARSTRO_LIVE_PORT, ARSTRO_LIVE_OUT (file that gets the gateway URL),
 * ARSTRO_LIVE_SECONDS (default 120).
 */
class LiveTest {
    @Test fun gatewayToARealDevice() {
        val relay = System.getenv("ARSTRO_LIVE_RELAY")
        assumeTrue("set ARSTRO_LIVE_RELAY to run it", !relay.isNullOrEmpty())
        val key = E2e.parseKey(File(System.getenv("ARSTRO_LIVE_KEY_FILE")).readText())
        val id = System.getenv("ARSTRO_LIVE_ID")
        var ch = Channel.open(relay!!, id, key)
        println("channel over ${ch.transport}")
        val g = Gateway(Gateway.newLabel(), System.getenv("ARSTRO_LIVE_PORT").toInt(), {
            synchronized(this) { if (ch.closed) ch = Channel.open(relay, id, key); ch }
        }, null, object : Gateway.Listener {
            override fun responded(status: Int) { println("responded $status (streams ${ch.openStreams})") }
            override fun failed(why: String) { println("failed: $why") }
        })
        g.start()
        File(System.getenv("ARSTRO_LIVE_OUT")).writeText(g.url)
        Thread.sleep((System.getenv("ARSTRO_LIVE_SECONDS") ?: "120").toLong() * 1000)
        println("streams still open at the end: ${ch.openStreams}")
        g.stop()
        ch.close()
    }
}
