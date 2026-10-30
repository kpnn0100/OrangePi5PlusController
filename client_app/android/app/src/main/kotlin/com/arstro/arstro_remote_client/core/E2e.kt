package com.arstro.arstro_remote_client.core

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/*
 * The end-to-end channel of the relay protocol, device side (docs/relay/PROTOCOL.md section 4).
 * A line-by-line port of server/arstro_remote/relay/e2e.py; docs/relay/vectors.json checks both.
 * Pure JVM (no android.*): the JVM unit tests run it as is.
 *
 *   viewer -> hello {v, cn}      device -> hello {v, sn, proof(device)}
 *   viewer -> proof {proof}      device -> [encrypted] ready {...}   (or the text error "wrong key")
 *
 * Keys: HKDF-SHA256(ikm = access key, salt = cn || sn, info = LABEL) -> 96 bytes =
 * viewer->device key | device->viewer key | proof key. Frames: AES-256-GCM, nonce = 4 zero bytes +
 * a 64-bit big-endian counter per direction from 0, no associated data.
 */

/** The peer broke the protocol (bad message, bad frame): the channel is closed. */
class ProtocolError(message: String) : Exception(message)

/** The proof does not match: the other side has a different access key. */
class WrongKey(message: String) : Exception(message)

/** A decrypted frame: kind u8 | stream u32 | flags u8 | payload. */
class Frame(val kind: Int, val stream: Long, val flags: Int, val payload: ByteArray)

object E2e {
    const val VERSION = 1
    val LABEL: ByteArray = "arstro-relay/1".toByteArray()
    const val KEY_PREFIX = "ak1_"
    const val KEY_LEN = 32
    const val NONCE_LEN = 32                 // cn / sn
    const val CHUNK = 64 * 1024              // largest DATA payload in one frame
    const val WINDOW = 512 * 1024            // unacknowledged DATA bytes per stream and direction
    const val MAX_MESSAGE = 1 shl 20         // largest WebSocket message on the path
    const val PIPE_MAX_UP = 256 * 1024       // largest up-request body of the HTTP transport
    const val PIPE_POLL = 25                 // s a down request waits before 204
    const val PIPE_IDLE = 60                 // s without any request: the pipe is dropped (1006)
    const val PIPE_BUFFER = 2 shl 20         // queued bytes per direction before the sender waits

    const val K_JSON = 1                     // payload: UTF-8 JSON object with "t"
    const val K_DATA = 2                     // payload: bytes of a stream
    const val F_FIN = 1                      // DATA of a ws stream: last fragment of a message
    const val F_TEXT = 2                     // DATA of a ws stream: the message is text
    const val HEADER = 6                     // kind u8 | stream u32 BE | flags u8

    private val random = SecureRandom()

    // ------------------------------------------------------------------ base64url (no padding)
    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    private val DECODE = IntArray(128) { -1 }.also { t ->
        ALPHABET.forEachIndexed { i, c -> t[c.code] = i }
        t['+'.code] = 62                     // standard base64 is accepted too
        t['/'.code] = 63
    }

    fun b64e(data: ByteArray): String {
        val sb = StringBuilder((data.size * 4 + 2) / 3)
        var i = 0
        while (i + 3 <= data.size) {
            val n = (data[i].toInt() and 0xff shl 16) or (data[i + 1].toInt() and 0xff shl 8) or (data[i + 2].toInt() and 0xff)
            sb.append(ALPHABET[n shr 18 and 63]).append(ALPHABET[n shr 12 and 63])
              .append(ALPHABET[n shr 6 and 63]).append(ALPHABET[n and 63])
            i += 3
        }
        when (data.size - i) {
            1 -> {
                val n = data[i].toInt() and 0xff shl 16
                sb.append(ALPHABET[n shr 18 and 63]).append(ALPHABET[n shr 12 and 63])
            }
            2 -> {
                val n = (data[i].toInt() and 0xff shl 16) or (data[i + 1].toInt() and 0xff shl 8)
                sb.append(ALPHABET[n shr 18 and 63]).append(ALPHABET[n shr 12 and 63]).append(ALPHABET[n shr 6 and 63])
            }
        }
        return sb.toString()
    }

    /** base64url (or standard base64), padding optional. Throws ProtocolError. */
    fun b64d(text: String): ByteArray {
        val t = text.trimEnd('=')
        if (t.length % 4 == 1) throw ProtocolError("bad base64: wrong length")
        val out = ByteArrayOutputStream(t.length * 3 / 4)
        var acc = 0
        var bits = 0
        for (c in t) {
            val v = if (c.code < 128) DECODE[c.code] else -1
            if (v < 0) throw ProtocolError("bad base64: unexpected character")
            acc = (acc shl 6) or v
            bits += 6
            if (bits >= 8) {
                bits -= 8
                out.write(acc shr bits and 0xff)
            }
        }
        return out.toByteArray()
    }

    fun randomBytes(n: Int): ByteArray = ByteArray(n).also { random.nextBytes(it) }

    // ------------------------------------------------------------------ the access key
    /** A new access key as text: "ak1_" + 43 base64url characters (32 random bytes). */
    fun newKey(): String = KEY_PREFIX + b64e(randomBytes(KEY_LEN))

    /** Key text -> 32 bytes. Spaces and line breaks from copy-paste are ignored. */
    fun parseKey(text: String?): ByteArray {
        val t = (text ?: "").filterNot { it.isWhitespace() }
        require(t.startsWith(KEY_PREFIX)) { "an access key starts with $KEY_PREFIX" }
        val raw = try { b64d(t.substring(KEY_PREFIX.length)) } catch (e: ProtocolError) {
            throw IllegalArgumentException("the access key has characters that do not belong in it")
        }
        require(raw.size == KEY_LEN) { "the access key is too short or too long - copy all of it" }
        return raw
    }

    /** A short public fingerprint of a key, to tell keys apart in a UI. Not secret, not the key. */
    fun keyId(raw: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(LABEL + " key-id ".toByteArray() + raw)
        return b64e(d.copyOf(6))
    }

    // ------------------------------------------------------------------ crypto
    fun hmac(key: ByteArray, data: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(data)
    }

    /** HKDF-SHA256 (RFC 5869). */
    fun hkdf(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        val prk = hmac(if (salt.isEmpty()) ByteArray(32) else salt, ikm)
        val out = ByteArrayOutputStream(length)
        var t = ByteArray(0)
        var i = 1
        while (out.size() < length) {
            t = hmac(prk, t + info + byteArrayOf(i.toByte()))
            out.write(t)
            i++
        }
        return out.toByteArray().copyOf(length)
    }

    class Keys(val viewerToDevice: ByteArray, val deviceToViewer: ByteArray, val proofKey: ByteArray)

    fun derive(rawKey: ByteArray, cn: ByteArray, sn: ByteArray): Keys {
        if (cn.size != NONCE_LEN || sn.size != NONCE_LEN) throw ProtocolError("cn and sn must be $NONCE_LEN bytes")
        val okm = hkdf(rawKey, cn + sn, LABEL, 96)
        return Keys(okm.copyOfRange(0, 32), okm.copyOfRange(32, 64), okm.copyOfRange(64, 96))
    }

    /** HMAC-SHA256(proof key, LABEL + " " + role), role "device" or "viewer". */
    fun proof(proofKey: ByteArray, role: String): ByteArray = hmac(proofKey, LABEL + " $role".toByteArray())

    fun nonce(counter: Long): ByteArray = ByteBuffer.allocate(12).putInt(0).putLong(counter).array()

    fun decodeJson(payload: ByteArray): JSONObject {
        val msg = try { JSONObject(String(payload, Charsets.UTF_8)) } catch (e: Exception) {
            throw ProtocolError("bad JSON frame")
        }
        if (msg.opt("t") !is String) throw ProtocolError("a JSON frame must be an object with t")
        return msg
    }

    /** A text handshake message -> JSON object (ProtocolError if it is not one). */
    fun parseText(text: String): JSONObject = try { JSONObject(text) } catch (e: Exception) {
        throw ProtocolError("bad handshake message")
    }

    // ------------------------------------------------------------------ handshake (device side)
    class DeviceState internal constructor(internal val keys: Keys)

    /** On the viewer's hello -> (state for deviceCheck, the answer to send as text). */
    fun deviceAnswer(rawKey: ByteArray, hello: JSONObject): Pair<DeviceState, JSONObject> {
        if (hello.opt("t") != "hello" || hello.opt("v") != VERSION) throw ProtocolError("expected hello v$VERSION")
        val cn = b64d(hello.optString("cn", ""))
        val sn = randomBytes(NONCE_LEN)
        val keys = derive(rawKey, cn, sn)
        val msg = JSONObject().put("t", "hello").put("v", VERSION).put("sn", b64e(sn))
            .put("proof", b64e(proof(keys.proofKey, "device")))
        return DeviceState(keys) to msg
    }

    /** On the viewer's proof -> Secure (send = device->viewer). Throws WrongKey / ProtocolError. */
    fun deviceCheck(state: DeviceState, msg: JSONObject): Secure {
        if (msg.opt("t") != "proof") throw ProtocolError("expected proof")
        val got = b64d(msg.optString("proof", ""))
        if (!MessageDigest.isEqual(proof(state.keys.proofKey, "viewer"), got)) throw WrongKey("the viewer has a different access key")
        return Secure(state.keys.deviceToViewer, state.keys.viewerToDevice)
    }

    // ------------------------------------------------------------------ handshake (viewer side, for tests)
    fun viewerHello(): Pair<ByteArray, JSONObject> {
        val cn = randomBytes(NONCE_LEN)
        return cn to JSONObject().put("t", "hello").put("v", VERSION).put("cn", b64e(cn))
    }

    fun viewerCheck(rawKey: ByteArray, cn: ByteArray, answer: JSONObject): Pair<Secure, JSONObject> {
        if (answer.opt("t") == "error") throw ProtocolError(answer.optString("error", "refused"))
        if (answer.opt("t") != "hello" || answer.opt("v") != VERSION) throw ProtocolError("expected hello v$VERSION")
        val keys = derive(rawKey, cn, b64d(answer.optString("sn", "")))
        if (!MessageDigest.isEqual(proof(keys.proofKey, "device"), b64d(answer.optString("proof", "")))) {
            throw WrongKey("wrong access key for this device")
        }
        return Secure(keys.viewerToDevice, keys.deviceToViewer) to
            JSONObject().put("t", "proof").put("proof", b64e(proof(keys.proofKey, "viewer")))
    }
}

/** One side of an established channel: seal outgoing frames, open incoming ones. Not thread-safe:
 *  the channel seals under its send lock (nonce order = wire order) and opens on its reader. */
class Secure(sendKey: ByteArray, recvKey: ByteArray) {
    private val sendKey = SecretKeySpec(sendKey, "AES")
    private val recvKey = SecretKeySpec(recvKey, "AES")
    private val enc = Cipher.getInstance("AES/GCM/NoPadding")
    private val dec = Cipher.getInstance("AES/GCM/NoPadding")
    private var sn = 0L
    private var rn = 0L

    fun seal(kind: Int, stream: Long, payload: ByteArray = ByteArray(0), flags: Int = 0): ByteArray =
        seal(kind, stream, payload, 0, payload.size, flags)

    fun seal(kind: Int, stream: Long, payload: ByteArray, off: Int, len: Int, flags: Int): ByteArray {
        val pt = ByteArray(E2e.HEADER + len)
        pt[0] = kind.toByte()
        pt[1] = (stream shr 24).toByte(); pt[2] = (stream shr 16).toByte()
        pt[3] = (stream shr 8).toByte(); pt[4] = stream.toByte()
        pt[5] = flags.toByte()
        System.arraycopy(payload, off, pt, E2e.HEADER, len)
        enc.init(Cipher.ENCRYPT_MODE, sendKey, GCMParameterSpec(128, E2e.nonce(sn)))
        val ct = enc.doFinal(pt)
        sn++
        return ct
    }

    fun sealJson(stream: Long, obj: JSONObject): ByteArray = seal(E2e.K_JSON, stream, obj.toString().toByteArray())

    /** -> Frame; throws ProtocolError if it does not decrypt. */
    fun open(ciphertext: ByteArray): Frame {
        val pt = try {
            dec.init(Cipher.DECRYPT_MODE, recvKey, GCMParameterSpec(128, E2e.nonce(rn)))
            dec.doFinal(ciphertext)
        } catch (e: Exception) {
            throw ProtocolError("a frame did not decrypt (wrong key, reordered or changed on the way)")
        }
        rn++
        if (pt.size < E2e.HEADER) throw ProtocolError("frame too short")
        val stream = ((pt[1].toLong() and 0xff) shl 24) or ((pt[2].toLong() and 0xff) shl 16) or
                     ((pt[3].toLong() and 0xff) shl 8) or (pt[4].toLong() and 0xff)
        return Frame(pt[0].toInt() and 0xff, stream, pt[5].toInt() and 0xff, pt.copyOfRange(E2e.HEADER, pt.size))
    }
}
