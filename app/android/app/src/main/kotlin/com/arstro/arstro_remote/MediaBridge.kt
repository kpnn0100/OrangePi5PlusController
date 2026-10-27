package com.arstro.arstro_remote

import android.app.DownloadManager
import android.content.ContentValues
import android.content.Context
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Log
import io.flutter.plugin.common.BasicMessageChannel
import io.flutter.plugin.common.BinaryCodec
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import io.flutter.view.TextureRegistry
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

private const val TAG = "ArstroMedia"

/**
 * Live preview (REC-02): H.264 access units from the Pi's /ws/preview (received in Dart)
 * are decoded by MediaCodec straight into a Flutter texture.
 *
 * Two independent decoders, each with its own thread, queue, codec and texture: "main" - the
 * whole picture (stream version 1) - and "loupe" - the focus loupe's small square stream
 * (version 3, REC-14). The loupe never touches the main decoder.
 *
 *   method channel "arstro/preview": create -> textureId, configure {width,height}, reset, release;
 *     with {decoder: "loupe"} each acts on the loupe decoder (create/release of the main one
 *     also ends the loupe: the controller that owns the bridge changed)
 *   binary channel "arstro/preview/frames": [ver u8][flags u8: bit0 key][pts_us u64 BE][Annex-B AU]
 *     routed by the version byte: 1 -> main, 3 -> loupe
 *   events back to Dart: playing, size {width,height}, error {message} - the loupe's carry
 *     decoder: "loupe"
 */
class PreviewBridge(private val textures: TextureRegistry, messenger: BinaryMessenger) {
    private val main = Handler(Looper.getMainLooper())
    private val method = MethodChannel(messenger, "arstro/preview")
    private val frames = BasicMessageChannel(messenger, "arstro/preview/frames", BinaryCodec.INSTANCE,
                                             messenger.makeBackgroundTaskQueue())
    @Volatile private var decoder: Decoder? = null
    @Volatile private var loupe: Decoder? = null

    init {
        method.setMethodCallHandler { call, result -> handle(call, result) }
        frames.setMessageHandler { msg, reply ->
            if (msg != null && msg.remaining() > 0) {
                when (msg.get(0).toInt()) {
                    1 -> decoder?.push(msg)
                    3 -> loupe?.push(msg)
                }
            }
            reply.reply(null)
        }
    }

    private fun handle(call: MethodCall, result: MethodChannel.Result) {
        val isLoupe = (call.arguments as? Map<*, *>)?.get("decoder") == "loupe"
        when (call.method) {
            "create" -> {
                if (isLoupe) {
                    releaseLoupe()
                    val d = Decoder(textures.createSurfaceProducer(), 3, "arstro-loupe", 480, 480) { name, args ->
                        event(name, args + ("decoder" to "loupe"))
                    }
                    loupe = d
                    result.success(d.id)
                } else {
                    releaseLoupe()
                    decoder?.release()
                    val d = Decoder(textures.createSurfaceProducer(), 1, "arstro-preview", 1280, 720, ::event)
                    decoder = d
                    result.success(d.id)
                }
            }
            "configure" -> {
                (if (isLoupe) loupe else decoder)
                    ?.configure(call.argument<Int>("width") ?: 0, call.argument<Int>("height") ?: 0)
                result.success(null)
            }
            "reset" -> { (if (isLoupe) loupe else decoder)?.reset(); result.success(null) }
            "release" -> {
                releaseLoupe()
                if (!isLoupe) { decoder?.release(); decoder = null }
                result.success(null)
            }
            else -> result.notImplemented()
        }
    }

    private fun releaseLoupe() {
        val l = loupe ?: return
        loupe = null
        l.release()
    }

    private fun event(name: String, args: Map<String, Any?>) {
        main.post { method.invokeMethod(name, args) }
    }

    fun dispose() {
        releaseLoupe()
        decoder?.release()
        decoder = null
        method.setMethodCallHandler(null)
        frames.setMessageHandler(null)
    }
}

private class Frame(val key: Boolean, val pts: Long, val data: ByteArray)

/** One MediaCodec decoder into one texture, fed the frames of stream [version]. */
private class Decoder(
    private val producer: TextureRegistry.SurfaceProducer,
    private val version: Int,
    threadName: String,
    private var width: Int,
    private var height: Int,
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
    private var rendered = false
    private val thread = Thread({ loop() }, threadName)

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

    /**
     * A new stream size (main: a `config` - the preview quality changed; loupe: a `zoom` reply
     * with a new size, REC-14). Dart holds the frames that follow until this
     * returns, so the queue holds only frames of the old stream: they are dropped, the codec
     * is restarted at the new size on the new stream's first keyframe. A new stream of the same
     * size is caught by its SPS in loop().
     */
    fun configure(w: Int, h: Int) {
        if (w <= 0 || h <= 0) return
        synchronized(lock) {
            if (w != width || h != height) {
                width = w
                height = h
                queue.clear()
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
        if (n <= 10 || buf.get(0).toInt() != version) return
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

/**
 * Saves into Downloads/Arstro: a recording with the system download manager (GAL-03), or text
 * the Pi sent - the editor's CSV sheet (GAL-13) - written there directly.
 *
 *   enqueue {url, name, headers} -> download id
 *   save {name, text, mime} -> "Downloads/Arstro/<name>"
 */
class DownloadBridge(private val context: Context, messenger: BinaryMessenger) {
    private val method = MethodChannel(messenger, "arstro/download")
    private val main = Handler(Looper.getMainLooper())

    init {
        method.setMethodCallHandler { call, result ->
            when (call.method) {
                "enqueue" -> enqueue(call, result)
                "save" -> save(call, result)
                else -> result.notImplemented()
            }
        }
    }

    private fun enqueue(call: MethodCall, result: MethodChannel.Result) {
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

    private fun save(call: MethodCall, result: MethodChannel.Result) {
        val name = (call.argument<String>("name") ?: "arstro.txt").replace('/', '_')
        val text = call.argument<String>("text") ?: ""
        val mime = call.argument<String>("mime") ?: "text/plain"
        Thread {
            try {
                val bytes = text.toByteArray(Charsets.UTF_8)
                if (Build.VERSION.SDK_INT >= 29) {
                    val resolver = context.contentResolver
                    val values = ContentValues().apply {
                        put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                        put(MediaStore.MediaColumns.MIME_TYPE, mime)
                        put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Arstro")
                        put(MediaStore.MediaColumns.IS_PENDING, 1)
                    }
                    val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                        ?: throw IllegalStateException("Downloads refused the file")
                    try {
                        resolver.openOutputStream(uri)?.use { it.write(bytes) }
                            ?: throw IllegalStateException("cannot write to Downloads")
                    } catch (e: Exception) {
                        resolver.delete(uri, null, null)
                        throw e
                    }
                    values.clear()
                    values.put(MediaStore.MediaColumns.IS_PENDING, 0)
                    resolver.update(uri, values, null, null)
                } else {
                    @Suppress("DEPRECATION")
                    val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "Arstro")
                    dir.mkdirs()
                    File(dir, name).writeBytes(bytes)
                }
                main.post { result.success("Downloads/Arstro/$name") }
            } catch (e: Exception) {
                Log.w(TAG, "save $name", e)
                main.post { result.error("save", e.message, null) }
            }
        }.start()
    }

    fun dispose() = method.setMethodCallHandler(null)
}
