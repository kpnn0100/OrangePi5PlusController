"""NTWB framing (spec.FRAMES): encode and incrementally decode frames and blob payloads.

Pure functions and one buffer class - no sockets - so the host, the Python SDK and the
tests share one implementation of the byte format.
"""

import json
import struct

from . import spec

T_JSON = spec.FRAMES["json"]["type"]
T_BLOB = spec.FRAMES["blob"]["type"]
_HDR = struct.Struct(">BI")
_BLOB = struct.Struct(">H")


class WireError(ValueError):
    pass


def encode_json(msg):
    payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _frame(T_JSON, payload)


def blob_payload(header, data):
    h = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(h) > spec.MAX_BLOB_HEADER:
        raise WireError("blob header too large (%d bytes)" % len(h))
    return _BLOB.pack(len(h)) + h + bytes(data)


def encode_blob(header, data):
    return _frame(T_BLOB, blob_payload(header, data))


def parse_blob(payload):
    """payload -> (header dict, data bytes)."""
    if len(payload) < 2:
        raise WireError("blob shorter than its header length")
    (n,) = _BLOB.unpack_from(payload, 0)
    if n > spec.MAX_BLOB_HEADER or 2 + n > len(payload):
        raise WireError("bad blob header length %d" % n)
    try:
        header = json.loads(payload[2:2 + n].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise WireError("blob header is not JSON: %s" % e)
    return header, bytes(payload[2 + n:])


def parse_json(payload):
    try:
        return json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise WireError("message is not JSON: %s" % e)


def _frame(ftype, payload):
    if len(payload) > spec.MAX_FRAME:
        raise WireError("frame too large (%d bytes)" % len(payload))
    return _HDR.pack(ftype, len(payload)) + payload


class Decoder:
    """Feed bytes, get ("json", dict) / ("blob", (header, data)) items."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf += data
        out = []
        while len(self._buf) >= _HDR.size:
            ftype, n = _HDR.unpack_from(self._buf, 0)
            if n > spec.MAX_FRAME:
                raise WireError("frame too large (%d bytes)" % n)
            if ftype not in (T_JSON, T_BLOB):
                raise WireError("unknown frame type %d" % ftype)
            if len(self._buf) < _HDR.size + n:
                break
            payload = bytes(self._buf[_HDR.size:_HDR.size + n])
            del self._buf[:_HDR.size + n]
            out.append(("json", parse_json(payload)) if ftype == T_JSON else ("blob", parse_blob(payload)))
        return out
