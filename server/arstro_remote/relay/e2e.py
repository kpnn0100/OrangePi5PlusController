"""The end-to-end channel between a viewer and a machine, through the device manager (RLY-03).

The one Python definition of the relay protocol's crypto, framing and message names.
`docs/relay/PROTOCOL.md` describes it; `docs/relay/vectors.json` is generated from this file

    python3 -m arstro_remote.relay.e2e vectors > docs/relay/vectors.json

and both implementations (this one and the browser's `relay/arstro_relay/static/e2e.js`) are
tested against it, so they cannot drift apart.

The device manager (relay) never sees the access key: it forwards the hello / proof messages
and the encrypted frames unchanged.

    viewer                                   device
    hello  {v, cn}                  ->
                                    <-       hello {v, sn, proof(device)}   (or error)
    (checks the device's proof first: a wrong key or a fake device stops here)
    proof  {proof(viewer)}          ->
                                    <-       [encrypted] ready {...}        (or error "wrong key")
    [encrypted frames both ways]

Keys: HKDF-SHA256(ikm = access key, salt = cn || sn, info = LABEL) -> 96 bytes =
viewer->device key | device->viewer key | proof key. Frames: AES-256-GCM, nonce = 4 zero
bytes + a 64-bit big-endian counter per direction starting at 0 (implicit: the WebSocket keeps
order), no associated data. A frame that does not decrypt ends the channel.

Needs `cryptography` (Debian / Ubuntu / Armbian: python3-cryptography): the standard library
has no AES-GCM. Imports nothing else from arstro_remote, so the standalone client can carry it.
"""

import base64
import hashlib
import hmac
import json
import secrets
import struct
import sys

VERSION = 1
LABEL = b"arstro-relay/1"
KEY_PREFIX = "ak1_"
KEY_LEN = 32
NONCE_LEN = 32            # cn / sn
CHUNK = 64 * 1024         # largest DATA payload in one frame
WINDOW = 512 * 1024       # unacknowledged DATA bytes per stream and direction
MAX_MESSAGE = 1 << 20     # largest WebSocket message anywhere on the path (the relay enforces it)

K_JSON = 1                # payload: UTF-8 JSON object with "t"
K_DATA = 2                # payload: bytes of a stream
F_FIN = 1                 # DATA of a ws stream: last fragment of a message
F_TEXT = 2                # DATA of a ws stream: the message is text (UTF-8)

HEADER = struct.Struct(">BIB")     # kind u8 | stream u32 | flags u8

# Plain JSON messages the relay itself reads or writes (device <-> relay, viewer <-> relay).
# name -> (connection, direction, meaning). docs/relay/PROTOCOL.md lists every one of them.
RELAY_MESSAGES = {
    "hello":   ("agent", "device->relay", "first message: id, name, secret, token, version, hostname, platform"),
    "welcome": ("agent", "relay->device", "registration accepted: heartbeat seconds, relay version"),
    "open":    ("agent", "relay->device", "a viewer waits on channel sid (addr = viewer address): dial a data socket"),
    "refuse":  ("agent", "device->relay", "the device will not take channel sid (error says why)"),
    "ping":    ("agent", "device->relay", "keep-alive, every heartbeat seconds"),
    "pong":    ("agent", "relay->device", "answer to ping"),
    "bye":     ("agent", "relay->device", "the relay closes the connection (reason)"),
    "data":    ("agent/data", "device->relay", "first message of a data socket: id, secret, sid"),
    "linked":  ("agent/data", "relay->device", "the data socket is joined to the viewer; forwarding starts"),
    "relay":   ("connect", "relay->viewer", "ok: true when joined to the device, else error; forwarding starts"),
    "error":   ("any", "relay->device|viewer", "refused (error says why); the relay closes the socket"),
}

# Messages of the end-to-end channel. "hello" / "proof" / "error" travel as plain text messages
# during the handshake; everything else is a K_JSON frame inside the encryption.
CHANNEL_MESSAGES = {
    "hello":  ("handshake", "both", "viewer: v, cn. device: v, sn, proof (HMAC of the proof key, 'device')"),
    "proof":  ("handshake", "viewer->device", "HMAC of the proof key, 'viewer'"),
    "error":  ("handshake|stream", "device->viewer", "refused: wrong key, too many viewers, port not allowed, ... (ends the stream / channel)"),
    "ready":  ("stream 0", "device->viewer", "first encrypted frame: v, id, name, hostname, version, slot, ports (allow-list or null)"),
    "ping":   ("stream 0", "both", "keep-alive; answered with pong"),
    "pong":   ("stream 0", "both", "answer to ping"),
    "http":   ("stream", "viewer->device", "open an HTTP request: method, port, path, headers [[k, v]], body (bool)"),
    "res":    ("stream", "device->viewer", "HTTP response head: status, reason, headers [[k, v]]"),
    "ws":     ("stream", "viewer->device", "open a WebSocket: port, path, headers [[k, v]], protocols"),
    "tcp":    ("stream", "viewer->device", "open a raw TCP connection: port"),
    "open":   ("stream", "device->viewer", "the ws / tcp connection is open (protocol for ws)"),
    "end":    ("stream", "both", "no more DATA from this side (request body, response body, tcp half-close)"),
    "close":  ("stream", "both", "ws: close with code, reason; tcp: abort. Ends the stream"),
    "cancel": ("stream", "viewer->device", "the viewer gives up the stream; the device drops the local connection"),
    "ack":    ("stream", "both", "n DATA bytes of this stream were consumed (flow control, WINDOW)"),
    "ports":  ("stream", "both", "viewer: list the listening TCP ports. device: ports [{port, addr, proc}] (ends the stream)"),
}


class ProtocolError(Exception):
    """The peer broke the protocol (bad message, bad frame): the channel is closed."""


class WrongKey(Exception):
    """The proof does not match: the other side has a different access key."""


# ------------------------------------------------------------------ encoding helpers
def b64e(data):
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode()


def b64d(text):
    text = str(text)
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as e:
        raise ProtocolError("bad base64: %s" % e)


# ------------------------------------------------------------------ the access key
def new_key():
    """A new access key as text: 'ak1_' + 43 base64url characters (32 random bytes)."""
    return KEY_PREFIX + b64e(secrets.token_bytes(KEY_LEN))


def parse_key(text):
    """Key text -> 32 bytes. Spaces and line breaks from copy-paste are ignored."""
    t = "".join(str(text or "").split())
    if not t.startswith(KEY_PREFIX):
        raise ValueError("an access key starts with %s" % KEY_PREFIX)
    try:
        raw = base64.urlsafe_b64decode(t[len(KEY_PREFIX):] + "=" * (-len(t[len(KEY_PREFIX):]) % 4))
    except (ValueError, TypeError):
        raise ValueError("the access key has characters that do not belong in it")
    if len(raw) != KEY_LEN:
        raise ValueError("the access key is too short or too long - copy all of it")
    return raw


def key_id(raw):
    """A short public fingerprint of a key, to tell keys apart in a UI. Not secret, not the key."""
    return b64e(hashlib.sha256(LABEL + b" key-id " + bytes(raw)).digest()[:6])


# ------------------------------------------------------------------ crypto
def derive(raw_key, cn, sn):
    """-> (viewer->device key, device->viewer key, proof key), 32 bytes each."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    if len(cn) != NONCE_LEN or len(sn) != NONCE_LEN:
        raise ProtocolError("cn and sn must be %d bytes" % NONCE_LEN)
    okm = HKDF(algorithm=hashes.SHA256(), length=96, salt=bytes(cn) + bytes(sn), info=LABEL).derive(bytes(raw_key))
    return okm[:32], okm[32:64], okm[64:]


def proof(proof_key, role):
    """HMAC-SHA256(proof key, LABEL + ' ' + role), role 'device' or 'viewer'."""
    return hmac.new(proof_key, LABEL + b" " + role.encode(), hashlib.sha256).digest()


def nonce(counter):
    return b"\0\0\0\0" + struct.pack(">Q", counter)


class Secure:
    """One side of an established channel: seal outgoing frames, open incoming ones."""

    def __init__(self, send_key, recv_key):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._sn = 0
        self._rn = 0

    def seal(self, kind, stream, payload=b"", flags=0):
        pt = HEADER.pack(kind, stream, flags) + bytes(payload)
        ct = self._send.encrypt(nonce(self._sn), pt, None)
        self._sn += 1
        return ct

    def seal_json(self, stream, obj):
        return self.seal(K_JSON, stream, json.dumps(obj, separators=(",", ":")).encode())

    def open(self, ciphertext):
        """-> (kind, stream, flags, payload); raises ProtocolError if it does not decrypt."""
        from cryptography.exceptions import InvalidTag
        try:
            pt = self._recv.decrypt(nonce(self._rn), bytes(ciphertext), None)
        except InvalidTag:
            raise ProtocolError("a frame did not decrypt (wrong key, reordered or changed on the way)")
        self._rn += 1
        if len(pt) < HEADER.size:
            raise ProtocolError("frame too short")
        kind, stream, flags = HEADER.unpack_from(pt)
        return kind, stream, flags, pt[HEADER.size:]


def decode_json(payload):
    try:
        msg = json.loads(bytes(payload).decode())
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError("bad JSON frame: %s" % e)
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("a JSON frame must be an object with t")
    return msg


# ------------------------------------------------------------------ handshake
def viewer_hello():
    """-> (cn, message). The viewer sends the message as a text WebSocket message."""
    cn = secrets.token_bytes(NONCE_LEN)
    return cn, {"t": "hello", "v": VERSION, "cn": b64e(cn)}


def device_answer(raw_key, hello):
    """Device side, on the viewer's hello -> (state, message to send). state goes to device_check."""
    if hello.get("t") != "hello" or hello.get("v") != VERSION:
        raise ProtocolError("expected hello v%d" % VERSION)
    cn = b64d(hello.get("cn", ""))
    sn = secrets.token_bytes(NONCE_LEN)
    v2d, d2v, pk = derive(raw_key, cn, sn)
    msg = {"t": "hello", "v": VERSION, "sn": b64e(sn), "proof": b64e(proof(pk, "device"))}
    return (v2d, d2v, pk), msg


def viewer_check(raw_key, cn, answer):
    """Viewer side, on the device's hello -> (Secure, proof message). Raises WrongKey when the
    device does not prove the key - nothing derived from the key has been sent at that point."""
    if answer.get("t") == "error":
        raise ProtocolError(str(answer.get("error") or "refused"))
    if answer.get("t") != "hello" or answer.get("v") != VERSION:
        raise ProtocolError("expected hello v%d" % VERSION)
    v2d, d2v, pk = derive(raw_key, cn, b64d(answer.get("sn", "")))
    if not hmac.compare_digest(proof(pk, "device"), b64d(answer.get("proof", ""))):
        raise WrongKey("wrong access key for this device")
    return Secure(v2d, d2v), {"t": "proof", "proof": b64e(proof(pk, "viewer"))}


def device_check(state, msg):
    """Device side, on the viewer's proof -> Secure. Raises WrongKey."""
    v2d, d2v, pk = state
    if msg.get("t") != "proof":
        raise ProtocolError("expected proof")
    if not hmac.compare_digest(proof(pk, "viewer"), b64d(msg.get("proof", ""))):
        raise WrongKey("the viewer has a different access key")
    return Secure(d2v, v2d)


# ------------------------------------------------------------------ test vectors
def vectors():
    """Deterministic vectors for docs/relay/vectors.json (no randomness)."""
    raw = bytes(range(KEY_LEN))
    cn = bytes([0x11]) * NONCE_LEN
    sn = bytes([0x22]) * NONCE_LEN
    v2d, d2v, pk = derive(raw, cn, sn)
    viewer = Secure(v2d, d2v)
    device = Secure(d2v, v2d)
    frames = [
        ("viewer", K_JSON, 1, 0, json.dumps({"t": "http", "method": "GET", "port": 8080, "path": "/",
                                            "headers": [["accept", "*/*"]], "body": False},
                                           separators=(",", ":")).encode()),
        ("viewer", K_DATA, 3, F_FIN | F_TEXT, "héllo".encode()),
        ("device", K_JSON, 0, 0, b'{"t":"ready","v":1}'),
        ("device", K_DATA, 1, 0, bytes(range(256)) * 2),
    ]
    out = []
    for side, kind, stream, flags, payload in frames:
        ct = (viewer if side == "viewer" else device).seal(kind, stream, payload, flags)
        out.append({"from": side, "kind": kind, "stream": stream, "flags": flags,
                    "payload": b64e(payload), "ciphertext": b64e(ct)})
    return {
        "note": "generated by python3 -m arstro_remote.relay.e2e vectors - do not edit",
        "version": VERSION, "label": LABEL.decode(),
        "key": KEY_PREFIX + b64e(raw), "key_id": key_id(raw),
        "cn": b64e(cn), "sn": b64e(sn),
        "viewer_to_device_key": b64e(v2d), "device_to_viewer_key": b64e(d2v), "proof_key": b64e(pk),
        "proof_device": b64e(proof(pk, "device")), "proof_viewer": b64e(proof(pk, "viewer")),
        "frames": out,
        "note_frames": "each side's counter starts at 0; frames of one side are sealed in this order",
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["vectors"]:
        print(json.dumps(vectors(), indent=2, ensure_ascii=False))
    elif sys.argv[1:] == ["key"]:
        print(new_key())
    else:
        print("usage: python3 -m arstro_remote.relay.e2e vectors|key", file=sys.stderr)
        sys.exit(2)
