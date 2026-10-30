package com.arstro.arstro_remote_client.features

import android.Manifest
import android.content.Context
import android.net.Uri
import android.provider.CallLog
import android.provider.ContactsContract
import com.arstro.arstro_remote_client.core.Feature
import org.json.JSONArray
import org.json.JSONObject

/** Contacts & call log (PHONE.md "contacts", PHN-09). */
object ContactsFeature : Feature("contacts", "Contacts & call log") {
    override val permissions = listOf(Manifest.permission.READ_CONTACTS, Manifest.permission.READ_CALL_LOG)

    override fun ops() = mapOf(
        "contacts.list" to op { c, a -> list(c.app, a.str("q")?.trim().orEmpty(), a.limit(200, 2000)) },
        "contacts.calls" to op { c, a -> calls(c.app, a.limit(100, 1000)) },
    )

    private fun list(app: Context, q: String, limit: Int): JSONArray {
        val cr = app.contentResolver
        // contact ids in display-name order: by name, plus by number when the query has digits
        val ids = LinkedHashMap<Long, String>()
        val uri = if (q.isEmpty()) ContactsContract.Contacts.CONTENT_URI
                  else Uri.withAppendedPath(ContactsContract.Contacts.CONTENT_FILTER_URI, Uri.encode(q))
        cr.query(uri, arrayOf(ContactsContract.Contacts._ID, ContactsContract.Contacts.DISPLAY_NAME_PRIMARY), null, null,
                 ContactsContract.Contacts.DISPLAY_NAME_PRIMARY + " COLLATE LOCALIZED ASC")?.use { cur ->
            while (cur.moveToNext() && ids.size < limit) ids[cur.getLong(0)] = cur.getString(1) ?: ""
        }
        if (q.any { it.isDigit() } && ids.size < limit) {
            cr.query(Uri.withAppendedPath(ContactsContract.CommonDataKinds.Phone.CONTENT_FILTER_URI, Uri.encode(q)),
                     arrayOf(ContactsContract.CommonDataKinds.Phone.CONTACT_ID, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY),
                     null, null, null)?.use { cur ->
                while (cur.moveToNext() && ids.size < limit) ids.putIfAbsent(cur.getLong(0), cur.getString(1) ?: "")
            }
        }
        val numbers = HashMap<Long, MutableList<String>>()
        val emails = HashMap<Long, MutableList<String>>()
        for (chunk in ids.keys.chunked(400)) {
            val sel = ContactsContract.Data.CONTACT_ID + " IN (" + chunk.joinToString(",") + ") AND " +
                ContactsContract.Data.MIMETYPE + " IN (?, ?)"
            cr.query(ContactsContract.Data.CONTENT_URI,
                     arrayOf(ContactsContract.Data.CONTACT_ID, ContactsContract.Data.MIMETYPE, ContactsContract.Data.DATA1), sel,
                     arrayOf(ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE, ContactsContract.CommonDataKinds.Email.CONTENT_ITEM_TYPE),
                     null)?.use { cur ->
                while (cur.moveToNext()) {
                    val id = cur.getLong(0)
                    val value = cur.getString(2)?.trim()?.takeIf { it.isNotEmpty() } ?: continue
                    val target = if (cur.getString(1) == ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE) numbers else emails
                    val l = target.getOrPut(id) { ArrayList() }
                    if (l.none { same(it, value) }) l.add(value)
                }
            }
        }
        val out = JSONArray()
        for ((id, name) in ids) {
            val o = JSONObject().put("name", name).put("numbers", JSONArray(numbers[id] ?: emptyList<String>()))
            emails[id]?.let { o.put("emails", JSONArray(it)) }
            out.put(o)
        }
        return out
    }

    private fun same(a: String, b: String): Boolean =
        a.filter { it.isDigit() || it == '+' || it == '@' || it.isLetter() } == b.filter { it.isDigit() || it == '+' || it == '@' || it.isLetter() }

    private fun calls(app: Context, limit: Int): JSONArray {
        val out = JSONArray()
        app.contentResolver.query(CallLog.Calls.CONTENT_URI,
            arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME, CallLog.Calls.TYPE, CallLog.Calls.DATE, CallLog.Calls.DURATION),
            null, null, CallLog.Calls.DATE + " DESC")?.use { cur ->
            val names = ContactNames(app)
            while (cur.moveToNext() && out.length() < limit) {
                val number = cur.getString(0) ?: ""
                val type = when (cur.getInt(2)) {
                    CallLog.Calls.INCOMING_TYPE, CallLog.Calls.ANSWERED_EXTERNALLY_TYPE -> "in"
                    CallLog.Calls.OUTGOING_TYPE -> "out"
                    CallLog.Calls.MISSED_TYPE, CallLog.Calls.VOICEMAIL_TYPE -> "missed"
                    CallLog.Calls.REJECTED_TYPE, CallLog.Calls.BLOCKED_TYPE -> "rejected"
                    else -> "in"
                }
                val name = cur.getString(1)?.takeIf { it.isNotBlank() } ?: names.name(number)
                val o = JSONObject().put("number", number).put("type", type).put("date", cur.getLong(3)).put("duration_s", cur.getLong(4))
                if (name != null) o.put("name", name)
                out.put(o)
            }
        }
        return out
    }
}

/** Phone number -> contact name (cached; null without the contacts permission or when unknown). */
class ContactNames(private val app: Context) {
    private val cache = HashMap<String, String?>()
    private val allowed = app.granted(Manifest.permission.READ_CONTACTS)

    fun name(number: String?): String? {
        if (!allowed || number.isNullOrBlank()) return null
        return cache.getOrPut(number) {
            try {
                app.contentResolver.query(Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number)),
                    arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)?.use { c ->
                    if (c.moveToFirst()) c.getString(0) else null
                }
            } catch (e: Exception) { null }
        }
    }
}

