package com.arstro.arstro_remote_client.features

import android.app.ActivityManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import org.json.JSONObject

/** Clipboard (PHN-13): set any time; read only while the app is on the screen (Android 10+ rule). */
object ClipboardFeature : Feature("clipboard", "Clipboard") {
    override fun ops() = mapOf(
        "clipboard.set" to op { c, a ->
            val text = a.str("text") ?: throw OpError("\"text\" is missing")
            if (text.length > 1_000_000) throw OpError("the text is too long for the clipboard")
            onMain {
                c.app.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("Arstro", text))
            }
            null
        },
        "clipboard.get" to op { c, _ -> get(c.app) },
    )

    private fun foreground(): Boolean {
        val info = ActivityManager.RunningAppProcessInfo()
        ActivityManager.getMyMemoryState(info)
        return info.importance <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
    }

    private fun get(app: Context): JSONObject {
        if (Build.VERSION.SDK_INT >= 29 && !foreground()) {
            throw OpError("Android lets an app read the clipboard only while it is on the screen - open Arstro Remote Client on the phone and try again", 409)
        }
        val text = onMain {
            val cm = app.getSystemService(ClipboardManager::class.java)
            val clip = cm.primaryClip
            if (clip == null || clip.itemCount == 0) null else clip.getItemAt(0).coerceToText(app)?.toString()
        }
        if (text == null && Build.VERSION.SDK_INT >= 29 && !foreground()) {
            throw OpError("Android lets an app read the clipboard only while it is on the screen - open Arstro Remote Client on the phone and try again", 409)
        }
        return jsonOf("text" to (text ?: ""))
    }
}
