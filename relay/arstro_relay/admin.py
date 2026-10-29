"""Admin commands on the device manager's data directory (RELAY_DATA or --data).

    python3 -m arstro_relay admin list [--json]
    python3 -m arstro_relay admin forget ID
    python3 -m arstro_relay admin token [--rotate]

A running device manager picks the changes up within a few seconds (it watches the files).
In the container: docker exec <container> python -m arstro_relay admin list
"""

import argparse
import json
import os
import sys
import time

from .store import Store


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if isinstance(t, (int, float)) else "-"


def main(argv):
    ap = argparse.ArgumentParser(prog="python3 -m arstro_relay admin",
                                 description="Manage the device manager's data. A running device manager "
                                             "picks the changes up within a few seconds.")
    ap.add_argument("--data", default=os.environ.get("RELAY_DATA") or "/data",
                    help="the data directory (default: RELAY_DATA or /data)")
    sub = ap.add_subparsers(dest="cmd", metavar="list|forget|token")
    p = sub.add_parser("list", help="list the registered devices")
    p.add_argument("--json", action="store_true", help="print JSON (no secret hashes)")
    p = sub.add_parser("forget", help="remove a device; its id is free again and it is disconnected if online")
    p.add_argument("id")
    p = sub.add_parser("token", help="print the join token devices need")
    p.add_argument("--rotate", action="store_true", help="make a new join token (devices need it for their next connection)")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    if not os.path.isdir(args.data):
        print("no data directory at %s (set RELAY_DATA or use --data)" % args.data, file=sys.stderr)
        return 1
    store = Store(args.data)

    if args.cmd == "list":
        devices = store.devices()
        rows = [dict({k: v for k, v in r.items() if k != "secret_sha256"}, id=i) for i, r in sorted(devices.items())]
        if args.json:
            print(json.dumps(rows, indent=1))
            return 0
        fmt = "%-24s %-24s %-16s %-8s %-10s %-4s %s"
        print(fmt % ("ID", "NAME", "HOSTNAME", "PLATFORM", "VERSION", "SLOT", "LAST SEEN"))
        for r in rows:
            print(fmt % (r["id"], r.get("name") or "-", r.get("hostname") or "-", r.get("platform") or "-",
                         r.get("version") or "-", r.get("slot") or "-", _when(r.get("last_seen"))))
        print("%d device(s)" % len(rows))
        return 0

    if args.cmd == "forget":
        removed = []

        def change(devices):
            if args.id not in devices:
                return False
            removed.append(devices.pop(args.id))
        store.update(change)
        if not removed:
            print("no device with id %s (see: admin list)" % args.id, file=sys.stderr)
            return 1
        print("forgot %s (%s). If it is online, the device manager disconnects it within a few seconds.\n"
              "Its id is free again: the next device that joins with this id (and the join token) owns it.\n"
              "To keep a device out for good, also rotate the join token (admin token --rotate)."
              % (args.id, removed[0].get("name") or "-"))
        return 0

    env_token = os.environ.get("RELAY_JOIN_TOKEN")
    if args.rotate:
        if env_token:
            print("the join token comes from RELAY_JOIN_TOKEN - change it there and restart the device manager",
                  file=sys.stderr)
            return 1
        print(store.rotate_token())
        print("new join token saved. Devices online now stay connected; every device needs the new token "
              "for its next connection.", file=sys.stderr)
        return 0
    print(env_token or store.join_token())
    return 0
