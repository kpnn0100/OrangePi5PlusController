package com.arstro.arstro_remote_client.features

import android.os.Environment
import android.os.StatFs
import android.util.Log
import android.webkit.MimeTypeMap
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import com.arstro.arstro_remote_client.core.Request
import com.arstro.arstro_remote_client.core.Response
import com.arstro.arstro_remote_client.core.SpecialAccess
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FilterInputStream
import java.io.InputStream

/**
 * Files (PHONE.md "files", PHN-11): the shared storage (/storage/emulated/0) with All files access.
 * Paths are shown relative to it ("/" = the root, "/DCIM/Camera"); nothing outside it is reachable.
 * Downloads stream with Content-Length and Range (206); uploads go to a temporary file first and are
 * renamed into place when complete; an existing file is only replaced when asked (overwrite=1).
 */
object FilesFeature : Feature("files", "Files") {
    override val specialAccess = listOf(SpecialAccess.ALL_FILES)

    private const val TEMP_PREFIX = ".arstro-upload-"

    @Suppress("DEPRECATION")
    private val root: String by lazy { Environment.getExternalStorageDirectory().absolutePath.trimEnd('/') }
    private val canonicalRoot: String by lazy { try { File(root).canonicalPath } catch (e: Exception) { root } }

    override fun ops() = mapOf(
        "files.delete" to op { c, a -> delete(c, a.needStr("path")) },
        "files.mkdir" to op { c, a -> mkdir(a.needStr("path")) },
        "files.rename" to op { c, a -> rename(a.needStr("path"), a.needStr("name")) },
    )

    override fun routes() = mapOf(
        "GET /api/files/list" to route { _, req -> Response.json(list(req.param("path"))) },
        "GET /api/files/download" to route { _, req -> download(req) },
        "PUT /api/files/upload" to route { c, req -> upload(c, req) },
        "POST /api/files/upload" to route { c, req -> upload(c, req) },
    )

    // ------------------------------------------------------------------------------------- paths

    /** The real file for a viewer's path, refusing anything outside the storage root (also via symlinks). */
    private fun resolve(path: String?): File {
        val abs = StoragePaths.resolve(root, path) ?: throw OpError("that path is outside the phone's storage", 403)
        val f = File(abs)
        val canon = try { f.canonicalPath } catch (e: Exception) { abs }
        if (!StoragePaths.inside(canonicalRoot, canon) && !StoragePaths.inside(root, canon)) {
            throw OpError("that path is outside the phone's storage", 403)
        }
        return f
    }

    private fun shown(f: File) = StoragePaths.display(root, f.absolutePath)

    private fun entry(f: File): JSONObject = JSONObject().put("name", f.name).put("path", shown(f)).put("dir", f.isDirectory)
        .put("size", if (f.isDirectory) 0 else f.length()).put("mtime", f.lastModified())

    // ------------------------------------------------------------------------------------- list / ops

    private fun list(path: String?): JSONObject {
        val dir = resolve(path)
        if (!dir.exists()) throw OpError("no such folder: ${shown(dir)}", 404)
        if (!dir.isDirectory) throw OpError("${shown(dir)} is a file, not a folder")
        val files = dir.listFiles() ?: throw OpError("Android does not let the app read this folder - allow All files access in the app", 403)
        val sorted = files.filter { !it.name.startsWith(TEMP_PREFIX) }
            .sortedWith(compareBy<File>({ !it.isDirectory }, { it.name.lowercase() }))
        val shownPath = shown(dir)
        val out = JSONObject().put("path", shownPath).put("parent", StoragePaths.parent(shownPath) ?: JSONObject.NULL)
            .put("entries", JSONArray(sorted.map { entry(it) }))
        try {
            val st = StatFs(dir.absolutePath)
            out.put("free", st.availableBytes).put("total", st.totalBytes)
        } catch (e: Exception) { }
        return out
    }

    private fun delete(c: PhoneContext, path: String): JSONObject {
        val f = resolve(path)
        if (f.absolutePath == root) throw OpError("the storage root cannot be deleted", 403)
        if (!f.exists()) throw OpError("no such file or folder: ${shown(f)}", 404)
        val ok = if (f.isDirectory) f.deleteRecursively() else f.delete()
        if (!ok) throw OpError("could not delete ${shown(f)} (Android refused part of it)", 409)
        c.log(Log.INFO, "files: deleted an entry")
        return jsonOf("path" to shown(f))
    }

    private fun mkdir(path: String): JSONObject {
        val f = resolve(path)
        if (f.exists()) throw OpError("${shown(f)} already exists", 409)
        if (!f.mkdirs()) throw OpError("could not create ${shown(f)}", 409)
        return jsonOf("path" to shown(f))
    }

    private fun rename(path: String, name: String): JSONObject {
        if (!StoragePaths.validName(name)) throw OpError("\"$name\" is not a valid name (no / and not . or ..)")
        val f = resolve(path)
        if (f.absolutePath == root) throw OpError("the storage root cannot be renamed", 403)
        if (!f.exists()) throw OpError("no such file or folder: ${shown(f)}", 404)
        val target = File(f.parentFile, name)
        resolve(target.absolutePath)
        if (target.exists()) throw OpError("${shown(target)} already exists", 409)
        if (!f.renameTo(target)) throw OpError("could not rename ${shown(f)}", 409)
        return jsonOf("path" to shown(target))
    }

    // ------------------------------------------------------------------------------------- download

    private fun mime(name: String): String =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase()) ?: "application/octet-stream"

    private fun download(req: Request): Response {
        val f = resolve(req.param("path") ?: throw OpError("\"path\" is missing"))
        if (!f.exists()) throw OpError("no such file: ${shown(f)}", 404)
        if (f.isDirectory) throw OpError("${shown(f)} is a folder - download its files one by one")
        if (!f.canRead()) throw OpError("Android does not let the app read ${shown(f)}", 403)
        val total = f.length()
        val inline = req.param("inline") == "1"
        val base = listOf(
            "Content-Type" to mime(f.name),
            "Content-Disposition" to StoragePaths.contentDisposition(f.name, inline),
            "Accept-Ranges" to "bytes",
            "Last-Modified" to java.text.SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss 'GMT'", java.util.Locale.US)
                .apply { timeZone = java.util.TimeZone.getTimeZone("GMT") }.format(java.util.Date(f.lastModified())),
            "Cache-Control" to "no-store",
        )
        val head = req.method == "HEAD"
        return when (val r = StoragePaths.parseRange(req.headers["range"], total)) {
            is StoragePaths.RangeResult.Whole ->
                Response(200, base + ("Content-Length" to total.toString()), if (head) null else FileInputStream(f), total)
            is StoragePaths.RangeResult.Part -> {
                val body = if (head) null else FileInputStream(f).also { skipFully(it, r.start) }.let { Limited(it, r.length) }
                Response(206, base + listOf("Content-Length" to r.length.toString(),
                                            "Content-Range" to "bytes ${r.start}-${r.end}/$total"), body, r.length)
            }
            is StoragePaths.RangeResult.Unsatisfiable ->
                Response(416, listOf("Content-Range" to "bytes */$total", "Content-Type" to "application/json"),
                         java.io.ByteArrayInputStream("{\"ok\":false,\"error\":\"the range is outside the file\"}".toByteArray()), -1)
        }
    }

    private fun skipFully(input: InputStream, n: Long) {
        var left = n
        while (left > 0) {
            val s = input.skip(left)
            if (s <= 0) {
                if (input.read() < 0) break
                left--
            } else left -= s
        }
    }

    /** At most `left` bytes of `input`. */
    private class Limited(input: InputStream, private var left: Long) : FilterInputStream(input) {
        override fun read(): Int {
            if (left <= 0) return -1
            val b = super.read()
            if (b >= 0) left--
            return b
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (left <= 0) return -1
            val n = super.read(b, off, minOf(len.toLong(), left).toInt())
            if (n > 0) left -= n
            return n
        }

        override fun available(): Int = minOf(super.available().toLong(), left).toInt()
    }

    // ------------------------------------------------------------------------------------- upload

    private fun upload(c: PhoneContext, req: Request): Response {
        val name = req.param("name")
        if (!StoragePaths.validName(name)) throw OpError("\"name\" is missing or not a valid file name")
        val dir = resolve(req.param("dir") ?: "/")
        if (!dir.isDirectory) throw OpError("no such folder: ${shown(dir)}", 404)
        val length = req.contentLength
        if (length < 0) throw OpError("Content-Length is required for an upload", 411)
        val target = File(dir, name!!)
        resolve(target.absolutePath)
        val overwrite = req.param("overwrite").let { it == "1" || it == "true" }
        if (target.isDirectory) throw OpError("${shown(target)} is a folder", 409)
        if (target.exists() && !overwrite) throw OpError("${shown(target)} already exists - upload again with overwrite to replace it", 409)
        try {
            if (StatFs(dir.absolutePath).availableBytes < length) throw OpError("not enough free space on the phone", 507)
        } catch (e: IllegalArgumentException) { }
        val tmp = File(dir, TEMP_PREFIX + System.nanoTime() + "-" + name.take(40))
        var written = 0L
        try {
            val body = req.body
            tmp.outputStream().use { out ->
                if (body != null) {
                    val buf = ByteArray(64 * 1024)
                    while (written < length) {
                        val n = body.read(buf, 0, minOf(buf.size.toLong(), length - written).toInt())
                        if (n < 0) break
                        out.write(buf, 0, n)
                        written += n
                    }
                }
                out.fd.sync()
            }
            if (written != length) throw OpError("the upload was cut off ($written of $length bytes) - try again")
            if (target.exists() && !overwrite) throw OpError("${shown(target)} already exists - upload again with overwrite to replace it", 409)
            if (!tmp.renameTo(target)) {
                if (overwrite && target.delete() && tmp.renameTo(target)) Unit
                else throw OpError("could not save ${shown(target)}", 409)
            }
        } catch (e: java.io.IOException) {
            throw OpError("could not write ${shown(target)} (${e.message?.take(80) ?: "I/O error"})", 500)
        } finally {
            if (tmp.exists()) tmp.delete()
        }
        c.log(Log.INFO, "files: uploaded $length bytes")
        return Response.json(JSONObject().put("ok", true).put("path", shown(target)).put("size", length))
    }
}
