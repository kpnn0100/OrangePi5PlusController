package com.arstro.arstro_remote_client.features

import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Canvas
import android.util.LruCache
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import com.arstro.arstro_remote_client.core.Response
import com.arstro.arstro_remote_client.core.SpecialAccess
import java.io.ByteArrayOutputStream
import org.json.JSONArray
import org.json.JSONObject

/** Apps (PHN-14): the launchable apps, their icons, open one. */
object AppsFeature : Feature("apps", "Apps") {
    override val specialAccess = listOf(SpecialAccess.OVERLAY)

    private val icons by lazy {
        object : LruCache<String, ByteArray>(4 * 1024 * 1024) {
            override fun sizeOf(key: String, value: ByteArray) = value.size
        }
    }

    override fun ops() = mapOf(
        "apps.list" to op { c, _ -> list(c.app) },
        "apps.open" to op { c, a -> open(c, a.needStr("package")) },
        "apps.icon" to op { c, a -> iconResponse(c.app, a.needStr("package"), a.int("size")) },
    )

    override fun routes() = mapOf(
        "GET /api/apps/icon" to route { c, req ->
            iconResponse(c.app, req.param("package") ?: throw OpError("\"package\" is missing"), req.param("size")?.toIntOrNull())
        },
    )

    private fun list(app: Context): JSONArray {
        val pm = app.packageManager
        val main = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        @Suppress("DEPRECATION")
        val acts = pm.queryIntentActivities(main, 0)
        val seen = LinkedHashMap<String, JSONObject>()
        for (ri in acts) {
            val ai = ri.activityInfo?.applicationInfo ?: continue
            if (seen.containsKey(ai.packageName)) continue
            seen[ai.packageName] = JSONObject().put("package", ai.packageName)
                .put("label", ai.loadLabel(pm).toString())
                .put("system", ai.flags and ApplicationInfo.FLAG_SYSTEM != 0)
        }
        return JSONArray(seen.values.sortedBy { it.getString("label").lowercase() })
    }

    private fun open(c: PhoneContext, pkg: String): Any? {
        val intent = c.app.packageManager.getLaunchIntentForPackage(pkg)
            ?: throw OpError("$pkg is not installed or has nothing to open", 404)
        c.startFromBackground(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED))
        return null
    }

    private fun iconResponse(app: Context, pkg: String, size: Int?): Response {
        val px = (size ?: 96).coerceIn(16, 512)
        val key = "$pkg@$px"
        val png = icons.get(key) ?: run {
            val d = try { app.packageManager.getApplicationIcon(pkg) } catch (e: PackageManager.NameNotFoundException) {
                throw OpError("$pkg is not installed", 404)
            }
            val bmp = Bitmap.createBitmap(px, px, Bitmap.Config.ARGB_8888)
            val canvas = Canvas(bmp)
            d.setBounds(0, 0, px, px)
            d.draw(canvas)
            val out = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.PNG, 100, out)
            bmp.recycle()
            out.toByteArray().also { icons.put(key, it) }
        }
        return Response.bytes(png, "image/png", 200, listOf("Cache-Control" to "max-age=86400"))
    }
}
