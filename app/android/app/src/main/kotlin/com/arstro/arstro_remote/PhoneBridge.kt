package com.arstro.arstro_remote

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.net.Uri
import android.net.wifi.WifiNetworkSuggestion
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.provider.Settings
import android.util.Log
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File

private const val TAG = "ArstroPhone"
private const val REQ_JOIN_WIFI = 4711

/**
 * What the app asks of the phone itself - never a connection to the machine (Bluetooth only,
 * CON-06):
 *
 *   openUrl {url} -> bool              the phone's browser (e.g. the web UI inside the hotspot)
 *   joinWifi {ssid, password} -> str   the machine's hotspot (WIFI-08) offered to the phone's Wi-Fi:
 *                                      "saved" | "cancelled" (Android 11+ asks), "settings" (older)
 *   saveText {name, text, mime} -> "Downloads/Arstro/<name>"   the editor's CSV (GAL-13)
 */
class PhoneBridge(private val activity: Activity, messenger: BinaryMessenger) {
    private val method = MethodChannel(messenger, "arstro/phone")
    private val main = Handler(Looper.getMainLooper())
    private var pendingJoin: MethodChannel.Result? = null

    init {
        method.setMethodCallHandler { call, result ->
            when (call.method) {
                "openUrl" -> openUrl(call, result)
                "joinWifi" -> joinWifi(call, result)
                "saveText" -> saveText(call, result)
                else -> result.notImplemented()
            }
        }
    }

    private fun openUrl(call: MethodCall, result: MethodChannel.Result) {
        val url = call.argument<String>("url") ?: return result.success(false)
        try {
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE))
            result.success(true)
        } catch (e: ActivityNotFoundException) {
            result.success(false)
        }
    }

    private fun joinWifi(call: MethodCall, result: MethodChannel.Result) {
        val ssid = call.argument<String>("ssid") ?: return result.error("joinWifi", "no network name", null)
        val password = call.argument<String>("password") ?: ""
        try {
            if (Build.VERSION.SDK_INT >= 30) {
                val suggestion = WifiNetworkSuggestion.Builder().setSsid(ssid).setWpa2Passphrase(password).build()
                val intent = Intent(Settings.ACTION_WIFI_ADD_NETWORKS)
                    .putParcelableArrayListExtra(Settings.EXTRA_WIFI_NETWORK_LIST, arrayListOf(suggestion))
                pendingJoin?.success("cancelled")
                pendingJoin = result
                @Suppress("DEPRECATION")
                activity.startActivityForResult(intent, REQ_JOIN_WIFI)
            } else {
                activity.startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
                result.success("settings")
            }
        } catch (e: Exception) {
            pendingJoin = null
            Log.w(TAG, "joinWifi", e)
            try {
                activity.startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
                result.success("settings")
            } catch (e2: Exception) {
                result.error("joinWifi", e.message, null)
            }
        }
    }

    /** True when it was our request. */
    fun onActivityResult(requestCode: Int, resultCode: Int): Boolean {
        if (requestCode != REQ_JOIN_WIFI) return false
        pendingJoin?.success(if (resultCode == Activity.RESULT_OK) "saved" else "cancelled")
        pendingJoin = null
        return true
    }

    private fun saveText(call: MethodCall, result: MethodChannel.Result) {
        val name = (call.argument<String>("name") ?: "arstro.txt").replace('/', '_')
        val text = call.argument<String>("text") ?: ""
        val mime = call.argument<String>("mime") ?: "text/plain"
        val context = activity.applicationContext
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

    fun dispose() {
        method.setMethodCallHandler(null)
        pendingJoin?.success("cancelled")
        pendingJoin = null
    }
}
