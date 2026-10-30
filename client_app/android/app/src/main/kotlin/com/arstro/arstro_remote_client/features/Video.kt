package com.arstro.arstro_remote_client.features

/*
 * Pure helpers of the video streams (PHONE.md "Video"): H.264 Annex-B parsing, the WebCodecs codec
 * string, the binary frame header and the encoded size. No Android classes - JVM unit tests.
 */
object H264 {
    const val NAL_IDR = 5
    const val NAL_SPS = 7
    const val NAL_PPS = 8

    /** Offsets of the NAL units (first byte after each 00 00 01 / 00 00 00 01 start code). */
    fun nalOffsets(data: ByteArray): List<Int> {
        val out = ArrayList<Int>()
        var i = 0
        while (i + 2 < data.size) {
            if (data[i].toInt() == 0 && data[i + 1].toInt() == 0 && data[i + 2].toInt() == 1) {
                out.add(i + 3)
                i += 3
            } else {
                i++
            }
        }
        return out
    }

    fun nalTypes(data: ByteArray): List<Int> =
        nalOffsets(data).filter { it < data.size }.map { data[it].toInt() and 0x1F }

    fun hasSps(data: ByteArray): Boolean = nalTypes(data).contains(NAL_SPS)

    /** "avc1.PPCCLL" from the first SPS in `data` (profile_idc, constraint flags, level_idc), or null. */
    fun codecString(data: ByteArray): String? {
        for (off in nalOffsets(data)) {
            if (off + 3 < data.size && (data[off].toInt() and 0x1F) == NAL_SPS) {
                return "avc1.%02X%02X%02X".format(data[off + 1].toInt() and 0xFF, data[off + 2].toInt() and 0xFF,
                                                   data[off + 3].toInt() and 0xFF)
            }
        }
        return null
    }

    /** One binary video message: flags u8 (bit0 = key frame) ‖ pts_us u64 BE ‖ the access unit. */
    fun packet(key: Boolean, ptsUs: Long, au: ByteArray, prefix: ByteArray? = null): ByteArray {
        val pre = prefix?.size ?: 0
        val out = ByteArray(9 + pre + au.size)
        out[0] = if (key) 1 else 0
        for (i in 0 until 8) out[1 + i] = (ptsUs ushr (56 - 8 * i)).toByte()
        if (prefix != null) System.arraycopy(prefix, 0, out, 9, pre)
        System.arraycopy(au, 0, out, 9 + pre, au.size)
        return out
    }
}

object VideoSizes {
    /** The encoded size of a `srcW` x `srcH` picture with its long side at most `maxLong` (never upscaled),
     *  both even, same aspect ratio as close as the even rounding allows. */
    fun fit(srcW: Int, srcH: Int, maxLong: Int): Pair<Int, Int> {
        require(srcW > 0 && srcH > 0)
        val long = maxOf(srcW, srcH)
        val scale = if (maxLong in 1 until long) maxLong.toDouble() / long else 1.0
        fun even(v: Double) = maxOf(2, (Math.round(v).toInt()) and 1.inv())
        return even(srcW * scale) to even(srcH * scale)
    }

    /** Aligned to 16 (for encoders that refuse other sizes). */
    fun align16(w: Int, h: Int): Pair<Int, Int> = maxOf(16, w / 16 * 16) to maxOf(16, h / 16 * 16)

    /** A default bit rate for a size: about 0.05 bit per pixel and frame, 300 kbit/s .. 8 Mbit/s. */
    fun bitrate(w: Int, h: Int, fps: Int): Int =
        (w.toLong() * h * fps / 20).coerceIn(300_000L, 8_000_000L).toInt()

    /** "1280x720" -> (1280, 720), null when it is not a size. */
    fun parse(text: String?): Pair<Int, Int>? {
        val m = Regex("""^\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*$""").matchEntire(text ?: return null) ?: return null
        val w = m.groupValues[1].toInt()
        val h = m.groupValues[2].toInt()
        if (w < 16 || h < 16 || w > 8192 || h > 8192) return null
        return w to h
    }
}
