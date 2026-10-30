package com.arstro.arstro_remote.phone.features

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** PHONE.md "Video": Annex-B parsing, the codec string, the frame header, encoded sizes. */
class VideoTest {
    private fun bytes(vararg v: Int) = ByteArray(v.size) { v[it].toByte() }

    // SPS (High profile 0x64, constraints 0x00, level 3.1 = 0x1F) and PPS with 4-byte start codes, then an IDR with a 3-byte one
    private val config = bytes(0, 0, 0, 1, 0x67, 0x64, 0x00, 0x1F, 0xAC, 0xD9, 0, 0, 0, 1, 0x68, 0xEB, 0xE3, 0xCB)
    private val idr = bytes(0, 0, 1, 0x65, 0x88, 0x84, 0x00)

    @Test fun findsNalUnits() {
        assertEquals(listOf(4, 14), H264.nalOffsets(config))
        assertEquals(listOf(H264.NAL_SPS, H264.NAL_PPS), H264.nalTypes(config))
        assertEquals(listOf(H264.NAL_IDR), H264.nalTypes(idr))
        assertEquals(listOf(H264.NAL_SPS, H264.NAL_PPS, H264.NAL_IDR), H264.nalTypes(config + idr))
        assertTrue(H264.hasSps(config))
        assertFalse(H264.hasSps(idr))
        assertTrue(H264.nalTypes(ByteArray(0)).isEmpty())
    }

    @Test fun codecStringFromTheSps() {
        assertEquals("avc1.64001F", H264.codecString(config))
        assertEquals("avc1.42C01E", H264.codecString(bytes(0, 0, 1, 0x27, 0x42, 0xC0, 0x1E, 0x95)))
        assertNull(H264.codecString(idr))
        assertNull(H264.codecString(bytes(0, 0, 1, 0x67, 0x42)))   // cut off
    }

    @Test fun framePacket() {
        val p = H264.packet(true, 0x0102030405060708L, idr, config)
        assertEquals(9 + config.size + idr.size, p.size)
        assertEquals(1, p[0].toInt())
        assertArrayEquals(bytes(1, 2, 3, 4, 5, 6, 7, 8), p.copyOfRange(1, 9))
        assertArrayEquals(config + idr, p.copyOfRange(9, p.size))
        val q = H264.packet(false, 33_333L, idr)
        assertEquals(0, q[0].toInt())
        assertArrayEquals(bytes(0, 0, 0, 0, 0, 0, 0x82, 0x35), q.copyOfRange(1, 9))
        assertArrayEquals(idr, q.copyOfRange(9, q.size))
    }

    @Test fun encodedSizes() {
        assertEquals(324 to 720, VideoSizes.fit(1080, 2400, 720))
        assertEquals(720 to 324, VideoSizes.fit(2400, 1080, 720))
        assertEquals(1080 to 2400, VideoSizes.fit(1080, 2400, 4096))   // never upscaled
        assertEquals(1080 to 2400, VideoSizes.fit(1080, 2400, 0))      // 0 = as is
        val (w, h) = VideoSizes.fit(1079, 2341, 1080)
        assertEquals(0, w % 2)
        assertEquals(0, h % 2)
        assertEquals(320 to 720, VideoSizes.align16(324, 720))
        assertTrue(VideoSizes.bitrate(16, 16, 1) >= 300_000)
        assertTrue(VideoSizes.bitrate(4096, 4096, 60) <= 8_000_000)
    }

    @Test fun sizeParameter() {
        assertEquals(1280 to 720, VideoSizes.parse("1280x720"))
        assertEquals(640 to 480, VideoSizes.parse(" 640X480 "))
        assertNull(VideoSizes.parse(null))
        assertNull(VideoSizes.parse("big"))
        assertNull(VideoSizes.parse("10x10"))
        assertNull(VideoSizes.parse("99999x720"))
    }
}
