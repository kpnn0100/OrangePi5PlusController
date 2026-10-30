package com.arstro.arstro_remote_client.features

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.Surface
import com.arstro.arstro_remote_client.core.WsSession
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

/*
 * The video path shared by the screen and the camera (PHONE.md "Video"): a MediaCodec H.264 encoder fed
 * through its input surface, and a hub that fans the encoded frames out to the viewers' WebSockets.
 */

/** An H.264 encoder with an input Surface; its output is Annex-B (SPS/PPS put in front of key frames). */
class AvcEncoder(
    val width: Int,
    val height: Int,
    val fps: Int,
    val bitrate: Int,
    /** Repeat the last frame after this many µs without a new one (screen: static content), 0 = off. */
    repeatUs: Long,
    private val listener: Listener,
) {
    interface Listener {
        fun onConfig(encoder: AvcEncoder, codec: String)
        fun onFrame(encoder: AvcEncoder, packet: ByteArray, key: Boolean)
        fun onError(encoder: AvcEncoder, error: Exception) {}
    }

    private val codec: MediaCodec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
    val surface: Surface
    @Volatile private var running = true
    @Volatile var codecString: String? = null
        private set
    @Volatile private var config: ByteArray? = null
    private val thread: Thread

    init {
        try {
            val f = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height)
            f.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            f.setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            f.setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            f.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
            f.setInteger(MediaFormat.KEY_PRIORITY, 0)          // real time
            if (repeatUs > 0) f.setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, repeatUs)
            if (Build.VERSION.SDK_INT >= 29) f.setFloat(MediaFormat.KEY_MAX_FPS_TO_ENCODER, fps.toFloat())
            if (Build.VERSION.SDK_INT >= 30) f.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            codec.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            surface = codec.createInputSurface()
            codec.start()
        } catch (e: Exception) {
            try { codec.release() } catch (x: Exception) { }
            throw e
        }
        thread = Thread({ drain() }, "arstro-avc").apply { isDaemon = true; start() }
    }

    fun requestKeyFrame() {
        if (!running) return
        try {
            codec.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
        } catch (e: Exception) { }
    }

    @Synchronized
    fun release() {
        if (!running) return
        running = false
        try { thread.join(1000) } catch (e: InterruptedException) { }
        try { codec.stop() } catch (e: Exception) { }
        try { codec.release() } catch (e: Exception) { }
        try { surface.release() } catch (e: Exception) { }
    }

    private fun setConfig(bytes: ByteArray) {
        if (config?.contentEquals(bytes) == true) return
        config = bytes
        val c = H264.codecString(bytes) ?: return
        codecString = c
        listener.onConfig(this, c)
    }

    private fun drain() {
        val info = MediaCodec.BufferInfo()
        try {
            while (running) {
                val idx = codec.dequeueOutputBuffer(info, 100_000)
                if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    val fmt = codec.outputFormat
                    val sps = fmt.getByteBuffer("csd-0")
                    val pps = fmt.getByteBuffer("csd-1")
                    if (sps != null && config == null) {
                        val a = ByteArray(sps.remaining()).also { sps.get(it) }
                        val b = if (pps != null) ByteArray(pps.remaining()).also { pps.get(it) } else ByteArray(0)
                        setConfig(a + b)
                    }
                    continue
                }
                if (idx < 0) continue
                val buf = codec.getOutputBuffer(idx)
                if (buf == null || info.size <= 0) {
                    codec.releaseOutputBuffer(idx, false)
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) break
                    continue
                }
                buf.position(info.offset)
                buf.limit(info.offset + info.size)
                val data = ByteArray(info.size)
                buf.get(data)
                codec.releaseOutputBuffer(idx, false)
                if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                    setConfig(data)
                    continue
                }
                val key = info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
                val prefix = if (key && !H264.hasSps(data)) config else null
                listener.onFrame(this, H264.packet(key, info.presentationTimeUs, data, prefix), key)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) break
            }
        } catch (e: Exception) {
            if (running) {
                log(Log.WARN, "video encoder stopped: ${e.javaClass.simpleName}")
                listener.onError(this, e)
            }
        }
    }

    companion object {
        /** An encoder at `w` x `h`, or at the 16-aligned size when the codec refuses the exact one. */
        fun create(w: Int, h: Int, fps: Int, bitrate: Int, repeatUs: Long, listener: Listener): AvcEncoder = try {
            AvcEncoder(w, h, fps, bitrate, repeatUs, listener)
        } catch (e: Exception) {
            val (aw, ah) = VideoSizes.align16(w, h)
            if (aw == w && ah == h) throw e
            AvcEncoder(aw, ah, fps, bitrate, repeatUs, listener)
        }
    }
}

/** The viewers of one video source: config + frames to each, dropping frames for a viewer that lags. */
class VideoHub(private val requestKeyFrame: () -> Unit) {
    class Viewer(val session: WsSession) {
        @Volatile var needKey = true
        @Volatile var max = 0          // requested long side (0 = default)
        @Volatile var fps = 0
        @Volatile var kbps = 0
    }

    private val viewers = CopyOnWriteArrayList<Viewer>()
    @Volatile private var configText: String? = null
    @Volatile private var lastKeyRequest = 0L

    val count: Int get() = viewers.size
    val all: List<Viewer> get() = viewers.toList()

    fun add(session: WsSession): Viewer {
        val v = Viewer(session)
        viewers.add(v)
        return v
    }

    fun remove(session: WsSession) { viewers.removeAll { it.session === session } }

    fun find(session: WsSession): Viewer? = viewers.firstOrNull { it.session === session }

    /** The largest requested long side / fps / kbps among the viewers (0 when none asked). */
    fun maxQuality(): Triple<Int, Int, Int> =
        Triple(viewers.maxOfOrNull { it.max } ?: 0, viewers.maxOfOrNull { it.fps } ?: 0, viewers.maxOfOrNull { it.kbps } ?: 0)

    /** A new picture format: `{"t": "config", codec, width, height, rotation}` to everyone, then a key frame. */
    fun config(codec: String, width: Int, height: Int, rotation: Int) {
        val text = JSONObject().put("t", "config").put("codec", codec).put("width", width).put("height", height)
            .put("rotation", rotation).toString()
        configText = text
        for (v in viewers) {
            v.needKey = true
            sendText(v.session, text)
        }
        keyFrame(force = true)
    }

    fun clearConfig() { configText = null }

    /** A viewer that joins a running stream: the current config and a key frame soon. */
    fun greet(v: Viewer) {
        configText?.let { sendText(v.session, it) }
        v.needKey = true
        keyFrame(force = true)
    }

    fun frame(packet: ByteArray, key: Boolean) {
        for (v in viewers) {
            val s = v.session
            if (!s.isOpen) continue
            if (v.needKey && !key) continue
            if (s.queued > MAX_QUEUED) {
                v.needKey = true
                keyFrame(force = false)
                continue
            }
            try {
                s.sendBinary(packet)
                if (key) v.needKey = false
            } catch (e: Exception) { }
        }
        if (!key && viewers.any { it.needKey }) keyFrame(force = false)
    }

    fun text(text: String) { for (v in viewers) sendText(v.session, text) }

    fun closeAll(code: Int, reason: String) {
        for (v in viewers) try { v.session.close(code, reason) } catch (e: Exception) { }
        viewers.clear()
    }

    private fun keyFrame(force: Boolean) {
        val now = System.currentTimeMillis()
        if (!force && now - lastKeyRequest < 1000) return
        lastKeyRequest = now
        requestKeyFrame()
    }

    private fun sendText(s: WsSession, text: String) {
        try { if (s.isOpen) s.sendText(text) } catch (e: Exception) { }
    }

    companion object {
        const val MAX_QUEUED = 1L shl 20
    }
}
