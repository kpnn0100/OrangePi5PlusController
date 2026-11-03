package com.arstro.arstro_devices.core

import java.net.URI

/*
 * What the user types as the device manager's address -> the URLs to try (MGR-01). Pure JVM.
 *
 *   arstro.example.com                      -> https://arstro.example.com/arstro/internal/devices,
 *                                              https://arstro.example.com, then the same over http
 *   192.0.2.10:8090                          -> the same, http tried first (a LAN address)
 *   https://h/arstro/internal/devices/d/x/1/ -> https://h/arstro/internal/devices (a viewer link pasted)
 */
object Urls {
    const val DEFAULT_BASE = "/arstro/internal/devices"

    private val ipLike = Regex("^(\\d{1,3}(\\.\\d{1,3}){3}|localhost|\\[[0-9a-fA-F:]+\\])$")

    /** Candidate base URLs, most likely first. Throws IllegalArgumentException for text that is no address. */
    fun candidates(input: String): List<String> {
        var t = input.trim().replace(" ", "")
        require(t.isNotEmpty()) { "type the device manager's address" }
        val explicit = Regex("^[a-zA-Z][a-zA-Z0-9+.-]*://").containsMatchIn(t)
        if (!explicit) t = "https://$t"
        val u = try { URI(t) } catch (e: Exception) { throw IllegalArgumentException("that is not a web address") }
        val scheme = u.scheme?.lowercase()
        require(scheme == "http" || scheme == "https") { "the address must start with https:// or http://" }
        val host = u.host ?: throw IllegalArgumentException("that is not a web address")
        require(host.isNotEmpty()) { "that is not a web address" }
        val authority = u.rawAuthority
        val path = basePath(u.rawPath ?: "")
        val paths = if (path.isEmpty()) listOf(DEFAULT_BASE, "") else listOf(path)
        val schemes = when {
            explicit -> listOf(scheme)
            ipLike.matches(host) || host.endsWith(".local") -> listOf("http", "https")
            else -> listOf("https", "http")
        }
        return schemes.flatMap { s -> paths.map { p -> "$s://$authority$p" } }
    }

    /** A path the user pasted -> the device manager's base path (no trailing slash). */
    fun basePath(raw: String): String {
        var p = raw.trimEnd('/')
        for (cut in listOf("/d/", "/api/", "/static/", "/client/")) {
            val i = p.indexOf(cut)
            if (i >= 0) p = p.substring(0, i)
        }
        for (end in listOf("/index.html", "/sw.js", "/shim.js")) p = p.removeSuffix(end)
        return p.trimEnd('/')
    }

    /** https://host/base -> "host" (+ the path when it is not the default), for a short label. */
    fun label(url: String): String = try {
        val u = URI(url)
        val p = u.rawPath ?: ""
        u.rawAuthority + if (p.isEmpty() || p == DEFAULT_BASE) "" else p
    } catch (e: Exception) {
        url
    }
}
