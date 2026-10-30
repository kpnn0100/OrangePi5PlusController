package com.arstro.arstro_remote.phone.features

import java.io.File

/*
 * Find my phone's location history (PHONE.md "find.history"): one line per fix in a small text file in the
 * app's private files - "time_ms,lat,lon,accuracy_m" - kept for 7 days. Rewritten atomically (temp file +
 * rename) on every add, so a crash never leaves half a file. No Android classes - JVM unit tests.
 */
class LocationHistory(private val file: File, private val keepMs: Long = KEEP_MS) {
    data class Fix(val lat: Double, val lon: Double, val accuracyM: Double, val time: Long)

    companion object {
        const val KEEP_MS = 7L * 24 * 3600 * 1000
        const val INTERVAL_MS = 15L * 60 * 1000
        private const val FUTURE_SLACK_MS = 24L * 3600 * 1000

        fun format(f: Fix): String = "${f.time},${f.lat},${f.lon},${f.accuracyM}"

        /** One line -> a fix, null when the line is not one (damaged file, other format). */
        fun parse(line: String): Fix? {
            val p = line.trim().split(',')
            if (p.size != 4) return null
            val t = p[0].toLongOrNull() ?: return null
            val lat = p[1].toDoubleOrNull() ?: return null
            val lon = p[2].toDoubleOrNull() ?: return null
            val acc = p[3].toDoubleOrNull() ?: return null
            if (lat !in -90.0..90.0 || lon !in -180.0..180.0 || acc.isNaN() || acc < 0) return null
            return Fix(lat, lon, acc, t)
        }

        /** The fixes still kept at `now`, oldest first (drops older than `keepMs` and far-future ones). */
        fun prune(fixes: List<Fix>, now: Long, keepMs: Long = KEEP_MS): List<Fix> =
            fixes.filter { it.time >= now - keepMs && it.time <= now + FUTURE_SLACK_MS }.sortedBy { it.time }
    }

    @Synchronized
    fun all(now: Long): List<Fix> {
        val lines = try { if (file.exists()) file.readLines() else emptyList() } catch (e: Exception) { emptyList() }
        return prune(lines.mapNotNull { parse(it) }, now, keepMs)
    }

    /** Newest first, at most `limit`. */
    @Synchronized
    fun list(now: Long, limit: Int): List<Fix> = all(now).asReversed().take(maxOf(0, limit))

    @Synchronized
    fun lastTime(now: Long): Long? = all(now).lastOrNull()?.time

    @Synchronized
    fun add(fix: Fix, now: Long) {
        val kept = prune(all(now) + fix, now, keepMs)
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(kept.joinToString("") { format(it) + "\n" })
        if (!tmp.renameTo(file)) {
            file.delete()
            tmp.renameTo(file)
        }
    }

    @Synchronized
    fun clear() { file.delete() }
}
