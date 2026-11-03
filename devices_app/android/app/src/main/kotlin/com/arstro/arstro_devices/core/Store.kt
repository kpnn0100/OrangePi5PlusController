package com.arstro.arstro_devices.core

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/*
 * What the app remembers (MGR-01, MGR-02, MGR-05):
 *   servers   - the device managers: id, url, name, version (no secrets)
 *   secrets   - viewer passwords and access keys, AES-GCM encrypted with a key that lives in the
 *               Android Keystore (it never leaves it; app data alone does not give the keys)
 *   recents   - per server: the ports that answered, newest first
 *   origins   - per device and port: the secret host label and the last local port (a stable
 *               origin keeps the device page's login and storage)
 * The plain JSON never holds a key or a password. android:allowBackup is off.
 */
class Store(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("arstro_devices", Context.MODE_PRIVATE)
    private val vault: SharedPreferences = context.getSharedPreferences("arstro_devices_vault", Context.MODE_PRIVATE)

    companion object {
        private const val ALIAS = "arstro_devices_vault"
        const val MAX_RECENTS = 40
    }

    // ------------------------------------------------------------------ servers
    @Synchronized fun servers(): JSONArray = JSONArray(prefs.getString("servers", "[]"))

    @Synchronized fun server(id: String): JSONObject? {
        val a = servers()
        for (i in 0 until a.length()) if (a.getJSONObject(i).optString("id") == id) return a.getJSONObject(i)
        return null
    }

    @Synchronized fun putServer(s: JSONObject) {
        val a = servers()
        val out = JSONArray()
        var replaced = false
        for (i in 0 until a.length()) {
            val o = a.getJSONObject(i)
            if (o.optString("id") == s.optString("id")) { out.put(s); replaced = true } else out.put(o)
        }
        if (!replaced) out.put(s)
        prefs.edit().putString("servers", out.toString()).apply()
    }

    @Synchronized fun removeServer(id: String) {
        val a = servers()
        val out = JSONArray()
        for (i in 0 until a.length()) if (a.getJSONObject(i).optString("id") != id) out.put(a.getJSONObject(i))
        val e = prefs.edit().putString("servers", out.toString()).remove("recents:$id")
        for (k in prefs.all.keys) if (k.startsWith("origin:$id/") || k.startsWith("kid:$id/")) e.remove(k)
        e.apply()
        val v = vault.edit()
        for (k in vault.all.keys) if (k == "pw:$id" || k.startsWith("key:$id/")) v.remove(k)
        v.apply()
        if (current() == id) setCurrent(if (out.length() > 0) out.getJSONObject(0).optString("id") else null)
    }

    fun current(): String? = prefs.getString("current", null)
    fun setCurrent(id: String?) = prefs.edit().apply { if (id == null) remove("current") else putString("current", id) }.apply()

    // ------------------------------------------------------------------ secrets
    fun password(server: String): String? = open(vault.getString("pw:$server", null))
    fun setPassword(server: String, pw: String?) =
        vault.edit().apply { if (pw.isNullOrEmpty()) remove("pw:$server") else putString("pw:$server", seal(pw)) }.apply()

    fun key(server: String, device: String): String? = open(vault.getString("key:$server/$device", null))
    fun keyId(server: String, device: String): String? = prefs.getString("kid:$server/$device", null)

    fun setKey(server: String, device: String, key: String?) {
        if (key == null) {
            vault.edit().remove("key:$server/$device").apply()
            prefs.edit().remove("kid:$server/$device").apply()
        } else {
            vault.edit().putString("key:$server/$device", seal(key)).apply()
            prefs.edit().putString("kid:$server/$device", E2e.keyId(E2e.parseKey(key))).apply()
        }
    }

    private fun secretKey(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val g = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        g.init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build())
        return g.generateKey()
    }

    private fun seal(text: String): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, secretKey())
        return E2e.b64e(c.iv) + "." + E2e.b64e(c.doFinal(text.toByteArray()))
    }

    private fun open(sealed: String?): String? {
        if (sealed == null) return null
        return try {
            val (iv, ct) = sealed.split('.', limit = 2)
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, E2e.b64d(iv)))
            String(c.doFinal(E2e.b64d(ct)))
        } catch (e: Exception) {
            null                              // the Keystore key is gone (e.g. restored data): ask again
        }
    }

    // ------------------------------------------------------------------ recents
    @Synchronized fun recents(server: String): JSONArray = JSONArray(prefs.getString("recents:$server", "[]"))

    /** The port answered: move it to the top (count + 1). */
    @Synchronized fun addRecent(server: String, device: String, name: String, port: Int, status: Int) {
        val now = System.currentTimeMillis()
        val old = findRecent(server, device, port)
        val item = (old ?: JSONObject().put("device", device).put("port", port).put("count", 0).put("title", ""))
            .put("name", name).put("at", now).put("status", status).put("count", (old?.optInt("count") ?: 0) + 1)
        val out = JSONArray().put(item)
        val a = recents(server)
        for (i in 0 until a.length()) {
            val o = a.getJSONObject(i)
            if (!(o.optString("device") == device && o.optInt("port") == port) && out.length() < MAX_RECENTS) out.put(o)
        }
        prefs.edit().putString("recents:$server", out.toString()).apply()
    }

    /** Still answering while open: refresh the time only (no reordering beyond moving it up). */
    @Synchronized fun touchRecent(server: String, device: String, port: Int) = editRecent(server, device, port) {
        it.put("at", System.currentTimeMillis())
    }

    @Synchronized fun titleRecent(server: String, device: String, port: Int, title: String) = editRecent(server, device, port) {
        it.put("title", title.take(120))
    }

    @Synchronized fun removeRecent(server: String, device: String, port: Int) {
        val a = recents(server)
        val out = JSONArray()
        for (i in 0 until a.length()) {
            val o = a.getJSONObject(i)
            if (!(o.optString("device") == device && o.optInt("port") == port)) out.put(o)
        }
        prefs.edit().putString("recents:$server", out.toString()).apply()
    }

    private fun findRecent(server: String, device: String, port: Int): JSONObject? {
        val a = recents(server)
        for (i in 0 until a.length()) {
            val o = a.getJSONObject(i)
            if (o.optString("device") == device && o.optInt("port") == port) return o
        }
        return null
    }

    private fun editRecent(server: String, device: String, port: Int, f: (JSONObject) -> Unit) {
        val a = recents(server)
        var hit = false
        for (i in 0 until a.length()) {
            val o = a.getJSONObject(i)
            if (o.optString("device") == device && o.optInt("port") == port) { f(o); hit = true }
        }
        if (hit) prefs.edit().putString("recents:$server", a.toString()).apply()
    }

    // ------------------------------------------------------------------ origins
    /** (label, last local port) for a device's port; a new random label the first time. */
    @Synchronized fun origin(server: String, device: String, port: Int): Pair<String, Int> {
        val o = prefs.getString("origin:$server/$device/$port", null)?.let { JSONObject(it) }
        if (o != null) return o.optString("label") to o.optInt("local")
        val label = Gateway.newLabel()
        prefs.edit().putString("origin:$server/$device/$port", JSONObject().put("label", label).put("local", 0).toString()).apply()
        return label to 0
    }

    @Synchronized fun setOriginPort(server: String, device: String, port: Int, local: Int) {
        val (label, _) = origin(server, device, port)
        prefs.edit().putString("origin:$server/$device/$port", JSONObject().put("label", label).put("local", local).toString()).apply()
    }
}
