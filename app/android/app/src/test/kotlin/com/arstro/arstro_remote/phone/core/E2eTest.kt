package com.arstro.arstro_remote.phone.core

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import java.io.IOException

/** The relay crypto and framing against docs/relay/vectors.json (generated from the Python e2e.py). */
class E2eTest {
    companion object {
        /** The repository root (the folder with docs/relay/vectors.json), found upwards from the module dir. */
        val repo: File by lazy {
            var d: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
            while (d != null && !File(d, "docs/relay/vectors.json").isFile) d = d.parentFile
            d ?: error("docs/relay/vectors.json not found above ${System.getProperty("user.dir")}")
        }
        val vectors: JSONObject by lazy { JSONObject(File(repo, "docs/relay/vectors.json").readText()) }
    }

    private fun b(s: String) = E2e.b64d(s)

    @Test fun constants() {
        assertEquals(vectors.getInt("version"), E2e.VERSION)
        assertEquals(vectors.getString("label"), String(E2e.LABEL))
        assertEquals(65536, E2e.CHUNK)
        assertEquals(524288, E2e.WINDOW)
        assertEquals(1048576, E2e.MAX_MESSAGE)
        assertEquals(262144, E2e.PIPE_MAX_UP)
        assertEquals(2 shl 20, E2e.PIPE_BUFFER)
    }

    @Test fun keyAndKeyId() {
        val raw = E2e.parseKey(vectors.getString("key"))
        assertArrayEquals(ByteArray(32) { it.toByte() }, raw)
        assertEquals(vectors.getString("key_id"), E2e.keyId(raw))
        // whitespace from copy-paste is ignored
        val spaced = vectors.getString("key").chunked(7).joinToString(" \n")
        assertArrayEquals(raw, E2e.parseKey(spaced))
        val k = E2e.newKey()
        assertEquals(47, k.length)
        assertTrue(k.startsWith("ak1_"))
        assertEquals(32, E2e.parseKey(k).size)
        for (bad in listOf("", "ak2_AAAA", "ak1_AAAA", "ak1_" + "A".repeat(44), "ak1_" + "*".repeat(43))) {
            try { E2e.parseKey(bad); fail("accepted $bad") } catch (_: IllegalArgumentException) {}
        }
    }

    @Test fun base64url() {
        for (n in 0..70) {
            val data = ByteArray(n) { (it * 37 + n).toByte() }
            val text = E2e.b64e(data)
            assertTrue(text.none { it == '=' || it == '+' || it == '/' })
            assertArrayEquals(data, E2e.b64d(text))
            assertArrayEquals(data, E2e.b64d(java.util.Base64.getEncoder().encodeToString(data)))
            assertEquals(java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(data), text)
        }
        try { E2e.b64d("ab!c"); fail() } catch (_: ProtocolError) {}
    }

    @Test fun keyDerivationAndProofs() {
        val raw = E2e.parseKey(vectors.getString("key"))
        val k = E2e.derive(raw, b(vectors.getString("cn")), b(vectors.getString("sn")))
        assertEquals(vectors.getString("viewer_to_device_key"), E2e.b64e(k.viewerToDevice))
        assertEquals(vectors.getString("device_to_viewer_key"), E2e.b64e(k.deviceToViewer))
        assertEquals(vectors.getString("proof_key"), E2e.b64e(k.proofKey))
        assertEquals(vectors.getString("proof_device"), E2e.b64e(E2e.proof(k.proofKey, "device")))
        assertEquals(vectors.getString("proof_viewer"), E2e.b64e(E2e.proof(k.proofKey, "viewer")))
    }

    @Test fun framesMatchTheVectors() {
        val raw = E2e.parseKey(vectors.getString("key"))
        val k = E2e.derive(raw, b(vectors.getString("cn")), b(vectors.getString("sn")))
        val viewer = Secure(k.viewerToDevice, k.deviceToViewer)
        val device = Secure(k.deviceToViewer, k.viewerToDevice)
        val frames = vectors.getJSONArray("frames")
        assertEquals(4, frames.length())
        for (i in 0 until frames.length()) {
            val f = frames.getJSONObject(i)
            val payload = b(f.getString("payload"))
            val sealer = if (f.getString("from") == "viewer") viewer else device
            val ct = sealer.seal(f.getInt("kind"), f.getLong("stream"), payload, f.getInt("flags"))
            assertEquals("frame $i", f.getString("ciphertext"), E2e.b64e(ct))
        }
        // and the other side opens them, in order
        val v2 = Secure(k.viewerToDevice, k.deviceToViewer)
        val d2 = Secure(k.deviceToViewer, k.viewerToDevice)
        for (i in 0 until frames.length()) {
            val f = frames.getJSONObject(i)
            val opener = if (f.getString("from") == "viewer") d2 else v2
            val fr = opener.open(b(f.getString("ciphertext")))
            assertEquals(f.getInt("kind"), fr.kind)
            assertEquals(f.getLong("stream"), fr.stream)
            assertEquals(f.getInt("flags"), fr.flags)
            assertArrayEquals(b(f.getString("payload")), fr.payload)
        }
    }

    @Test fun framingHeaderAndTampering() {
        val k = E2e.derive(ByteArray(32) { 7 }, ByteArray(32) { 1 }, ByteArray(32) { 2 })
        val a = Secure(k.viewerToDevice, k.deviceToViewer)
        val bSide = Secure(k.deviceToViewer, k.viewerToDevice)
        val big = ByteArray(E2e.CHUNK) { (it % 251).toByte() }
        val ct1 = a.seal(E2e.K_DATA, 0xFFFFFFFFL, big, E2e.F_FIN or E2e.F_TEXT)
        assertEquals(E2e.HEADER + big.size + 16, ct1.size)
        val f1 = bSide.open(ct1)
        assertEquals(0xFFFFFFFFL, f1.stream)
        assertEquals(3, f1.flags)
        assertArrayEquals(big, f1.payload)
        // a changed byte does not decrypt
        val ct2 = a.seal(E2e.K_JSON, 5, "{\"t\":\"end\"}".toByteArray())
        ct2[3] = (ct2[3].toInt() xor 1).toByte()
        try { bSide.open(ct2); fail("tampered frame accepted") } catch (_: ProtocolError) {}
        // reordered frames do not decrypt (implicit counter)
        val k2 = E2e.derive(ByteArray(32) { 9 }, ByteArray(32) { 1 }, ByteArray(32) { 2 })
        val s = Secure(k2.viewerToDevice, k2.deviceToViewer)
        val r = Secure(k2.deviceToViewer, k2.viewerToDevice)
        s.seal(E2e.K_JSON, 1, "{\"t\":\"a\"}".toByteArray())
        val second = s.seal(E2e.K_JSON, 1, "{\"t\":\"b\"}".toByteArray())
        try { r.open(second); fail("out-of-order frame accepted") } catch (_: ProtocolError) {}
        // JSON frames need an object with t
        try { E2e.decodeJson("[1]".toByteArray()); fail() } catch (_: ProtocolError) {}
        try { E2e.decodeJson("{\"x\":1}".toByteArray()); fail() } catch (_: ProtocolError) {}
        assertEquals("ping", E2e.decodeJson("{\"t\":\"ping\"}".toByteArray()).getString("t"))
    }

    @Test fun deviceHandshake() {
        val raw = E2e.parseKey(E2e.newKey())
        val (cn, hello) = E2e.viewerHello()
        assertEquals(32, cn.size)
        val (state, answer) = E2e.deviceAnswer(raw, hello)
        assertEquals("hello", answer.getString("t"))
        assertEquals(1, answer.getInt("v"))
        val (viewer, proof) = E2e.viewerCheck(raw, cn, answer)
        val device = E2e.deviceCheck(state, proof)
        val ct = device.seal(E2e.K_JSON, 0, "{\"t\":\"ready\"}".toByteArray())
        assertEquals("ready", E2e.decodeJson(viewer.open(ct).payload).getString("t"))
        val back = viewer.seal(E2e.K_DATA, 3, byteArrayOf(1, 2, 3))
        assertArrayEquals(byteArrayOf(1, 2, 3), device.open(back).payload)

        // a viewer with another key rejects the device's proof first
        val other = E2e.parseKey(E2e.newKey())
        val (cn2, hello2) = E2e.viewerHello()
        val (state2, answer2) = E2e.deviceAnswer(raw, hello2)
        try { E2e.viewerCheck(other, cn2, answer2); fail("wrong key accepted") } catch (_: WrongKey) {}
        // and a wrong proof is refused by the device
        try { E2e.deviceCheck(state2, JSONObject().put("t", "proof").put("proof", E2e.b64e(ByteArray(32)))); fail() } catch (_: WrongKey) {}
        // bad hellos
        try { E2e.deviceAnswer(raw, JSONObject().put("t", "hello").put("v", 2).put("cn", hello.getString("cn"))); fail() } catch (_: ProtocolError) {}
        try { E2e.deviceAnswer(raw, JSONObject().put("t", "hello").put("v", 1).put("cn", "AAAA")); fail() } catch (_: ProtocolError) {}
    }

    @Test fun pipeRecords() {
        val up = PipeCodec.encodeUp(listOf(PipeCodec.K_TEXT to "hi".toByteArray(), PipeCodec.K_BINARY to byteArrayOf(9),
                                           PipeCodec.K_CLOSE to PipeCodec.closePayload(1000, "bye")))
        assertArrayEquals(byteArrayOf(1, 0, 0, 0, 2, 'h'.code.toByte(), 'i'.code.toByte(), 2, 0, 0, 0, 1, 9,
                                      8, 0, 0, 0, 5, 0x03, 0xE8.toByte(), 'b'.code.toByte(), 'y'.code.toByte(), 'e'.code.toByte()), up)
        // down: kind | seq u32 | len u32 | payload
        val down = byteArrayOf(1, 0, 0, 0, 7, 0, 0, 0, 2, 'o'.code.toByte(), 'k'.code.toByte(),
                               8, 0, 0, 0, 8, 0, 0, 0, 2, 0x0F, 0xA0.toByte())
        val recs = PipeCodec.decodeDown(down)
        assertEquals(2, recs.size)
        assertEquals(1, recs[0].kind); assertEquals(7L, recs[0].seq); assertEquals("ok", String(recs[0].payload))
        assertEquals(8, recs[1].kind); assertEquals(8L, recs[1].seq)
        assertEquals(4000 to "", PipeCodec.parseClose(recs[1].payload))
        assertEquals(1005 to "", PipeCodec.parseClose(ByteArray(0)))
        assertEquals(0, PipeCodec.decodeDown(ByteArray(0)).size)
        for (cut in listOf(down.copyOf(5), down.copyOf(10))) {
            try { PipeCodec.decodeDown(cut); fail("cut record accepted") } catch (_: IOException) {}
        }
        // close codes that cannot be sent become 1000; long reasons are cut to 123 bytes
        assertEquals(1000, PipeCodec.parseClose(PipeCodec.closePayload(1006, "x")).first)
        assertEquals(4403, PipeCodec.parseClose(PipeCodec.closePayload(4403, "x")).first)
        assertTrue(PipeCodec.parseClose(PipeCodec.closePayload(1000, "é".repeat(100))).second.toByteArray().size <= 123)
    }

    @Test fun routeSplitting() {
        assertEquals("https://h.example/arstro/internal/devices" to "/api/agent",
                     Transports.split("wss://h.example/arstro/internal/devices/api/agent"))
        assertEquals("http://127.0.0.1:8090/b" to "/api/agent/data", Transports.split("ws://127.0.0.1:8090/b/api/agent/data"))
        assertEquals("http://127.0.0.1:8090" to "/api/connect/my-phone", Transports.split("http://127.0.0.1:8090/api/connect/my-phone"))
        assertEquals("wss://h.example/arstro/internal/devices", Transports.wsBase("https://h.example/arstro/internal/devices/"))
        try { Transports.split("ftp://x/api/agent"); fail() } catch (_: IllegalArgumentException) {}
        try { Transports.split("https://x/api/other"); fail() } catch (_: IllegalArgumentException) {}
    }

    @Test fun targetSplitting() {
        val (p, q) = splitTarget("/api/files/list?path=%2Fsdcard%2FMy+Files&x=1&x=2&flag")
        assertEquals("/api/files/list", p)
        assertEquals(listOf("/sdcard/My Files"), q["path"])
        assertEquals(listOf("1", "2"), q["x"])
        assertEquals(listOf(""), q["flag"])
        assertEquals("/static/a b.js", splitTarget("/static/a%20b.js").first)
        assertEquals("/a+b", splitTarget("/a+b").first)
    }
}
