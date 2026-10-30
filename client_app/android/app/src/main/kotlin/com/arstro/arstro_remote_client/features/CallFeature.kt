package com.arstro.arstro_remote_client.features

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.Ringtone
import android.media.RingtoneManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person
import com.arstro.arstro_remote_client.R
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import com.arstro.arstro_remote_client.core.SpecialAccess
import com.arstro.arstro_remote_client.core.WsHandler
import com.arstro.arstro_remote_client.core.WsSession
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Internet calls (PHONE.md "Call", PHN-06): the phone rings full screen (CallActivity, also over the lock
 * screen), the person answers or declines; while active the phone's microphone (voice communication, echo
 * cancelling) goes to the viewer and the viewer's PCM plays on the earpiece (speaker on request).
 * States: idle -> ringing -> active -> ended (reason) -> idle; topic `call`.
 */
object CallFeature : Feature("call", "Internet calls") {
    // (the core counts POST_NOTIFICATIONS as granted before Android 13)
    override val permissions = listOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS)
    override val specialAccess = listOf(SpecialAccess.OVERLAY, SpecialAccess.FULL_SCREEN_INTENT)

    const val ACTION_ANSWER = "com.arstro.arstro_remote_client.call.ANSWER"
    const val ACTION_DECLINE = "com.arstro.arstro_remote_client.call.DECLINE"
    const val ACTION_HANGUP = "com.arstro.arstro_remote_client.call.HANGUP"
    private const val CHANNEL = "arstro_call"
    private const val NOTIFICATION_ID = 7101
    private const val FG = "call"
    private const val RING_TIMEOUT_MS = 45_000L
    private const val MAX_QUEUED = 256L * 1024

    private val lock = Any()
    @Volatile private var ctx: PhoneContext? = null
    @Volatile private var app: Context? = null
    @Volatile var state = "idle"
        private set
    @Volatile var reason: String? = null
        private set
    @Volatile var since = System.currentTimeMillis()
        private set
    @Volatile var caller = "Arstro"
        private set
    @Volatile var speaker = false
        private set
    @Volatile var muted = false
        private set
    @Volatile private var socket: WsSession? = null
    private var capture: PcmCapture? = null
    @Volatile private var track: AudioTrack? = null
    private var ringtone: Ringtone? = null
    private var savedMode = AudioManager.MODE_NORMAL
    private val uiListeners = CopyOnWriteArrayList<() -> Unit>()
    private val timeout = Runnable { end("no answer") }
    private val toIdle = Runnable {
        if (state == "ended") {
            state = "idle"
            reason = null
            since = System.currentTimeMillis()
            changed()
        }
    }
    private val keepRinging = object : Runnable {
        override fun run() {
            if (state != "ringing") return
            ringtone?.let { if (!it.isPlaying) try { it.play() } catch (e: Exception) { } }
            mainHandler.postDelayed(this, 2000)
        }
    }

    // ------------------------------------------------------------------------------------- API

    override fun ops() = mapOf(
        "call.start" to op { c, a -> remember(c); ring(c, a.str("name")); jsonOf("state" to state) },
        "call.end" to op { c, _ -> remember(c); end("the caller hung up"); null },
    )

    override fun sockets() = mapOf("/ws/call" to socket { c, s -> open(c, s) })

    override fun start(ctx: PhoneContext) {
        remember(ctx)
        publish()
    }

    override fun stop(ctx: PhoneContext) {
        end("$title was turned off on the phone")
        this.ctx = null
    }

    override fun note(ctx: PhoneContext): String? = when (state) {
        "ringing" -> "ringing"
        "active" -> "in a call"
        else -> null
    }

    fun addListener(l: () -> Unit) { uiListeners.add(l) }
    fun removeListener(l: () -> Unit) { uiListeners.remove(l) }

    private fun remember(c: PhoneContext) {
        if (ctx == null) ctx = c
        app = c.app
    }

    private fun stateJson(): JSONObject =
        jsonOf("state" to state, "since" to since).also { o -> reason?.let { o.put("reason", it) } }

    private fun changed() {
        try { ctx?.events?.publish("call", stateJson()) } catch (e: Exception) { }
        val s = socket
        if (s != null && s.isOpen) try { s.sendText(stateJson().put("t", "state").toString()) } catch (e: Exception) { }
        mainHandler.post { for (l in uiListeners) try { l() } catch (e: Exception) { } }
    }

    private fun publish() { try { ctx?.events?.publish("call", stateJson()) } catch (e: Exception) { } }

    // ------------------------------------------------------------------------------------- viewer side

    private fun open(c: PhoneContext, s: WsSession): WsHandler {
        remember(c)
        synchronized(lock) {
            val cur = socket
            if (cur != null && cur.isOpen && cur !== s) {
                val why = "the phone is already in a call"
                try { s.sendText(JSONObject().put("t", "state").put("state", "ended").put("reason", why).toString()) } catch (e: Exception) { }
                try { s.close(1008, why) } catch (e: Exception) { }
                return object : WsHandler {}
            }
            socket = s
        }
        if (state == "idle" || state == "ended") {
            try {
                ring(c, s.query["name"]?.firstOrNull())
            } catch (e: OpError) {
                synchronized(lock) { if (socket === s) socket = null }
                try { s.close(1011, e.message ?: "the call could not start") } catch (x: Exception) { }
                return object : WsHandler {}
            }
        } else {
            try { s.sendText(stateJson().put("t", "state").toString()) } catch (e: Exception) { }
            if (state == "active") try { s.sendText(Pcm.CONFIG) } catch (e: Exception) { }
        }
        return object : WsHandler {
            override fun onText(session: WsSession, text: String) {
                val m = try { JSONObject(text) } catch (e: Exception) { return }
                if (m.optString("t") == "end") end("the caller hung up")
            }

            override fun onBinary(session: WsSession, data: ByteArray) {
                if (state != "active") return
                val t = track ?: return
                try { t.write(data, 0, data.size and 1.inv(), AudioTrack.WRITE_NON_BLOCKING) } catch (e: Exception) { }
            }

            override fun onClose(session: WsSession, code: Int, reason: String) {
                val mine = synchronized(lock) { if (socket === session) { socket = null; true } else false }
                if (mine && (state == "ringing" || state == "active")) end("the caller left")
            }
        }
    }

    // ------------------------------------------------------------------------------------- the call

    private fun ring(c: PhoneContext, name: String?) {
        synchronized(lock) {
            if (state == "ringing" || state == "active") throw OpError("the phone is already in a call", 409)
            state = "ringing"
            reason = null
            since = System.currentTimeMillis()
            caller = name?.trim()?.take(60)?.takeIf { it.isNotEmpty() } ?: "Arstro"
            speaker = false
            muted = false
        }
        c.log(Log.INFO, "call: ringing")
        mainHandler.removeCallbacks(toIdle)
        mainHandler.removeCallbacks(timeout)
        mainHandler.postDelayed(timeout, RING_TIMEOUT_MS)
        mainHandler.post { startRinging(c.app) }
        notifyRinging(c.app)
        showOwnActivity(c, c.app, Intent(c.app, CallActivity::class.java))
        changed()
    }

    /** CallActivity: Answer. */
    fun answer() {
        val c = ctx ?: return
        val ok: Boolean
        synchronized(lock) {
            if (state != "ringing") return
            ok = socket?.isOpen == true
            if (ok) {
                state = "active"
                since = System.currentTimeMillis()
            }
        }
        mainHandler.removeCallbacks(timeout)
        if (!ok) {
            end("the caller left")
            return
        }
        stopRinging()
        changed()
        notifyActive(c.app)
        Thread({
            try {
                c.foregroundBlocking(FG, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE, "microphone")
                startAudio(c.app)
                socket?.let { s -> try { s.sendText(Pcm.CONFIG) } catch (e: Exception) { } }
                c.log(Log.INFO, "call: active")
            } catch (e: Exception) {
                end((e as? OpError)?.message ?: "the call audio could not start (${e.javaClass.simpleName})")
            }
        }, "arstro-call-audio").start()
    }

    /** CallActivity: Decline. */
    fun decline() = end("declined on the phone")

    /** CallActivity: End. */
    fun hangUp() = end("hung up on the phone")

    fun end(why: String) {
        val c = ctx
        val s: WsSession?
        synchronized(lock) {
            if (state != "ringing" && state != "active") return
            state = "ended"
            reason = why
            since = System.currentTimeMillis()
            s = socket
            socket = null
        }
        c?.log(Log.INFO, "call: ended")
        mainHandler.removeCallbacks(timeout)
        stopRinging()
        stopAudio()
        app?.let { a -> try { NotificationManagerCompat.from(a).cancel(NOTIFICATION_ID) } catch (e: Exception) { } }
        c?.releaseForeground(FG)
        changed()
        if (s != null && s.isOpen) {
            try { s.sendText(JSONObject().put("t", "state").put("state", "ended").put("reason", why).toString()) } catch (e: Exception) { }
            try { s.close(1000, why.take(120)) } catch (e: Exception) { }
        }
        mainHandler.postDelayed(toIdle, 5000)
    }

    fun setSpeaker(on: Boolean) {
        speaker = on
        app?.let { route(it) }
        changed()
    }

    fun setMuted(on: Boolean) {
        muted = on
        changed()
    }

    // ------------------------------------------------------------------------------------- ringing

    @Suppress("DEPRECATION")
    private fun startRinging(app: Context) {
        if (state != "ringing") return
        try {
            val uri = RingtoneManager.getActualDefaultRingtoneUri(app, RingtoneManager.TYPE_RINGTONE)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            val r = RingtoneManager.getRingtone(app, uri)
            if (r != null) {
                r.audioAttributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
                if (Build.VERSION.SDK_INT >= 28) r.isLooping = true
                r.play()
                ringtone = r
                mainHandler.postDelayed(keepRinging, 2000)
            }
        } catch (e: Exception) { }
        val am = app.getSystemService(AudioManager::class.java)
        if (am?.ringerMode != AudioManager.RINGER_MODE_SILENT) {
            try {
                val pattern = longArrayOf(0, 800, 800)
                val attrs = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE).build()
                if (Build.VERSION.SDK_INT >= 26) vibrator(app)?.vibrate(VibrationEffect.createWaveform(pattern, 0), attrs)
                else vibrator(app)?.vibrate(pattern, 0, attrs)
            } catch (e: Exception) { }
        }
    }

    private fun stopRinging() {
        mainHandler.removeCallbacks(keepRinging)
        mainHandler.post {
            try { ringtone?.stop() } catch (e: Exception) { }
            ringtone = null
            app?.let { a -> try { vibrator(a)?.cancel() } catch (e: Exception) { } }
        }
    }

    @Suppress("DEPRECATION")
    internal fun vibrator(app: Context): Vibrator? =
        if (Build.VERSION.SDK_INT >= 31) app.getSystemService(VibratorManager::class.java)?.defaultVibrator
        else app.getSystemService(Vibrator::class.java)

    private fun activityIntent(app: Context, action: String?) =
        PendingIntent.getActivity(app, (action ?: "open").hashCode(),
            Intent(app, CallActivity::class.java).setAction(action).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PI_FLAGS)

    @SuppressLint("MissingPermission")
    private fun notifyRinging(app: Context) {
        app.channel(CHANNEL, "Arstro calls", NotificationManager.IMPORTANCE_HIGH, silent = true)
        val full = activityIntent(app, null)
        val person = Person.Builder().setName(caller).build()
        val b = NotificationCompat.Builder(app, CHANNEL)
            .setSmallIcon(R.drawable.phone_ic_call)
            .setContentTitle("Arstro call")
            .setContentText("$caller is calling")
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setOngoing(true)
            .setContentIntent(full)
            .setFullScreenIntent(full, true)
        val n = try {
            b.setStyle(NotificationCompat.CallStyle.forIncomingCall(person, activityIntent(app, ACTION_DECLINE),
                                                                    activityIntent(app, ACTION_ANSWER))).build()
        } catch (e: Exception) {
            b.setStyle(null)
                .addAction(0, "Decline", activityIntent(app, ACTION_DECLINE))
                .addAction(0, "Answer", activityIntent(app, ACTION_ANSWER)).build()
        }
        try { NotificationManagerCompat.from(app).notify(NOTIFICATION_ID, n) } catch (e: Exception) { }
    }

    @SuppressLint("MissingPermission")
    private fun notifyActive(app: Context) {
        val full = activityIntent(app, null)
        val b = NotificationCompat.Builder(app, CHANNEL)
            .setSmallIcon(R.drawable.phone_ic_call)
            .setContentTitle("Arstro call")
            .setContentText("In a call with $caller")
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setUsesChronometer(true)
            .setWhen(since)
            .setContentIntent(full)
        val n = try {
            b.setStyle(NotificationCompat.CallStyle.forOngoingCall(Person.Builder().setName(caller).build(),
                                                                   activityIntent(app, ACTION_HANGUP))).build()
        } catch (e: Exception) {
            b.setStyle(null).addAction(0, "Hang up", activityIntent(app, ACTION_HANGUP)).build()
        }
        try { NotificationManagerCompat.from(app).notify(NOTIFICATION_ID, n) } catch (e: Exception) { }
    }

    // ------------------------------------------------------------------------------------- audio

    private fun startAudio(app: Context) {
        val am = app.getSystemService(AudioManager::class.java)
        savedMode = am.mode
        am.mode = AudioManager.MODE_IN_COMMUNICATION
        route(app)
        val min = AudioTrack.getMinBufferSize(Pcm.RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val t = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(Pcm.RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(maxOf(min, Pcm.FRAME_BYTES * 16))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        t.play()
        track = t
        val cap = PcmCapture(MediaRecorder.AudioSource.VOICE_COMMUNICATION, true) { frame ->
            val s = socket
            if (s != null && s.isOpen && s.queued < MAX_QUEUED && state == "active") {
                try { s.sendBinary(if (muted) ByteArray(frame.size) else frame) } catch (e: Exception) { }
            }
        }
        try {
            cap.start()
        } catch (e: Exception) {
            stopAudio()
            throw e
        }
        synchronized(lock) {
            if (state != "active") { cap.stop(); return }
            capture = cap
        }
    }

    @Suppress("DEPRECATION")
    private fun stopAudio() {
        val cap: PcmCapture?
        synchronized(lock) {
            cap = capture
            capture = null
        }
        cap?.stop()
        val t = track
        track = null
        if (t != null) {
            try { t.stop() } catch (e: Exception) { }
            try { t.release() } catch (e: Exception) { }
            app?.let { a ->
                val am = a.getSystemService(AudioManager::class.java)
                try {
                    if (Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice() else am.isSpeakerphoneOn = false
                    am.mode = savedMode
                } catch (e: Exception) { }
            }
        }
    }

    /** Earpiece by default, the loudspeaker when asked (or when there is no earpiece). */
    private fun route(app: Context) {
        val am = app.getSystemService(AudioManager::class.java) ?: return
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                val devices = am.availableCommunicationDevices
                val want = if (speaker) AudioDeviceInfo.TYPE_BUILTIN_SPEAKER else AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
                val d = devices.firstOrNull { it.type == want } ?: devices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
                if (d != null) am.setCommunicationDevice(d)
            } else {
                @Suppress("DEPRECATION")
                am.isSpeakerphoneOn = speaker
            }
        } catch (e: Exception) { }
    }
}
