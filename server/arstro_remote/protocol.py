"""Arstro Remote wire protocol.

Every message on the stream is a frame:

    +---------+------------------+-----------------+
    | type u8 | length u32 (BE)  | payload         |
    +---------+------------------+-----------------+

Frame types:
    0x01 JSON      - UTF-8 JSON object (requests, responses, events, input)
    0x02 TERM      - keyboard bytes for a shell (client -> Pi),
                     payload = [term_id u8][raw bytes...]
    0x03 TERM_OUT  - shell output (Pi -> client),
                     payload = [term_id u8][offset u64 BE][raw bytes...]
                     offset = position of the first byte in the shell's output
                     stream, so a client can drop bytes it already has (replays
                     after a reconnect can never show up twice).
    0x04 VIDEO     - encoded video access unit (recorder worker -> daemon only),
                     payload = [flags u8 (bit0 keyframe)][pts_us u64 BE][Annex-B bytes]

JSON conventions:
    request   {"id": 7, "op": "wifi.scan", ...params}
    response  {"id": 7, "ok": true,  "data": {...}}
              {"id": 7, "ok": false, "error": "message"}
    notify    {"op": "in.move", "dx": 3, "dy": -1}      (no id -> no response)
    event     {"ev": "stats", "data": {...}}             (server push)
"""

import json
import struct

PROTO_VERSION = 2   # 2: shared terminals, state events, recorder/gallery/web ops

T_JSON = 0x01
T_TERM = 0x02
T_TERM_OUT = 0x03
T_VIDEO = 0x04

HEADER = struct.Struct(">BI")
OFFSET = struct.Struct(">Q")
MAX_FRAME = 8 << 20  # 8 MiB (a 4K keyframe fits)


class ProtocolError(Exception):
    pass


def encode(ftype: int, payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME:
        raise ProtocolError("frame too large: %d" % len(payload))
    return HEADER.pack(ftype, len(payload)) + payload


def encode_json(obj) -> bytes:
    return encode(T_JSON, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def encode_term(term_id: int, data: bytes) -> bytes:
    return encode(T_TERM, bytes([term_id & 0xFF]) + data)


def encode_term_out(term_id: int, offset: int, data: bytes) -> bytes:
    return encode(T_TERM_OUT, bytes([term_id & 0xFF]) + OFFSET.pack(offset) + data)


VIDEO_HEAD = struct.Struct(">BQ")


def encode_video(keyframe: bool, pts_us: int, data: bytes) -> bytes:
    return encode(T_VIDEO, VIDEO_HEAD.pack(1 if keyframe else 0, max(0, int(pts_us))) + data)


def decode_video(payload: bytes):
    """-> (keyframe, pts_us, data)"""
    flags, pts = VIDEO_HEAD.unpack_from(payload, 0)
    return bool(flags & 1), pts, payload[VIDEO_HEAD.size:]


def decode_term_out(payload: bytes):
    """-> (term_id, offset, data)"""
    return payload[0], OFFSET.unpack_from(payload, 1)[0], payload[1 + OFFSET.size:]


class FrameDecoder:
    """Incremental decoder: feed() raw bytes, get back complete frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf += data
        frames = []
        while len(self._buf) >= HEADER.size:
            ftype, length = HEADER.unpack_from(self._buf, 0)
            if ftype not in (T_JSON, T_TERM, T_TERM_OUT, T_VIDEO):
                raise ProtocolError("unknown frame type 0x%02x" % ftype)
            if length > MAX_FRAME:
                raise ProtocolError("frame too large: %d" % length)
            end = HEADER.size + length
            if len(self._buf) < end:
                break
            payload = bytes(self._buf[HEADER.size:end])
            del self._buf[:end]
            frames.append((ftype, payload))
        return frames
