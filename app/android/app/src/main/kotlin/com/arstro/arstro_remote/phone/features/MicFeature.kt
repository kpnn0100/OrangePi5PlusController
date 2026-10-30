package com.arstro.arstro_remote.phone.features

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AudioEffect
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.util.Log
import com.arstro.arstro_remote.phone.core.Feature
import com.arstro.arstro_remote.phone.core.OpError
import com.arstro.arstro_remote.phone.core.PhoneContext
import com.arstro.arstro_remote.phone.core.WsHandler
import com.arstro.arstro_remote.phone.core.WsSession
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

/** PHONE.md "Audio": 16 kHz mono s16le, 20 ms frames of 640 bytes. */
object Pcm {
    const val RATE = 16_000
    const val FRAME_BYTES = 640
    val CONFIG: String = JSONObject().put("t", "config").put("rate", RATE).put("channels", 1).put("format", "s16le").toString()
}

/** The microphone as 20 ms PCM frames on a thread of its own (optionally with echo cancelling / noise suppression). */
class PcmCapture(private val source: Int, private val voiceEffects: Boolean, private val onFrame: (ByteArray) -> Unit) {
    @Volatile private var running = false
    private var record: AudioRecord? = null
    private val effects = ArrayList<AudioEffect>()
    private var thread: Thread? = null

    @SuppressLint("MissingPermission")
    @Synchronized
    fun start() {
        if (running) return
        val min = AudioRecord.getMinBufferSize(Pcm.RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val r = try {
            AudioRecord(source, Pcm.RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(min, Pcm.FRAME_BYTES * 8))
        } catch (e: Exception) {
            throw OpError("the microphone is not available (${e.javaClass.simpleName})", 409)
        }
        if (r.state != AudioRecord.STATE_INITIALIZED) {
            r.release()
            throw OpError("the microphone is not available (in use by another app?)", 409)
        }
        if (voiceEffects) {
            val sid = r.audioSessionId
            try { if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(sid)?.let { it.enabled = true; effects.add(it) } } catch (e: Exception) { }
            try { if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(sid)?.let { it.enabled = true; effects.add(it) } } catch (e: Exception) { }
            try { if (AutomaticGainControl.isAvailable()) AutomaticGainControl.create(sid)?.let { it.enabled = true; effects.add(it) } } catch (e: Exception) { }
        }
        try {
            r.startRecording()
        } catch (e: Exception) {
            r.release()
            throw OpError("the microphone could not start (${e.javaClass.simpleName})", 409)
        }
        record = r
        running = true
        thread = Thread({ loop(r) }, "arstro-pcm").apply { isDaemon = true; start() }
    }

    private fun loop(r: AudioRecord) {
        val buf = ByteArray(Pcm.FRAME_BYTES)
        while (running) {
            var off = 0
            while (off < buf.size && running) {
                val n = r.read(buf, off, buf.size - off)
                if (n <= 0) {
                    if (n < 0) { running = false; break }
                    continue
                }
                off += n
            }
            if (off == buf.size) try { onFrame(buf.copyOf()) } catch (e: Exception) { }
        }
    }

    @Synchronized
    fun stop() {
        running = false
        try { record?.stop() } catch (e: Exception) { }
        try { thread?.join(500) } catch (e: InterruptedException) { }
        for (e in effects) try { e.release() } catch (x: Exception) { }
        effects.clear()
        try { record?.release() } catch (e: Exception) { }
        record = null
        thread = null
    }
}

/** Listen (PHONE.md "mic", PHN-07): the microphone live on /ws/mic, captured only while someone listens. */
object MicFeature : Feature("mic", "Listen") {
    override val permissions = listOf(Manifest.permission.RECORD_AUDIO)

    private const val FG = "mic"
    private const val MAX_QUEUED = 256L * 1024
    private val listeners = CopyOnWriteArrayList<WsSession>()
    private var capture: PcmCapture? = null
    private val lock = Any()

    override fun sockets() = mapOf("/ws/mic" to socket { c, s -> open(c, s) })

    override fun stop(ctx: PhoneContext) {
        for (s in listeners) try { s.close(1001, "$title was turned off on the phone") } catch (e: Exception) { }
        listeners.clear()
        stopCapture(ctx)
    }

    override fun note(ctx: PhoneContext): String? = if (listeners.isNotEmpty()) "${listeners.size} listening" else null

    private fun open(c: PhoneContext, s: WsSession): WsHandler {
        listeners.add(s)
        s.sendText(Pcm.CONFIG)
        Thread({
            try {
                synchronized(lock) {
                    if (capture == null && listeners.isNotEmpty()) {
                        c.foregroundBlocking(FG, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE, "microphone")
                        val cap = PcmCapture(MediaRecorder.AudioSource.MIC, false) { frame -> send(frame) }
                        cap.start()
                        capture = cap
                        c.log(Log.INFO, "mic: listening")
                    }
                }
            } catch (e: Exception) {
                val msg = (e as? OpError)?.message ?: "the microphone could not start (${e.javaClass.simpleName})"
                listeners.remove(s)
                try { s.sendText(JSONObject().put("t", "error").put("error", msg).toString()) } catch (x: Exception) { }
                try { s.close(1011, msg.take(120)) } catch (x: Exception) { }
                if (listeners.isEmpty()) stopCapture(c)
                synchronized(lock) { if (capture == null) c.releaseForeground(FG) }
            }
        }, "arstro-mic-open").start()
        return object : WsHandler {
            override fun onClose(session: WsSession, code: Int, reason: String) {
                listeners.remove(session)
                if (listeners.isEmpty()) Thread { stopCapture(c) }.start()
            }
        }
    }

    private fun send(frame: ByteArray) {
        for (s in listeners) {
            if (!s.isOpen || s.queued > MAX_QUEUED) continue
            try { s.sendBinary(frame) } catch (e: Exception) { }
        }
    }

    private fun stopCapture(c: PhoneContext) {
        synchronized(lock) {
            if (listeners.isNotEmpty()) return
            val cap = capture ?: return
            capture = null
            cap.stop()
            c.releaseForeground(FG)
            c.log(Log.INFO, "mic: stopped")
        }
    }
}
