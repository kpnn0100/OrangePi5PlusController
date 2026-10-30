package com.arstro.arstro_remote_client.features

/*
 * Pure path and HTTP helpers of the Files feature (PHONE.md "files"). Paths the viewer sees are relative
 * to the storage root and start with "/" ("/" = /storage/emulated/0, "/DCIM/Camera"); the absolute form
 * "/storage/emulated/0/DCIM" is accepted too. Nothing outside the root is reachable ("..", which is
 * resolved here without touching the disk, and symlinks, which the feature checks on the real path).
 * No Android classes - JVM unit tests.
 */
object StoragePaths {
    /** The absolute, normalised path of `path` inside `root`, or null when it leaves the root. */
    fun resolve(root: String, path: String?): String? {
        val r = root.trimEnd('/')
        val p = (path ?: "").trim()
        if (p.contains('\u0000')) return null
        val rel = when {
            p == r -> ""
            p.startsWith("$r/") -> p.substring(r.length)
            else -> p
        }
        val stack = ArrayList<String>()
        for (seg in rel.split('/')) {
            when (seg) {
                "", "." -> {}
                ".." -> { if (stack.isEmpty()) return null; stack.removeAt(stack.size - 1) }
                else -> stack.add(seg)
            }
        }
        return if (stack.isEmpty()) r else r + "/" + stack.joinToString("/")
    }

    /** True when `abs` is `root` or below it (string check on already normalised / canonical paths). */
    fun inside(root: String, abs: String): Boolean {
        val r = root.trimEnd('/')
        return abs == r || abs.startsWith("$r/")
    }

    /** The viewer's form of an absolute path inside root: "/" or "/DCIM/x". */
    fun display(root: String, abs: String): String {
        val r = root.trimEnd('/')
        val rel = if (abs == r) "" else abs.removePrefix("$r/")
        return "/$rel"
    }

    /** The viewer's form of the parent folder, null at the root. */
    fun parent(display: String): String? {
        if (display == "/" || display.isEmpty()) return null
        val i = display.trimEnd('/').lastIndexOf('/')
        return if (i <= 0) "/" else display.substring(0, i)
    }

    /** A plain file name: not empty, no '/', not "." / "..", no NUL, at most 255 bytes. */
    fun validName(name: String?): Boolean =
        name != null && name.isNotEmpty() && name != "." && name != ".." && !name.contains('/') &&
            !name.contains('\u0000') && name.toByteArray(Charsets.UTF_8).size <= 255

    sealed class RangeResult {
        object Whole : RangeResult()
        data class Part(val start: Long, val end: Long) : RangeResult() {   // end inclusive
            val length: Long get() = end - start + 1
        }
        object Unsatisfiable : RangeResult()
    }

    /** A `Range` header against a file of `total` bytes (only the first range of a list is served). */
    fun parseRange(header: String?, total: Long): RangeResult {
        if (header == null) return RangeResult.Whole
        val h = header.trim()
        if (!h.startsWith("bytes=", ignoreCase = true)) return RangeResult.Whole
        val first = h.substring(6).split(',').first().trim()
        val dash = first.indexOf('-')
        if (dash < 0) return RangeResult.Whole
        val a = first.substring(0, dash).trim()
        val b = first.substring(dash + 1).trim()
        return try {
            if (a.isEmpty()) {
                if (b.isEmpty()) return RangeResult.Whole
                val n = b.toLong()
                if (n <= 0 || total == 0L) return RangeResult.Unsatisfiable
                RangeResult.Part(maxOf(0, total - n), total - 1)
            } else {
                val s = a.toLong()
                if (s < 0) return RangeResult.Whole
                if (s >= total) return RangeResult.Unsatisfiable
                val e = if (b.isEmpty()) total - 1 else minOf(b.toLong(), total - 1)
                if (e < s) return RangeResult.Whole
                RangeResult.Part(s, e)
            }
        } catch (e: NumberFormatException) {
            RangeResult.Whole
        }
    }

    /** `attachment; filename="..."; filename*=UTF-8''...` (or inline). */
    fun contentDisposition(name: String, inline: Boolean = false): String {
        val ascii = name.map { c -> if (c.code in 0x20..0x7E && c != '"' && c != '\\') c else '_' }.joinToString("")
        val enc = StringBuilder()
        for (b in name.toByteArray(Charsets.UTF_8)) {
            val c = b.toInt() and 0xFF
            val ch = c.toChar()
            if (ch.isLetterOrDigit() && c < 0x80 || ch in "!#$&+-.^_`|~") enc.append(ch) else enc.append("%%%02X".format(c))
        }
        return "${if (inline) "inline" else "attachment"}; filename=\"$ascii\"; filename*=UTF-8''$enc"
    }
}
