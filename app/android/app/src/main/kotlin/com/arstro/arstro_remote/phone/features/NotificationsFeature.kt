package com.arstro.arstro_remote.phone.features

import android.app.Notification
import android.app.RemoteInput
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import com.arstro.arstro_remote.phone.core.Feature
import com.arstro.arstro_remote.phone.core.OpError
import com.arstro.arstro_remote.phone.core.PhoneContext
import com.arstro.arstro_remote.phone.core.SpecialAccess
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/**
 * Notifications (PHONE.md "notifications", PHN-12): the phone's current notifications through
 * NotifListener, dismiss, reply through the notification's own reply action; events `notification`
 * (posted / removed) while the feature is on. Never logs titles or texts.
 */
object NotificationsFeature : Feature("notifications", "Notifications") {
    override val specialAccess = listOf(SpecialAccess.NOTIFICATION_LISTENER)

    @Volatile private var ctx: PhoneContext? = null
    private val labels = ConcurrentHashMap<String, String>()

    override fun ops() = mapOf(
        "notifications.list" to op { c, _ -> list(c.app) },
        "notifications.dismiss" to op { _, a -> dismiss(a.needStr("key")) },
        "notifications.reply" to op { c, a -> reply(c.app, a.needStr("key"), a.needStr("text")) },
    )

    override fun start(ctx: PhoneContext) {
        this.ctx = ctx
        if (NotifListener.instance == null) {
            try { NotificationListenerService.requestRebind(ComponentName(ctx.app, NotifListener::class.java)) } catch (e: Exception) { }
        }
    }

    override fun stop(ctx: PhoneContext) { this.ctx = null }

    override fun note(ctx: PhoneContext): String? =
        if (NotifListener.instance == null) "notification access is not connected yet" else null

    private fun listener(): NotifListener = NotifListener.instance
        ?: throw OpError("notification access is not connected - turn Notification access for Arstro Remote off and on again", 409)

    private fun label(app: Context, pkg: String): String = labels.getOrPut(pkg) {
        try {
            val pm = app.packageManager
            pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
        } catch (e: PackageManager.NameNotFoundException) { pkg }
    }

    private fun text(n: Notification): Pair<String, String> {
        val e: Bundle = n.extras ?: Bundle()
        val title = (e.getCharSequence(Notification.EXTRA_TITLE_BIG) ?: e.getCharSequence(Notification.EXTRA_TITLE))?.toString() ?: ""
        var body = (e.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: e.getCharSequence(Notification.EXTRA_TEXT))?.toString() ?: ""
        if (body.isEmpty()) {
            @Suppress("DEPRECATION")
            val msgs = e.getParcelableArray(Notification.EXTRA_MESSAGES)
            val last = msgs?.lastOrNull() as? Bundle
            body = last?.getCharSequence("text")?.toString() ?: ""
        }
        if (body.isEmpty()) body = e.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)?.joinToString("\n") ?: ""
        return title to body
    }

    private fun replyAction(n: Notification): Notification.Action? =
        n.actions?.firstOrNull { a -> a.remoteInputs?.any { it.allowFreeFormInput } == true }

    private fun json(app: Context, sbn: StatusBarNotification): JSONObject {
        val n = sbn.notification
        val (title, body) = text(n)
        return JSONObject().put("key", sbn.key).put("package", sbn.packageName).put("app", label(app, sbn.packageName))
            .put("title", title).put("text", body).put("time", sbn.postTime)
            .put("can_reply", replyAction(n) != null)
            .put("clearable", sbn.isClearable)
            .put("actions", JSONArray((n.actions ?: emptyArray()).map { it.title?.toString() ?: "" }))
    }

    private fun shown(sbn: StatusBarNotification): Boolean {
        val n = sbn.notification
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return false
        val (title, body) = text(n)
        return title.isNotEmpty() || body.isNotEmpty()
    }

    private fun list(app: Context): JSONArray {
        val l = listener()
        val active = try { l.activeNotifications } catch (e: Exception) { null }
            ?: throw OpError("notification access is not connected - turn Notification access for Arstro Remote off and on again", 409)
        return JSONArray(active.filter { shown(it) }.sortedByDescending { it.postTime }.map { json(app, it) })
    }

    private fun find(key: String): StatusBarNotification =
        listener().activeNotifications?.firstOrNull { it.key == key } ?: throw OpError("that notification is gone", 404)

    private fun dismiss(key: String): Any? {
        val sbn = find(key)
        if (!sbn.isClearable) throw OpError("this notification cannot be dismissed (the app keeps it)", 409)
        listener().cancelNotification(key)
        return null
    }

    private fun reply(app: Context, key: String, text: String): Any? {
        val sbn = find(key)
        val action = replyAction(sbn.notification)
            ?: throw OpError("nothing to reply to - the notification has no reply action", 409)
        val inputs = action.remoteInputs ?: throw OpError("nothing to reply to - the notification has no reply action", 409)
        val intent = Intent()
        val results = Bundle()
        for (ri in inputs) if (ri.allowFreeFormInput) results.putCharSequence(ri.resultKey, text)
        RemoteInput.addResultsToIntent(inputs, intent, results)
        if (Build.VERSION.SDK_INT >= 28) RemoteInput.setResultsSource(intent, RemoteInput.SOURCE_FREE_FORM_INPUT)
        try {
            action.actionIntent.send(app, 0, intent)
        } catch (e: Exception) {
            throw OpError("the app did not take the reply (${e.javaClass.simpleName})", 409)
        }
        return null
    }

    // ------------------------------------------------------------------------------------- from NotifListener

    fun posted(app: Context, sbn: StatusBarNotification) {
        val c = ctx ?: return
        if (!c.isOn(id) || !shown(sbn) || sbn.packageName == app.packageName) return
        val o = json(app, sbn)
        c.events.event("notification", JSONObject().put("key", o.getString("key")).put("package", sbn.packageName)
            .put("app", o.getString("app")).put("title", o.getString("title")).put("text", o.getString("text"))
            .put("time", sbn.postTime).put("can_reply", o.getBoolean("can_reply")))
    }

    fun removed(sbn: StatusBarNotification) {
        val c = ctx ?: return
        if (!c.isOn(id)) return
        c.events.event("notification", JSONObject().put("key", sbn.key).put("removed", true))
    }
}

/** The notification listener (system "Notification access"): hands everything to NotificationsFeature. */
class NotifListener : NotificationListenerService() {
    companion object {
        @Volatile var instance: NotifListener? = null
            private set
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        instance = this
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
        super.onListenerDisconnected()
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn != null) try { NotificationsFeature.posted(applicationContext, sbn) } catch (e: Exception) { }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        if (sbn != null) try { NotificationsFeature.removed(sbn) } catch (e: Exception) { }
    }
}
