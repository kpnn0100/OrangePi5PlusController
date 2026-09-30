#!/usr/bin/env python3
"""Hello NTWB - the example app (and the fixture of server/tests/test_ntwb.py).

Launched by Arstro Remote with NTWB_SOCKET/NTWB_TOKEN set; uses the Python SDK.
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
from arstro_remote.ntwb.app import App, MethodError  # noqa: E402

# The manifest id the host launched us as (NTWB_APP_ID), so one program can be installed twice.
app = App(os.environ.get("NTWB_APP_ID", "hello"), "1.0.0", capabilities=["blobs"])
counter = {"n": 0}


def png(size):
    rows = b"".join(b"\x00" + bytes(v for x in range(size) for v in (x * 255 // size, y * 255 // size, 160))
                    for y in range(size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@app.method("add")
def add(params, client):
    if not isinstance(params.get("n"), int):
        raise MethodError("n must be an integer")
    counter["n"] += params["n"]
    app.state("counter", counter["n"])
    app.event("added", {"n": params["n"], "by": client})
    return counter["n"]


@app.method("echo")
def echo(params, _client):
    return params


@app.method("fail")
def fail(_params, _client):
    raise MethodError("this method always fails")


@app.method("picture")
def picture(params, client):
    size = max(8, min(int(params.get("size", 64)), 256))
    data = png(size)
    app.blob("picture", "image/png", data, meta={"size": size}, client=client)
    return {"bytes": len(data)}


@app.method("burst")
def burst(params, client):
    """n small blobs at once: without `coalesce` every one must arrive, in order."""
    n = max(1, min(int(params.get("n", 5)), 50))
    for i in range(n):
        app.blob("picture", "image/png", png(8), meta={"size": 8, "i": i}, client=client,
                 coalesce=bool(params.get("coalesce")))
    return {"sent": n}


@app.method("session")
def session(_params, _client):
    """Which session this process serves - as the host said at launch and in `welcome`."""
    return {"env": os.environ.get("NTWB_SESSION"), "welcome": app.session}


@app.method("quit")
def quit_(_params, _client):
    app.stop("asked to quit")


def welcome(_msg):
    app.state("counter", counter["n"])
    app.log("info", "hello app ready")


app.on_welcome = welcome
app.run()
