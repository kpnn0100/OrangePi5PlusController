package com.arstro.arstro_remote_client.core

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.math.BigInteger
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.Signature
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import javax.net.ServerSocketFactory
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext

/**
 * The phone's own TLS certificate for https://<phone>:8443 (PHONE.md "On the network"): an EC P-256 key
 * and a self-signed X.509 v3 certificate, made once and kept in the app's private settings, so its
 * fingerprint stays the same (the app shows it to compare with the browser's warning page). Built
 * with a few lines of DER - Android has no public API that makes certificates. Pure JVM.
 */
object SelfSigned {
    class Identity(val keyPkcs8: ByteArray, val certDer: ByteArray) {
        val cert: X509Certificate by lazy {
            CertificateFactory.getInstance("X.509").generateCertificate(ByteArrayInputStream(certDer)) as X509Certificate
        }
        /** SHA-256 of the certificate, "AB:CD:..." as browsers show it. */
        val fingerprint: String get() =
            MessageDigest.getInstance("SHA-256").digest(certDer).joinToString(":") { "%02X".format(it) }

        fun serverSocketFactory(): ServerSocketFactory {
            val key: PrivateKey = KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(keyPkcs8))
            val pass = CharArray(0)
            val ks = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
                load(null, null)
                setKeyEntry("phone", key, pass, arrayOf(cert))
            }
            val kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(ks, pass) }
            return SSLContext.getInstance("TLS").apply { init(kmf.keyManagers, null, SecureRandom()) }.serverSocketFactory
        }
    }

    fun create(name: String = "Arstro phone", years: Int = 20): Identity {
        val kp = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1"), SecureRandom()) }.generateKeyPair()
        val now = System.currentTimeMillis()
        val serial = BigInteger(1, ByteArray(16).also { SecureRandom().nextBytes(it) }).setBit(127)
        val sigAlg = seq(oid("1.2.840.10045.4.3.2"))                                  // ecdsa-with-SHA256
        val dn = seq(set(seq(oid("2.5.4.3"), tlv(0x0c, name.toByteArray()))))        // CN=<name>
        val validity = seq(time(Date(now - 86_400_000L)), time(Date(now + years * 365L * 86_400_000L)))
        val san = seq(tlv(0x82, "localhost".toByteArray()), tlv(0x87, byteArrayOf(127, 0, 0, 1)))
        val ext = tlv(0xa3, seq(
            seq(oid("2.5.29.19"), tlv(0x04, seq())),                                 // basicConstraints: not a CA
            seq(oid("2.5.29.17"), tlv(0x04, san)),                                   // subjectAltName
        ))
        val tbs = seq(tlv(0xa0, int(BigInteger.valueOf(2))), int(serial), sigAlg, dn, validity, dn, kp.public.encoded, ext)
        val sig = Signature.getInstance("SHA256withECDSA").apply { initSign(kp.private); update(tbs) }.sign()
        val cert = seq(tbs, sigAlg, tlv(0x03, byteArrayOf(0) + sig))
        return Identity(kp.private.encoded, cert)
    }

    // ------------------------------------------------------------------ DER
    private fun len(n: Int): ByteArray = when {
        n < 0x80 -> byteArrayOf(n.toByte())
        n < 0x100 -> byteArrayOf(0x81.toByte(), n.toByte())
        n < 0x10000 -> byteArrayOf(0x82.toByte(), (n shr 8).toByte(), n.toByte())
        else -> byteArrayOf(0x83.toByte(), (n shr 16).toByte(), (n shr 8).toByte(), n.toByte())
    }

    fun tlv(tag: Int, body: ByteArray): ByteArray = byteArrayOf(tag.toByte()) + len(body.size) + body
    private fun seq(vararg parts: ByteArray) = tlv(0x30, cat(*parts))
    private fun set(vararg parts: ByteArray) = tlv(0x31, cat(*parts))
    private fun int(v: BigInteger) = tlv(0x02, v.toByteArray())
    private fun cat(vararg parts: ByteArray): ByteArray = ByteArrayOutputStream().apply { parts.forEach { write(it) } }.toByteArray()

    private fun oid(s: String): ByteArray {
        val p = s.split('.').map { it.toLong() }
        val out = ByteArrayOutputStream()
        out.write((p[0] * 40 + p[1]).toInt())
        for (v in p.drop(2)) {
            val stack = ArrayList<Int>()
            var x = v
            stack.add((x and 0x7f).toInt())
            x = x shr 7
            while (x > 0) { stack.add(((x and 0x7f) or 0x80).toInt()); x = x shr 7 }
            stack.reversed().forEach { out.write(it) }
        }
        return tlv(0x06, out.toByteArray())
    }

    private fun time(d: Date): ByteArray {
        val utc = d.before(Date(2524608000000L))                                       // < 2050: UTCTime
        val f = SimpleDateFormat(if (utc) "yyMMddHHmmss'Z'" else "yyyyMMddHHmmss'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }
        return tlv(if (utc) 0x17 else 0x18, f.format(d).toByteArray())
    }
}
