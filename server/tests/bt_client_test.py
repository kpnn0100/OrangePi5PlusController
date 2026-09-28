#!/usr/bin/env python3
"""Real-radio test: talk to the Pi daemon over Bluetooth RFCOMM from a Linux box.

    python3 server/tests/bt_client_test.py <PI_BT_ADDRESS> [channel]

The client machine must already be paired with the Pi (bluetoothctl pair ...).
Runs the protocol over the air: handshake, latency, stats, a shell round trip,
terminal throughput, pointer query and Wi-Fi status.
"""

import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from arstro_remote.client import Client  # noqa: E402


def connect(addr, channel, attempts=6):
    last = None
    for i in range(attempts):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(15)
        try:
            t0 = time.time()
            s.connect((addr, channel))
            s.settimeout(None)
            print("connected over RFCOMM ch %d in %.1fs (attempt %d)" % (channel, time.time() - t0, i + 1))
            return s
        except OSError as e:
            last = e
            print("  connect attempt %d failed: %s" % (i + 1, e))
            s.close()
            time.sleep(2)
    raise SystemExit("could not connect: %s" % last)


def main():
    addr = sys.argv[1]
    channel = int(sys.argv[2]) if len(sys.argv) > 2 else 22
    c = Client(connect(addr, channel))
    ok = True

    h = c.call("hello", app="bt-client-test", version="1", client_id="bt-client-test")
    print("hello: %s %s on %s, input=%s" % (h["name"], h["version"], h["hostname"], h["input"]))

    lat = []
    for _ in range(20):
        t0 = time.time()
        c.call("ping")
        lat.append((time.time() - t0) * 1000)
    lat.sort()
    print("ping over BT: min %.0f ms, median %.0f ms, max %.0f ms" % (lat[0], lat[len(lat) // 2], lat[-1]))

    s = c.call("stats.get")
    print("stats: ip=%s soc=%.1fC cpu=%s%% wifi=%s" % (s["network"]["ip"], s["temps"]["soc"],
                                                      s["cpu"]["percent"], s["wifi"].get("ssid")))

    tid = c.call("term.open", cols=100, rows=30)["term"]
    c.term_write(tid, b"echo BT_SHELL_$((20+22))\n")
    got = c.wait_for(lambda: b"BT_SHELL_42" in bytes(c.term_output.get(tid, b"")), 10)
    print("shell round trip: %s" % ("OK" if got else "FAILED"))
    ok &= got

    c.term_output[tid] = bytearray()
    t0 = time.time()
    c.term_write(tid, b"seq 1 30000; echo THROUGHPUT_DONE\n")
    got = c.wait_for(lambda: re.search(rb"30000\r?\nTHROUGHPUT_DONE", bytes(c.term_output.get(tid, b""))) is not None, 90)
    dt = time.time() - t0
    n = len(c.term_output.get(tid, b""))
    print("terminal throughput: %d bytes in %.1fs = %.1f KB/s %s" % (n, dt, n / dt / 1024, "OK" if got else "FAILED"))
    ok &= got
    c.call("term.close", term=tid)

    p = c.call("in.pointer")
    print("pointer: %s,%s on %sx%s" % (p["x"], p["y"], p["width"], p["height"]))

    w = c.call("wifi.status")
    print("wifi: connected=%s ssid=%s ip=%s" % (w["connected"], w["ssid"], w["ip"]))

    c.close()
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
