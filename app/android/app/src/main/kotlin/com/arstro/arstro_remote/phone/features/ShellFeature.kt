package com.arstro.arstro_remote.phone.features

import android.os.Build
import android.util.Log
import com.arstro.arstro_remote.phone.core.Feature
import com.arstro.arstro_remote.phone.core.OpError
import com.arstro.arstro_remote.phone.core.PhoneContext
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit
import org.json.JSONObject

/** Shell (PHN-16): `sh -c` as the app, in its own files folder; a timeout; output capped at 1 MB. */
object ShellFeature : Feature("shell", "Shell (the app's sandbox)") {
    private const val MAX_OUT = 1 shl 20

    override fun ops() = mapOf(
        "shell.run" to op { c, a -> run(c, a.needStr("cmd"), (a.int("timeout_s") ?: 30).coerceIn(1, 600)) },
    )

    private fun run(c: PhoneContext, cmd: String, timeoutS: Int): JSONObject {
        val pb = ProcessBuilder("sh", "-c", cmd).redirectErrorStream(true).directory(c.app.filesDir)
        pb.environment()["HOME"] = c.app.filesDir.absolutePath
        pb.environment()["TMPDIR"] = c.app.cacheDir.absolutePath
        c.log(Log.INFO, "shell: running a command (${cmd.length} chars)")
        val p = try { pb.start() } catch (e: Exception) { throw OpError("the shell could not start (${e.message?.take(80)})", 500) }
        try { p.outputStream.close() } catch (e: Exception) { }
        val out = ByteArrayOutputStream()
        var truncated = false
        val reader = Thread({
            val buf = ByteArray(16 * 1024)
            try {
                val input = p.inputStream
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    synchronized(out) {
                        val room = MAX_OUT - out.size()
                        if (room > 0) out.write(buf, 0, minOf(n, room))
                        if (n > room) truncated = true
                    }
                }
            } catch (e: Exception) { }
        }, "arstro-shell").apply { isDaemon = true; start() }
        val finished = waitFor(p, timeoutS * 1000L)
        if (!finished) {
            p.destroy()
            if (!waitFor(p, 1000)) {
                if (Build.VERSION.SDK_INT >= 26) p.destroyForcibly()
            }
        }
        reader.join(2000)
        val text = synchronized(out) { String(out.toByteArray(), Charsets.UTF_8) }
        val o = JSONObject().put("code", if (finished) p.exitValue() else JSONObject.NULL).put("out", text)
        if (truncated) o.put("truncated", true)
        if (!finished) o.put("timed_out", true)
        return o
    }

    private fun waitFor(p: Process, ms: Long): Boolean {
        if (Build.VERSION.SDK_INT >= 26) return p.waitFor(ms, TimeUnit.MILLISECONDS)
        val deadline = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < deadline) {
            try { p.exitValue(); return true } catch (e: IllegalThreadStateException) { Thread.sleep(50) }
        }
        return false
    }
}
