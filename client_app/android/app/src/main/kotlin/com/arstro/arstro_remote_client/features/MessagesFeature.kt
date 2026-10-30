package com.arstro.arstro_remote_client.features

import android.Manifest
import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Telephony
import android.telephony.SmsManager
import android.util.Log
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Messages (PHONE.md "messages", PHN-08): SMS threads and messages from the Telephony provider (names
 * through the contacts when that permission is there), sending through SmsManager (long texts in parts,
 * waits for the radio's answer), new messages pushed as event `sms` while the feature is on.
 * Never logs numbers or texts.
 */
object MessagesFeature : Feature("messages", "Messages (SMS)") {
    override val permissions = listOf(Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS, Manifest.permission.RECEIVE_SMS)

    @Volatile private var ctx: PhoneContext? = null

    override fun ops() = mapOf(
        "messages.threads" to op { c, a -> threads(c.app, a.limit(50, 500)) },
        "messages.list" to op { c, a -> list(c.app, a.str("thread")?.toLongOrNull(), a.str("address"), a.limit(100, 2000)) },
        "messages.send" to op { c, a -> send(c, a.needStr("to"), a.needStr("text")) },
    )

    override fun start(ctx: PhoneContext) { this.ctx = ctx }
    override fun stop(ctx: PhoneContext) { this.ctx = null }

    /** SmsReceiver: a new SMS arrived. */
    fun incoming(app: Context, address: String, body: String, date: Long) {
        val c = ctx ?: return
        if (!c.isOn(id)) return
        val o = JSONObject().put("address", address).put("body", body).put("date", date)
        ContactNames(app).name(address)?.let { o.put("name", it) }
        c.events.event("sms", o)
    }

    private fun threads(app: Context, limit: Int): JSONArray {
        val cr = app.contentResolver
        val unread = HashMap<Long, Int>()
        cr.query(Telephony.Sms.Inbox.CONTENT_URI, arrayOf(Telephony.Sms.THREAD_ID), Telephony.Sms.READ + " = 0", null, null)?.use { c ->
            while (c.moveToNext()) unread.merge(c.getLong(0), 1, Int::plus)
        }
        val names = ContactNames(app)
        val seen = LinkedHashMap<Long, JSONObject>()
        cr.query(Telephony.Sms.CONTENT_URI,
                 arrayOf(Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE),
                 null, null, Telephony.Sms.DATE + " DESC")?.use { c ->
            while (c.moveToNext() && seen.size < limit) {
                val tid = c.getLong(0)
                if (seen.containsKey(tid)) continue
                val address = c.getString(1) ?: ""
                val o = JSONObject().put("thread", tid).put("address", address)
                    .put("snippet", (c.getString(2) ?: "").take(200)).put("date", c.getLong(3)).put("unread", unread[tid] ?: 0)
                names.name(address)?.let { o.put("name", it) }
                seen[tid] = o
            }
        }
        return JSONArray(seen.values)
    }

    /** Newest first. */
    private fun list(app: Context, thread: Long?, address: String?, limit: Int): JSONArray {
        val cols = arrayOf(Telephony.Sms._ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE,
                           Telephony.Sms.TYPE, Telephony.Sms.READ)
        val order = Telephony.Sms.DATE + " DESC"
        val cr = app.contentResolver
        val cursor = when {
            thread != null -> cr.query(Telephony.Sms.CONTENT_URI, cols, Telephony.Sms.THREAD_ID + " = ?", arrayOf(thread.toString()), order)
            !address.isNullOrBlank() -> try {
                cr.query(Telephony.Sms.CONTENT_URI, cols, "PHONE_NUMBERS_EQUAL(" + Telephony.Sms.ADDRESS + ", ?)", arrayOf(address), order)
            } catch (e: Exception) {
                cr.query(Telephony.Sms.CONTENT_URI, cols, Telephony.Sms.ADDRESS + " = ?", arrayOf(address), order)
            }
            else -> cr.query(Telephony.Sms.CONTENT_URI, cols, null, null, order)
        }
        val out = JSONArray()
        val names = ContactNames(app)
        cursor?.use { c ->
            while (c.moveToNext() && out.length() < limit) {
                val addr = c.getString(1) ?: ""
                val o = JSONObject().put("id", c.getLong(0)).put("address", addr).put("body", c.getString(2) ?: "")
                    .put("date", c.getLong(3))
                    .put("type", if (c.getInt(4) == Telephony.Sms.MESSAGE_TYPE_INBOX) "in" else "out")
                    .put("read", c.getInt(5) != 0)
                if (c.getInt(4) == Telephony.Sms.MESSAGE_TYPE_FAILED) o.put("failed", true)
                names.name(addr)?.let { o.put("name", it) }
                out.put(o)
            }
        }
        return out
    }

    private fun send(c: PhoneContext, to: String, text: String): JSONObject {
        val app = c.app
        if (!Regex("""^[+0-9 ()\-.*#A-Za-z]{2,40}$""").matches(to.trim())) throw OpError("\"to\" is not a phone number")
        if (text.length > 5000) throw OpError("the text is too long (at most 5000 characters)")
        if (!app.packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY)) {
            throw OpError("this phone cannot send SMS (no mobile network)", 409)
        }
        @Suppress("DEPRECATION")
        val sm: SmsManager = (if (Build.VERSION.SDK_INT >= 31) app.getSystemService(SmsManager::class.java) else null)
            ?: SmsManager.getDefault()
        val parts = sm.divideMessage(text)
        val action = "com.arstro.arstro_remote_client.SMS_SENT." + UUID.randomUUID()
        val latch = CountDownLatch(parts.size)
        var error: Int? = null
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                if (resultCode != Activity.RESULT_OK && error == null) error = resultCode
                latch.countDown()
            }
        }
        app.registerLocal(receiver, IntentFilter(action))
        val confirmed: Boolean
        try {
            val sent = ArrayList(parts.indices.map { i ->
                PendingIntent.getBroadcast(app, i, Intent(action).setPackage(app.packageName), PI_FLAGS)
            })
            if (parts.size == 1) sm.sendTextMessage(to.trim(), null, text, sent[0], null)
            else sm.sendMultipartTextMessage(to.trim(), null, parts, sent, null)
            confirmed = latch.await(60, TimeUnit.SECONDS)
        } finally {
            app.unregisterQuietly(receiver)
        }
        error?.let { code ->
            c.log(Log.INFO, "messages: send failed ($code)")
            throw OpError(when (code) {
                SmsManager.RESULT_ERROR_RADIO_OFF -> "the SMS was not sent - the phone's radio is off (airplane mode?)"
                SmsManager.RESULT_ERROR_NO_SERVICE -> "the SMS was not sent - no mobile service"
                SmsManager.RESULT_ERROR_NULL_PDU -> "the SMS was not sent - the phone could not encode it"
                else -> "the SMS was not sent - the mobile network refused it (error $code)"
            }, 502)
        }
        c.log(Log.INFO, "messages: sent (${parts.size} part(s))")
        return jsonOf("sent" to true, "confirmed" to confirmed, "parts" to parts.size)
    }
}

/** New SMS (PHONE.md event `sms`): passed to MessagesFeature, which forwards it only while the feature is on. */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val msgs = try { Telephony.Sms.Intents.getMessagesFromIntent(intent) } catch (e: Exception) { null } ?: return
        val byAddress = LinkedHashMap<String, Pair<StringBuilder, Long>>()
        for (m in msgs) {
            if (m == null) continue
            val addr = m.originatingAddress ?: m.displayOriginatingAddress ?: continue
            val e = byAddress.getOrPut(addr) { StringBuilder() to m.timestampMillis }
            e.first.append(m.messageBody ?: "")
        }
        for ((addr, e) in byAddress) {
            try { MessagesFeature.incoming(context.applicationContext, addr, e.first.toString(), e.second) } catch (x: Exception) { }
        }
    }
}
