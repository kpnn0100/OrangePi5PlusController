package com.arstro.arstro_remote_client.features

import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.SystemClock
import android.os.VibrationEffect
import android.provider.Settings
import android.speech.tts.TextToSpeech
import android.view.KeyEvent
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import com.arstro.arstro_remote_client.core.SpecialAccess
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.json.JSONObject

/** Audio & tools (PHN-15): volume per stream, media keys, speak, vibrate, torch, brightness, open a URL. */
object ToolsFeature : Feature("tools", "Audio & tools") {
    override val specialAccess = listOf(SpecialAccess.OVERLAY, SpecialAccess.WRITE_SETTINGS)

    @Volatile private var tts: TextToSpeech? = null
    @Volatile private var ttsReady = false

    override fun ops() = mapOf(
        "tools.volume" to op { c, a -> volume(c.app, a.str("stream") ?: "media", a.num("level")) },
        "tools.media" to op { c, a -> media(c.app, a.needStr("action")) },
        "tools.speak" to op { c, a -> speak(c.app, a.needStr("text"), a.str("lang")) },
        "tools.vibrate" to op { c, a -> vibrate(c.app, (a.int("ms") ?: 500).coerceIn(1, 10_000)) },
        "tools.torch" to op { c, a -> Torch.set(c.app, a.bool("on") ?: throw OpError("\"on\" is missing (true or false)")); jsonOf("on" to Torch.on) },
        "tools.brightness" to op { c, a -> brightness(c.app, a) },
        "tools.open_url" to op { c, a -> openUrl(c, a.needStr("url")) },
    )

    override fun stop(ctx: PhoneContext) {
        val t = tts
        tts = null
        ttsReady = false
        if (t != null) mainHandler.post { try { t.shutdown() } catch (e: Exception) { } }
    }

    private fun streamOf(name: String) = when (name.lowercase()) {
        "media", "music" -> AudioManager.STREAM_MUSIC
        "ring", "ringer" -> AudioManager.STREAM_RING
        "alarm" -> AudioManager.STREAM_ALARM
        "call", "voice" -> AudioManager.STREAM_VOICE_CALL
        "notification" -> AudioManager.STREAM_NOTIFICATION
        else -> throw OpError("stream must be media, ring, alarm or call")
    }

    private fun volume(app: Context, stream: String, level: Double?): JSONObject {
        val s = streamOf(stream)
        val am = app.getSystemService(AudioManager::class.java)
        val max = am.getStreamMaxVolume(s)
        if (level != null) {
            if (level.isNaN() || level < 0 || level > 1) throw OpError("\"level\" must be between 0 and 1")
            val min = if (Build.VERSION.SDK_INT >= 28) am.getStreamMinVolume(s) else 0
            val v = Math.round(level * max).toInt().coerceIn(min, max)
            try {
                am.setStreamVolume(s, v, AudioManager.FLAG_SHOW_UI)
            } catch (e: SecurityException) {
                throw OpError("Android does not allow changing this volume while Do Not Disturb is on", 409)
            }
        }
        val cur = am.getStreamVolume(s)
        return jsonOf("stream" to stream.lowercase(), "level" to (if (max > 0) cur.toDouble() / max else 0.0), "max" to max)
    }

    private fun media(app: Context, action: String): Any? {
        val code = when (action.lowercase()) {
            "play_pause", "play", "pause" -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
            "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous", "prev" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> throw OpError("action must be play_pause, next, previous or stop")
        }
        val am = app.getSystemService(AudioManager::class.java)
        val t = SystemClock.uptimeMillis()
        am.dispatchMediaKeyEvent(KeyEvent(t, t, KeyEvent.ACTION_DOWN, code, 0))
        am.dispatchMediaKeyEvent(KeyEvent(t, t, KeyEvent.ACTION_UP, code, 0))
        return null
    }

    private fun engine(app: Context): TextToSpeech {
        tts?.let { if (ttsReady) return it }
        val latch = CountDownLatch(1)
        var status = TextToSpeech.ERROR
        val t = onMain { TextToSpeech(app) { s -> status = s; latch.countDown() } }
        if (!latch.await(10, TimeUnit.SECONDS) || status != TextToSpeech.SUCCESS) {
            mainHandler.post { try { t.shutdown() } catch (e: Exception) { } }
            throw OpError("no text-to-speech engine works on this phone", 409)
        }
        tts?.let { old -> if (old !== t) mainHandler.post { try { old.shutdown() } catch (e: Exception) { } } }
        tts = t
        ttsReady = true
        return t
    }

    private fun speak(app: Context, text: String, lang: String?): Any? {
        if (text.length > TextToSpeech.getMaxSpeechInputLength()) throw OpError("the text is too long to speak")
        val t = engine(app)
        if (!lang.isNullOrBlank()) {
            val r = t.setLanguage(Locale.forLanguageTag(lang))
            if (r == TextToSpeech.LANG_MISSING_DATA || r == TextToSpeech.LANG_NOT_SUPPORTED) {
                throw OpError("the phone cannot speak \"$lang\"", 409)
            }
        }
        if (t.speak(text, TextToSpeech.QUEUE_ADD, null, "arstro-" + SystemClock.uptimeMillis()) != TextToSpeech.SUCCESS) {
            throw OpError("the phone could not speak it", 409)
        }
        return null
    }

    @Suppress("DEPRECATION")
    private fun vibrate(app: Context, ms: Int): Any? {
        val v = CallFeature.vibrator(app) ?: throw OpError("this phone cannot vibrate", 404)
        if (!v.hasVibrator()) throw OpError("this phone cannot vibrate", 404)
        if (Build.VERSION.SDK_INT >= 26) v.vibrate(VibrationEffect.createOneShot(ms.toLong(), VibrationEffect.DEFAULT_AMPLITUDE))
        else v.vibrate(ms.toLong())
        return null
    }

    private fun brightness(app: Context, a: JSONObject): JSONObject {
        if (!Settings.System.canWrite(app)) {
            throw OpError("brightness needs Modify system settings - allow it in the app", 409)
        }
        val cr = app.contentResolver
        val raw = a.opt("level")
        if (raw == "auto" || a.bool("auto") == true) {
            Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC)
        } else {
            val level = a.num("level") ?: throw OpError("\"level\" is missing (0..1 or \"auto\")")
            if (level.isNaN() || level < 0 || level > 1) throw OpError("\"level\" must be between 0 and 1 or \"auto\"")
            Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL)
            Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS, Math.round(level * 255).toInt().coerceIn(1, 255))
        }
        val auto = Settings.System.getInt(cr, Settings.System.SCREEN_BRIGHTNESS_MODE, 0) == Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC
        val cur = Settings.System.getInt(cr, Settings.System.SCREEN_BRIGHTNESS, 128)
        return jsonOf("level" to cur / 255.0, "auto" to auto)
    }

    private fun openUrl(c: PhoneContext, url: String): Any? {
        val u = Uri.parse(url.trim())
        val scheme = u.scheme?.lowercase()
        if (scheme !in setOf("http", "https", "tel", "mailto", "geo", "sms", "smsto", "market")) {
            throw OpError("only http, https, tel, mailto, geo, sms and market links can be opened")
        }
        val intent = Intent(Intent.ACTION_VIEW, u).addCategory(Intent.CATEGORY_BROWSABLE)
        if (intent.resolveActivity(c.app.packageManager) == null && scheme != "http" && scheme != "https") {
            throw OpError("no app on the phone opens $scheme links", 404)
        }
        c.startFromBackground(intent)
        return null
    }
}
