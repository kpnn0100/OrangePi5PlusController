package com.arstro.arstro_remote_client.core

import java.net.Inet4Address
import java.net.NetworkInterface

/** The phone's own IPv4 addresses, for "open http://<address>:8080/" (Wi-Fi first, then its hotspot, then the rest). */
object NetAddr {
    data class Addr(val iface: String, val ip: String, val kind: String)

    fun all(): List<Addr> {
        val out = ArrayList<Addr>()
        try {
            for (ni in NetworkInterface.getNetworkInterfaces() ?: return out) {
                if (!ni.isUp || ni.isLoopback) continue
                for (a in ni.inetAddresses) {
                    if (a !is Inet4Address || a.isLoopbackAddress || a.isLinkLocalAddress) continue
                    out.add(Addr(ni.name, a.hostAddress ?: continue, kind(ni.name)))
                }
            }
        } catch (e: Exception) { }
        val order = listOf("wifi", "hotspot", "ethernet", "usb", "mobile", "other")
        return out.sortedBy { order.indexOf(it.kind) }
    }

    private fun kind(name: String): String = when {
        name.startsWith("wlan0") -> "wifi"
        name.startsWith("ap") || name.startsWith("swlan") || name.startsWith("wlan") -> "hotspot"
        name.startsWith("eth") -> "ethernet"
        name.startsWith("rndis") || name.startsWith("usb") -> "usb"
        name.startsWith("rmnet") || name.startsWith("ccmni") || name.startsWith("v4-") || name.startsWith("seth") -> "mobile"
        else -> "other"
    }

    /** The address to show first (null when the phone has no network). */
    fun best(): String? = all().firstOrNull { it.kind != "mobile" }?.ip ?: all().firstOrNull()?.ip
}
