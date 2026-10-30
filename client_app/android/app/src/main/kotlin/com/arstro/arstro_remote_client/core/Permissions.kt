package com.arstro.arstro_remote_client.core

import android.Manifest
import android.app.NotificationManager
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

/**
 * What a feature (or the service) still needs from Android: runtime permissions (asked with the
 * system dialog) and special accesses (granted on a system settings page). PHONE.md "Features".
 */
object Permissions {
    const val CONTROL_SERVICE = "com.arstro.arstro_remote_client.features.ControlService"
    const val ADMIN_RECEIVER = "com.arstro.arstro_remote_client.features.AdminReceiver"
    const val NOTIF_LISTENER = "com.arstro.arstro_remote_client.features.NotifListener"

    /** One thing that is missing: `name` = the permission (android.permission.X) or the SpecialAccess name. */
    class Need(val name: String, val kind: String, val title: String) {
        fun toMap(): Map<String, Any> = mapOf("name" to name, "kind" to kind, "title" to title)
    }

    private val titles = mapOf(
        Manifest.permission.CAMERA to "Camera",
        Manifest.permission.RECORD_AUDIO to "Microphone",
        "android.permission.POST_NOTIFICATIONS" to "Notifications",
        Manifest.permission.READ_SMS to "Read SMS",
        Manifest.permission.SEND_SMS to "Send SMS",
        Manifest.permission.RECEIVE_SMS to "Receive SMS",
        Manifest.permission.READ_CONTACTS to "Contacts",
        Manifest.permission.READ_CALL_LOG to "Call log",
        Manifest.permission.ACCESS_FINE_LOCATION to "Precise location",
        Manifest.permission.ACCESS_COARSE_LOCATION to "Location",
        "android.permission.ACCESS_BACKGROUND_LOCATION" to "Location all the time",
        Manifest.permission.READ_PHONE_STATE to "Phone state",
        Manifest.permission.READ_EXTERNAL_STORAGE to "Storage",
        Manifest.permission.WRITE_EXTERNAL_STORAGE to "Storage (write)",
    )

    fun title(name: String): String =
        titles[name] ?: runCatching { SpecialAccess.valueOf(name).title }.getOrNull()
        ?: name.substringAfterLast('.').lowercase().replace('_', ' ').replaceFirstChar { it.uppercase() }

    /** Whether Android has this runtime permission on this version (else it counts as granted). */
    fun applies(perm: String): Boolean = when (perm) {
        "android.permission.POST_NOTIFICATIONS" -> Build.VERSION.SDK_INT >= 33
        "android.permission.ACCESS_BACKGROUND_LOCATION" -> Build.VERSION.SDK_INT >= 29
        Manifest.permission.READ_EXTERNAL_STORAGE -> Build.VERSION.SDK_INT <= 32
        Manifest.permission.WRITE_EXTERNAL_STORAGE -> Build.VERSION.SDK_INT <= 29
        else -> true
    }

    fun granted(ctx: Context, perm: String): Boolean =
        !applies(perm) || ContextCompat.checkSelfPermission(ctx, perm) == PackageManager.PERMISSION_GRANTED

    fun granted(ctx: Context, access: SpecialAccess): Boolean = try {
        when (access) {
            SpecialAccess.SCREEN_CAPTURE -> true      // asked per session by the screen feature
            SpecialAccess.ACCESSIBILITY -> accessibilityOn(ctx)
            SpecialAccess.NOTIFICATION_LISTENER -> NotificationManagerCompat.getEnabledListenerPackages(ctx).contains(ctx.packageName)
            SpecialAccess.ALL_FILES ->
                if (Build.VERSION.SDK_INT >= 30) Environment.isExternalStorageManager()
                else granted(ctx, Manifest.permission.READ_EXTERNAL_STORAGE) && granted(ctx, Manifest.permission.WRITE_EXTERNAL_STORAGE)
            SpecialAccess.OVERLAY -> Settings.canDrawOverlays(ctx)
            SpecialAccess.DND -> nm(ctx).isNotificationPolicyAccessGranted
            SpecialAccess.DEVICE_ADMIN ->
                (ctx.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager)
                    .isAdminActive(ComponentName(ctx.packageName, ADMIN_RECEIVER))
            SpecialAccess.WRITE_SETTINGS -> Settings.System.canWrite(ctx)
            SpecialAccess.FULL_SCREEN_INTENT -> Build.VERSION.SDK_INT < 34 || nm(ctx).canUseFullScreenIntent()
            SpecialAccess.BATTERY_UNRESTRICTED ->
                (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(ctx.packageName)
        }
    } catch (e: Exception) {
        false
    }

    private fun nm(ctx: Context) = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    fun accessibilityOn(ctx: Context): Boolean {
        val list = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        return list.split(':').any { item ->
            val c = ComponentName.unflattenFromString(item.trim()) ?: return@any false
            c.packageName == ctx.packageName && c.className == CONTROL_SERVICE
        }
    }

    /** What a feature still needs, in the order to ask for it. */
    fun missing(ctx: Context, f: Feature): List<Need> {
        val out = ArrayList<Need>()
        for (p in f.permissions) if (!granted(ctx, p)) out.add(Need(p, "permission", title(p)))
        if (SpecialAccess.ALL_FILES in f.specialAccess && Build.VERSION.SDK_INT < 30) {
            for (p in listOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.WRITE_EXTERNAL_STORAGE)) {
                if (!granted(ctx, p) && out.none { it.name == p }) out.add(Need(p, "permission", title(p)))
            }
        }
        for (a in f.specialAccess) {
            if (a == SpecialAccess.ALL_FILES && Build.VERSION.SDK_INT < 30) continue
            if (!granted(ctx, a)) out.add(Need(a.name, "special", a.title))
        }
        return out
    }

    /** What the service itself needs: its notification and unrestricted battery use. */
    fun serviceMissing(ctx: Context): List<Need> {
        val out = ArrayList<Need>()
        val n = "android.permission.POST_NOTIFICATIONS"
        if (!granted(ctx, n)) out.add(Need(n, "permission", title(n)))
        if (!granted(ctx, SpecialAccess.BATTERY_UNRESTRICTED)) {
            out.add(Need(SpecialAccess.BATTERY_UNRESTRICTED.name, "special", SpecialAccess.BATTERY_UNRESTRICTED.title))
        }
        return out
    }

    private fun pkgUri(ctx: Context) = Uri.parse("package:" + ctx.packageName)

    /** The settings page that grants a special access (null: nothing to open on this version). */
    fun intentFor(ctx: Context, access: SpecialAccess): Intent? = when (access) {
        SpecialAccess.SCREEN_CAPTURE -> null
        SpecialAccess.ACCESSIBILITY -> Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
        SpecialAccess.NOTIFICATION_LISTENER ->
            if (Build.VERSION.SDK_INT >= 30) Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
                .putExtra(Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME, ComponentName(ctx.packageName, NOTIF_LISTENER).flattenToString())
            else Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS")
        SpecialAccess.ALL_FILES ->
            if (Build.VERSION.SDK_INT >= 30) Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, pkgUri(ctx)) else null
        SpecialAccess.OVERLAY -> Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, pkgUri(ctx))
        SpecialAccess.DND -> Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS)
        SpecialAccess.DEVICE_ADMIN -> Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
            .putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, ComponentName(ctx.packageName, ADMIN_RECEIVER))
            .putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION,
                      "Lets \"Find my phone\" lock this phone when you mark it lost. Nothing else.")
        SpecialAccess.WRITE_SETTINGS -> Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS, pkgUri(ctx))
        SpecialAccess.FULL_SCREEN_INTENT ->
            if (Build.VERSION.SDK_INT >= 34) Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, pkgUri(ctx)) else null
        SpecialAccess.BATTERY_UNRESTRICTED ->
            @Suppress("BatteryLife") Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, pkgUri(ctx))
    }

    /** A fallback when the precise page does not exist on this phone. */
    fun fallbackIntent(ctx: Context, access: SpecialAccess): Intent = when (access) {
        SpecialAccess.NOTIFICATION_LISTENER -> Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS")
        SpecialAccess.ALL_FILES -> Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)
        SpecialAccess.BATTERY_UNRESTRICTED -> Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
        SpecialAccess.OVERLAY -> Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION)
        else -> appInfoIntent(ctx)
    }

    fun appInfoIntent(ctx: Context) = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, pkgUri(ctx))
}
