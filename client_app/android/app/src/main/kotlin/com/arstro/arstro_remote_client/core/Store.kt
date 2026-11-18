package com.arstro.arstro_remote_client.core

import android.content.Context
import android.content.SharedPreferences
import android.os.Build

/**
 * The phone's settings (SharedPreferences "arstro_phone", private to the app): its identity at the
 * device manager (id, secret, access key), the device manager settings, the service switch and
 * the feature switches. Never logged.
 */
class Store(context: Context) {
    companion object {
        const val DEFAULT_URL = "https://arstro.cngthnh.io.vn/arstro/internal/devices"
        private val ID_RE = Regex("^[a-z0-9][a-z0-9-]{2,39}$")
        private const val ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"

        /** `<model>-phone-<6 random>`, matching ^[a-z0-9][a-z0-9-]{2,39}$. */
        fun newId(model: String): String {
            val tail = "-phone-" + (1..6).map { ALNUM[E2e.randomBytes(1)[0].toInt().and(0xff) % ALNUM.length] }.joinToString("")
            var slug = model.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-')
            if (slug.isEmpty()) slug = "android"
            slug = slug.take(40 - tail.length).trimEnd('-')
            if (slug.isEmpty()) slug = "android"
            val id = slug + tail
            return if (ID_RE.matches(id)) id else "android$tail"
        }

        const val WEB_PORT = 8080
        const val TLS_PORT = 8443
        private const val EASY = "abcdefghjkmnpqrstuvwxyz23456789"

        fun newPassword(): String = (1..10).map { EASY[E2e.randomBytes(1)[0].toInt().and(0xff) % EASY.length] }.joinToString("")

        fun defaultName(): String {
            val m = Build.MANUFACTURER.orEmpty().replaceFirstChar { it.uppercase() }
            val model = Build.MODEL.orEmpty()
            return (if (model.startsWith(m, ignoreCase = true)) model else "$m $model").trim().ifEmpty { "Android phone" }.take(64)
        }
    }

    private val p: SharedPreferences = context.applicationContext.getSharedPreferences("arstro_phone", Context.MODE_PRIVATE)

    @Synchronized private fun ensure(key: String, make: () -> String): String =
        p.getString(key, null) ?: make().also { p.edit().putString(key, it).apply() }

    // ------------------------------------------------------------------ identity
    val id: String get() = ensure("id") { newId(Build.MODEL.orEmpty()) }
    val secret: String get() = ensure("secret") { E2e.b64e(E2e.randomBytes(32)) }
    val key: String get() = ensure("key") { E2e.newKey() }
    val keyRaw: ByteArray get() = E2e.parseKey(key)
    val keyId: String get() = E2e.keyId(keyRaw)

    /** A new access key; viewers with the old one cannot connect any more. */
    @Synchronized fun rotateKey(): String = E2e.newKey().also { p.edit().putString("key", it).commit() }

    // ------------------------------------------------------------------ device manager
    var url: String
        get() = p.getString("url", null) ?: DEFAULT_URL
        set(v) = p.edit().putString("url", v.trim().trimEnd('/')).apply()
    var token: String
        get() = p.getString("token", null) ?: ""
        set(v) = p.edit().putString("token", v.trim()).apply()
    var name: String
        get() = p.getString("name", null)?.takeIf { it.isNotBlank() } ?: defaultName()
        set(v) = p.edit().putString("name", v.trim().filter { !it.isISOControl() }.take(64)).apply()

    /** Reach the phone through a device manager as well (off: only on the phone's own addresses).
     *  On for phones set up before 2.0 that had a join token. */
    var relayOn: Boolean
        get() = p.getBoolean("relay_on", token.isNotEmpty())
        set(v) = p.edit().putBoolean("relay_on", v).apply()

    // ------------------------------------------------------------------ the phone's own web server
    /** http://<phone>:8080/ and https://<phone>:8443/ (PHN-20). On unless turned off. */
    var webOn: Boolean
        get() = p.getBoolean("web_on", true)
        set(v) = p.edit().putBoolean("web_on", v).apply()

    /** The web login password: made on first use (10 easy-to-read characters), changeable in the app. */
    var webPassword: String
        get() = ensure("web_password") { newPassword() }
        set(v) = p.edit().putString("web_password", v).remove("web_sessions").commit().let { }

    /** Logged-in browsers: sha256(token) -> expiry ms (the tokens themselves are never stored). */
    var webSessions: Map<String, Long>
        get() = (p.getStringSet("web_sessions", null) ?: emptySet()).mapNotNull {
            val i = it.indexOf(':'); if (i <= 0) null else it.substring(0, i) to (it.substring(i + 1).toLongOrNull() ?: 0L)
        }.toMap()
        set(v) = p.edit().putStringSet("web_sessions", v.map { "${it.key}:${it.value}" }.toSet()).apply()

    /** The phone's TLS identity (SelfSigned), made once. */
    val tls: SelfSigned.Identity
        @Synchronized get() {
            val k = p.getString("tls_key", null)
            val c = p.getString("tls_cert", null)
            if (k != null && c != null) try {
                return SelfSigned.Identity(java.util.Base64.getDecoder().decode(k), java.util.Base64.getDecoder().decode(c))
            } catch (e: Exception) { }
            val id = SelfSigned.create(name)
            p.edit().putString("tls_key", java.util.Base64.getEncoder().encodeToString(id.keyPkcs8))
                .putString("tls_cert", java.util.Base64.getEncoder().encodeToString(id.certDer)).commit()
            return id
        }

    // ------------------------------------------------------------------ switches
    /** The service runs (and starts at boot) while this is on. */
    var enabled: Boolean
        get() = p.getBoolean("enabled", false)
        set(v) = p.edit().putBoolean("enabled", v).apply()
    var firstRun: Boolean
        get() = p.getBoolean("first_run", true)
        set(v) = p.edit().putBoolean("first_run", v).apply()
    /** Keep the CPU awake while sharing, so the device manager reaches the phone also while it sleeps
     *  (off: only while someone is connected). On unless turned off. */
    var awake: Boolean
        get() = p.getBoolean("awake", true)
        set(v) = p.edit().putBoolean("awake", v).apply()

    /** Feature switches: all off until the wizard (or the page) turns them on. */
    fun isOn(featureId: String): Boolean = p.getBoolean("feature.$featureId", false)
    fun setOn(featureId: String, on: Boolean) = p.edit().putBoolean("feature.$featureId", on).apply()

    fun listen(l: SharedPreferences.OnSharedPreferenceChangeListener) = p.registerOnSharedPreferenceChangeListener(l)
    fun unlisten(l: SharedPreferences.OnSharedPreferenceChangeListener) = p.unregisterOnSharedPreferenceChangeListener(l)
}
