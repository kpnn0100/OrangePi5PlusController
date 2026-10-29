#!/usr/bin/env python3
"""The device manager's documents stay in sync with its one definition (RLY-03, RLY-26).

e2e.py defines the messages and the crypto; PROTOCOL.md must name every message and route,
vectors.json must be the generator's output, docs/protocol.md every relay.* op, AGENT_API.md
every client command, relay/README.md every RELAY_* setting the server reads.
"""

import glob
import json
import os
import re

from harness import check, run, test

from arstro_remote.relay import client, e2e

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def named(doc, name):
    return ('"t":"%s"' % name) in doc or ("`%s`" % name) in doc or ("`%s " % name) in doc


@test("RLY-03")
def protocol_md_names_every_message():
    doc = read("docs", "relay", "PROTOCOL.md")
    missing = [n for n in list(e2e.RELAY_MESSAGES) + list(e2e.CHANNEL_MESSAGES) if not named(doc, n)]
    check(not missing, "PROTOCOL.md does not describe: %s" % ", ".join(missing))
    for name, value in (("CHUNK", "64 KiB"), ("WINDOW", "512 KiB"), ("MAX_MESSAGE", "1 MiB")):
        check(re.search(r"`%s`\s*\|\s*%s" % (name, re.escape(value)), doc),
              "PROTOCOL.md constants table: %s should be %s" % (name, value))
    check(e2e.CHUNK == 64 * 1024 and e2e.WINDOW == 512 * 1024 and e2e.MAX_MESSAGE == 1 << 20,
          "e2e.py constants changed: update PROTOCOL.md section 7 and this test")


@test("RLY-03")
def vectors_json_is_the_generator_output():
    on_disk = json.loads(read("docs", "relay", "vectors.json"))
    check(on_disk == json.loads(json.dumps(e2e.vectors())),
          "docs/relay/vectors.json is stale: python3 -m arstro_remote.relay.e2e vectors > ../docs/relay/vectors.json")


@test("RLY-21", "RLY-26")
def relay_server_has_every_documented_route():
    doc = read("docs", "relay", "PROTOCOL.md")
    routes = sorted(set(re.findall(r"`(?:GET|POST) B(/[a-z./_-]*)", doc)))
    check(len(routes) >= 10, "PROTOCOL.md section 1 should list the routes (found %s)" % routes)
    src = "".join(read(os.path.relpath(p, ROOT)) for p in glob.glob(os.path.join(ROOT, "relay", "arstro_relay", "*.py")))
    missing = [r for r in routes if r not in ("", "/") and r.split("<")[0].rstrip("/") not in src]
    check(not missing, "routes in PROTOCOL.md but not in relay/arstro_relay: %s" % missing)


@test("RLY-06", "RLY-08")
def protocol_md_lists_every_relay_op():
    src = read("server", "arstro_remote", "relay", "service.py")
    ops = sorted("relay." + m.replace("_", ".") for m in re.findall(r"def op_relay_(\w+)", src))
    check(ops, "service.py has no op_relay_* methods")
    doc = read("docs", "protocol.md")
    missing = [op for op in ops if "`%s`" % op not in doc]
    check(not missing, "docs/protocol.md does not list: %s" % missing)
    check("| `relay` |" in doc, "docs/protocol.md: the state topic `relay` is missing")
    check("RLY-" in read("docs", "parity.md"), "docs/parity.md has no Internet access row")


@test("RLY-26")
def agent_api_documents_every_client_command():
    doc = read("docs", "relay", "AGENT_API.md")
    missing = [c for c in client.COMMANDS if "| `%s" % c not in doc]
    check(not missing, "AGENT_API.md commands table lacks: %s" % missing)
    for var in ("ARSTRO_RELAY_URL", "ARSTRO_RELAY_KEY", "ARSTRO_RELAY_PASSWORD"):
        check(var in doc and var in read("server", "arstro_remote", "relay", "client.py"), var)


@test("RLY-20")
def relay_readme_documents_every_setting():
    src = "".join(read(os.path.relpath(p, ROOT)) for p in glob.glob(os.path.join(ROOT, "relay", "arstro_relay", "*.py")))
    used = sorted(set(re.findall(r"RELAY_[A-Z_]+", src)))
    check(used, "the relay server reads no RELAY_* settings?")
    readme = read("relay", "README.md")
    missing = [v for v in used if v not in readme]
    check(not missing, "relay/README.md does not document: %s" % missing)


if __name__ == "__main__":
    raise SystemExit(run(description=__doc__))
