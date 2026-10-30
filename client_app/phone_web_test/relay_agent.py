#!/usr/bin/env python3
"""A device agent for viewer_e2e.mjs: advertises a "phone" to a local device manager and forwards
its channel's streams to the mock phone's port - the role PhoneService's RelayAgent plays on
Android (docs/phone/PHONE.md), here with Arstro Remote's Python agent (server/arstro_remote/relay).

    PYTHONPATH=server python3 relay_agent.py --relay http://127.0.0.1:8090/arstro/internal/devices \
        --token <join token> --port <mock port>

Prints "AGENT <device id> <access key>" at once, then runs until killed.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))

from arstro_remote.relay import e2e  # noqa: E402
from arstro_remote.relay.agent import Agent  # noqa: E402
from arstro_remote.relay.service import new_identity  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    ident = new_identity()
    print("AGENT %s %s" % (ident["id"], ident["key"]), flush=True)
    agent = Agent()
    agent.configure({"url": args.relay, "token": args.token, "name": "Test phone", "enabled": True, "ports": [args.port],
                     "id": ident["id"], "secret": ident["secret"], "key_raw": e2e.parse_key(ident["key"]),
                     "version": "phone-web-test", "hostname": "test-phone", "platform": "android", "slot": None,
                     "web": args.port, "blocked": None})
    agent.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        agent.stop()


if __name__ == "__main__":
    main()
