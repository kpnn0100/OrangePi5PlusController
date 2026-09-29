package com.arstro.arstro_remote

import android.app.DownloadManager
import android.content.Context
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.util.Log
import io.flutter.plugin.common.BasicMessageChannel
import io.flutter.plugin.common.BinaryCodec
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import io.flutter.view.TextureRegistry
import java.nio.ByteBuffer
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

private const val TAG = "ArstroMedia"

/**
 * Live preview (REC-02): H.264 access units from the Pi's /ws/preview (received in Dart)
 * are decoded by MediaCodec straight into a Flutter texture.
 *
 *   method channel "arstro/preview": create -> textureId, configure {width,height}, reset, release
 *   binary channel "arstro/preview/frames": [ver u8 = 1][flags u8: bit0 key][pts_us u64 BE][Annex-B AU]
 *   events back to Dart: playing, size {width,height}, error {message}
 */
class PreviewBridge(private val textures: TextureRegistry, messenger: BinaryMessenger) {
    private val main = Handler(Looper.getMainLooper())
    private val method = MethodChannel(messenger, "arstro/preview")
    private val frames = BasicMessageChannel(messenger, "arstro/preview/frames", BinaryCodec.INSTANCE,
                                             messenger.makeBackgroundTaskQueue())
    @Volatile private var decoder: Decoder? = null

    init {
        method.setMethodCallHandler { call, result -> handle(call, result) }
        frames.setMessageHandler { msg, reply ->
            if (msg != null) decoder?.push(msg)
            reply.reply(null)
        }
    }

    private fun handle(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "create" -> {
                decoder?.release()
                val d = Decoder(textures.createSurfaceProducer(), ::event)
                decoder = d
                result.success(d.id)
            }
            "configure" -> {
                decoder?.configure(call.argument<Int>("width") ?: 0, call.argument<Int>("height") ?: 0)
                result.success(null)
            }
            "reset" -> { decoder?.reset(); result.success(null) }
            "release" -> { decoder?.release(); decoder = null; result.success(null) }
            else -> result.notImplemented()
        }
    }

    private fun event(name: String, args: Map<String, Any?>) {
        main.post { method.invokeMethod(name, args) }
    }

    fun dispose() {
        decoder?.release()
        decoder = null
        method.setMethodCallHandler(null)
        frames.setMessageHandler(null)
    }
}

private class Frame(val key: Boolean, val pts: Long, val data: ByteArray)

private class Decoder(
    private val producer: TextureRegistry.SurfaceProducer,
    private val emit: (String, Map<String, Any?>) -> Unit,
) {
    val id: Long get() = producer.id()
    // short: when decoding falls behind we skip to the next keyframe instead of lagging
    private val queue = LinkedBlockingQueue<Frame>(6)
    private val lock = Object()
    @Volatile private var running = true
    @Volatile private var needKey = true
    private var surfaceOk = true
    private var resetRequested = false
    private var codec: MediaCodec? = null
    private var sps: ByteArray? = null
    private var width = 1280
    private var height = 720
    private var rendered = false
    private val thread = Thread({ loop() }, "arstro-preview")

    init {
        producer.setSize(width, height)
        producer.setCallback(object : TextureRegistry.SurfaceProducer.Callback {
            override fun onSurfaceAvailable() {
                synchronized(lock) { surfaceOk = true; resetRequested = true }
            }

            override fun onSurfaceCleanup() {
                // must stop drawing into the old surface before this returns
                synchronized(lock) { surfaceOk = false; releaseCodec() }
            }
        })
        thread.start()
    }

    fun configure(w: Int, h: Int) {
        if (w <= 0 || h <= 0) return
        synchronized(lock) {
            if (w != width || h != height) {
                width = w
                height = h
                producer.setSize(w, h)
                resetRequested = true
            }
        }
    }

    fun reset() {
        queue.clear()
        synchronized(lock) { resetRequested = true }
    }

    fun push(buf: ByteBuffer) {
        val n = buf.remaining()
        if (n <= 10 || buf.get(0).toInt() != 1) return
        val key = (buf.get(1).toInt() and 1) == 1
        val pts = buf.getLong(2)
        val data = ByteArray(n - 10)
        buf.position(10)
        buf.get(data)
        if (!queue.offer(Frame(key, pts, data))) {       // decoder fell behind: skip to a keyframe
            queue.clear()
            needKey = true
        }
    }

    private fun loop() {
        val info = MediaCodec.BufferInfo()
        while (running) {
            val f = try { queue.poll(15, TimeUnit.MILLISECONDS) } catch (e: InterruptedException) { null }
            synchronized(lock) {
                try {
                    if (resetRequested) {
                        releaseCodec()
                        resetRequested = false
                        needKey = true
                    }
                    drain(info)
                    if (f == null || (needKey && !f.key)) return@synchronized
                    if (f.key) {
                        val s = nal(f.data, 7)
                        if (codec != null && s != null && !s.contentEquals(sps)) releaseCodec()   // new size
                        if (codec == null && (!surfaceOk || start(f.data) == null)) return@synchronized
                    }
                    val c = codec ?: return@synchronized
                    val idx = c.dequeueInputBuffer(10_000)
                    if (idx < 0) {
                        needKey = true
                        return@synchronized
                    }
                    val ib = c.getInputBuffer(idx)
                    if (ib == null || ib.capacity() < f.data.size) {
                        c.queueInputBuffer(idx, 0, 0, f.pts, 0)
                        needKey = true
                        return@synchronized
                    }
                    ib.clear()
                    ib.put(f.data)
                    c.queueInputBuffer(idx, 0, f.data.size, f.pts, 0)
                    needKey = false
                    drain(info)
                } catch (e: Exception) {
                    Log.w(TAG, "decoder error", e)
                    releaseCodec()
                    needKey = true
                    emit("error", mapOf("message" to e.toString()))
                }
            }
        }
        synchronized(lock) { releaseCodec() }
    }

    private fun start(au: ByteArray): MediaCodec? {
        val s = nal(au, 7) ?: return null
        val p = nal(au, 8) ?: return null
        val mime = MediaFormat.MIMETYPE_VIDEO_AVC
        val fmt = MediaFormat.createVideoFormat(mime, width, height)
        fmt.setByteBuffer("csd-0", ByteBuffer.wrap(s))
        fmt.setByteBuffer("csd-1", ByteBuffer.wrap(p))
        fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, maxOf(1 shl 20, width * height))
        fmt.setInteger(MediaFormat.KEY_PRIORITY, 0)                      // real time
        val c = MediaCodec.createDecoderByType(mime)
        if (Build.VERSION.SDK_INT >= 30) {
            val caps = c.codecInfo.getCapabilitiesForType(mime)
            if (caps.isFeatureSupported(MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency)) {
                fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            }
        }
        c.configure(fmt, producer.surface, null, 0)
        c.start()
        codec = c
        sps = s
        rendered = false
        return c
    }

    private fun drain(info: MediaCodec.BufferInfo) {
        val c = codec ?: return
        while (true) {
            val i = c.dequeueOutputBuffer(info, 0)
            if (i >= 0) {
                c.releaseOutputBuffer(i, true)                               // show it now
                if (!rendered) {
                    rendered = true
                    emit("playing", emptyMap())
                }
            } else if (i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                val f = c.outputFormat
                var w = f.getInteger(MediaFormat.KEY_WIDTH)
                var h = f.getInteger(MediaFormat.KEY_HEIGHT)
                if (f.containsKey("crop-right") && f.containsKey("crop-left")) {
                    w = f.getInteger("crop-right") - f.getInteger("crop-left") + 1
                    h = f.getInteger("crop-bottom") - f.getInteger("crop-top") + 1
                }
                emit("size", mapOf("width" to w, "height" to h))
            } else {
                return
            }
        }
    }

    private fun releaseCodec() {
        val c = codec ?: return
        codec = null
        sps = null
        try { c.stop() } catch (_: Exception) {}
        try { c.release() } catch (_: Exception) {}
    }

    fun release() {
        running = false
        thread.interrupt()
        try { thread.join(500) } catch (_: InterruptedException) {}
        synchronized(lock) { releaseCodec() }
        producer.release()
    }

    companion object {
        /** The first NAL unit of `type` in an Annex-B access unit, with a 4-byte start code. */
        fun nal(au: ByteArray, type: Int): ByteArray? {
            var i = 0
            var start = -1
            while (i + 3 <= au.size) {
                val sc3 = au[i].toInt() == 0 && au[i + 1].toInt() == 0 && au[i + 2].toInt() == 1
                if (sc3) {
                    if (start >= 0) {
                        var end = i
                        if (end > start && au[end - 1].toInt() == 0) end--          // 4-byte start code
                        return byteArrayOf(0, 0, 0, 1) + au.copyOfRange(start, end)
                    }
                    val hdr = i + 3
                    if (hdr < au.size && (au[hdr].toInt() and 0x1f) == type) start = hdr
                    i = hdr
                    continue
                }
                i++
            }
            return if (start >= 0) byteArrayOf(0, 0, 0, 1) + au.copyOfRange(start, au.size) else null
        }
    }
}

/** Saves a recording into Downloads/Arstro with the system download manager (GAL-03). */
class DownloadBridge(private val context: Context, messenger: BinaryMessenger) {
    private val method = MethodChannel(messenger, "arstro/download")

    init {
        method.setMethodCallHandler { call, result ->
            if (call.method != "enqueue") return@setMethodCallHandler result.notImplemented()
            try {
                val url = call.argument<String>("url")!!
                val name = call.argument<String>("name")!!.replace('/', '_')
                val headers = call.argument<Map<String, String>>("headers") ?: emptyMap()
                val req = DownloadManager.Request(Uri.parse(url))
                    .setTitle(name)
                    .setDescription("Arstro Remote")
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "Arstro/$name")
                for ((k, v) in headers) req.addRequestHeader(k, v)
                val dm = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
                result.success(dm.enqueue(req))
            } catch (e: Exception) {
                result.error("download", e.message, null)
            }
        }
    }

    fun dispose() = method.setMethodCallHandler(null)
}
