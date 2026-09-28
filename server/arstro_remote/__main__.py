"""arstro-remote command line.

    arstro-remote run [--no-bluetooth] [--debug]   start the daemon
    arstro-remote status                           show daemon / Bluetooth status
    arstro-remote pair [SECONDS]                   open the pairing window (-1 = forever, 0 = close)
    arstro-remote unpair ADDRESS                   forget a paired phone
    arstro-remote call OP [JSON]                   send any protocol request (debugging)
    arstro-remote version
"""

import argparse
import json
import sys

from . import __version__


def _client():
    from .client import Client
    from .daemon import control_socket_path
    try:
        return Client.unix(control_socket_path())
    except OSError as e:
        sys.exit("arstro-remote: daemon not reachable at %s (%s)" % (control_socket_path(), e))


def _fmt_status(st):
    bt = st["bluetooth"]
    lines = ["Arstro Remote %s  pid %s  up %ss" % (st["version"], st["pid"], st["uptime"])]
    if bt.get("ready"):
        rem = bt["pairing_remaining"]
        pairing = "open (forever)" if rem < 0 else "open (%ds left)" % rem if bt["pairing_open"] else "closed"
        lines += [
            "Bluetooth  : %s  %s  channel %s" % (bt["alias"], bt["address"], bt["channel"]),
            "Powered    : %s   Discoverable: %s   Pairable: %s" % (bt["powered"], bt["discoverable"],
                                                                bt["pairable"]),
            "Pairing    : %s" % pairing,
            "Paired     : %s" % (", ".join("%s (%s)%s%s" % (d["name"], d["address"],
                                                            "" if d["trusted"] else " NOT trusted",
                                                            " connected" if d["connected"] else "")
                                           for d in bt["paired_devices"]) or "none"),
        ]
    else:
        lines.append("Bluetooth  : NOT READY %s" % bt.get("error", ""))
    inp = st["input"]
    lines.append("Input      : %s (%s)" % (inp["backend"], "ok" if inp["available"] else "unavailable"))
    lines.append("Sessions   : %d" % len(st["sessions"]))
    for s in st["sessions"]:
        lines.append("  #%d %s %s up %ss in %dB out %dB" % (s["num"], s["peer"], s["client"].get("app") or "",
                                                          s["connected_for"], s["bytes_in"], s["bytes_out"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arstro-remote", description="Arstro Remote System (Orange Pi side)")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="start the daemon")
    run.add_argument("--no-bluetooth", action="store_true", help="control socket only (testing)")
    run.add_argument("--debug", action="store_true")
    sub.add_parser("status")
    pair = sub.add_parser("pair")
    pair.add_argument("seconds", nargs="?", type=int, default=None)
    unpair = sub.add_parser("unpair")
    unpair.add_argument("address")
    call = sub.add_parser("call")
    call.add_argument("op")
    call.add_argument("params", nargs="?", default="{}")
    sub.add_parser("version")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        from .daemon import Daemon, load_config, setup_logging
        setup_logging(args.debug)
        Daemon(load_config(), use_bluetooth=not args.no_bluetooth).run()
    elif args.cmd == "status":
        c = _client()
        print(_fmt_status(c.call("admin.status")))
    elif args.cmd == "pair":
        c = _client()
        params = {} if args.seconds is None else {"seconds": args.seconds}
        print(_fmt_status(c.call("admin.pair", **params)))
    elif args.cmd == "unpair":
        print(json.dumps(_client().call("admin.unpair", address=args.address)))
    elif args.cmd == "call":
        resp = _client().request(args.op, **json.loads(args.params))
        print(json.dumps(resp, indent=2))
        return 0 if resp.get("ok") else 1
    elif args.cmd == "version":
        print(__version__)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
