"""NTWB - the native-to-web bridge protocol - the ONE definition (NTWB-01).

Everything else is generated from or checked against the tables in this file:

  * the host validates every message it receives with `validate()` (NTWB-06),
  * `docs/ntwb/api.json` and `docs/ntwb/API.md` are rendered from them
    (`python3 -m arstro_remote.ntwb.spec --write docs/ntwb`), and a test fails when the
    committed copies differ from what this file produces (NTWB-10),
  * the C++ client (arstro `core/Ntwb`) vendors `api.json` and its own test checks its
    message table against it; the web SDK (`ntwb.js`) is checked the same way.

A message, a field, an environment variable or a manifest key that is not in these tables
does not exist - add it here first, then regenerate, then implement.

    python3 -m arstro_remote.ntwb.spec --json        # the catalogue, machine-readable
    python3 -m arstro_remote.ntwb.spec --markdown    # the rendered reference
    python3 -m arstro_remote.ntwb.spec --write DIR   # both files into DIR
"""

import json
import re
import sys

VERSION = "1.0.0"
MAJOR = 1

# ------------------------------------------------------------------------------ framing
# App <-> host byte stream (a Unix-domain stream socket):
#
#     +---------+------------------+-------------------------------+
#     | type u8 | length u32 (BE)  | payload (length bytes)        |
#     +---------+------------------+-------------------------------+
#
# Browser <-> host: WebSocket; a JSON message is a text frame, a blob a binary frame whose
# payload is the blob payload below (no type/length prefix - WebSocket frames already).
FRAMES = {
    "json": {"type": 1, "payload": "one UTF-8 JSON object: a message from MESSAGES"},
    "blob": {"type": 2, "payload": "[header length u16 BE][header: UTF-8 JSON object, see BLOB_HEADER][data bytes]"},
}
MAX_FRAME = 64 << 20          # bytes of payload, per frame
MAX_BLOB_HEADER = 16 << 10    # bytes of the blob's JSON header

# ------------------------------------------------------------------------------ roles
ROLES = {
    "host": "Arstro Remote: registers apps, launches them, serves their web UI and relays between app and clients",
    "app": "a native program with an NTWB adapter; it keeps its core and state, the host never interprets its methods",
    "client": "a browser page (the app's web UI, with ntwb.js) or any other front end that speaks the WebSocket side",
}
# direction codes used in MESSAGES
DIRECTIONS = {
    "a2h": "app -> host (socket)",
    "h2a": "host -> app (socket)",
    "c2h": "client -> host (WebSocket)",
    "h2c": "host -> client (WebSocket)",
}

# ------------------------------------------------------------------------------ types
# str · int · num · bool · obj (JSON object) · any (any JSON value) · list[str]
# enum:a|b|c · id (str: [A-Za-z0-9._:-]{1,64}) · name (str: [a-z][a-z0-9._-]{0,63})
TYPES = {
    "str": "a JSON string",
    "int": "a JSON integer",
    "num": "a JSON number",
    "bool": "true or false",
    "obj": "a JSON object",
    "any": "any JSON value, null included",
    "list[str]": "a JSON array of strings",
    "id": "string of 1-64 characters from A-Z a-z 0-9 . _ : -",
    "name": "string: a lower-case letter, then up to 63 of a-z 0-9 . _ -",
    "version": "string: semantic version MAJOR.MINOR.PATCH",
    "enum": "one of the listed strings",
}


def F(type_, required, doc):
    return {"type": type_, "required": required, "doc": doc}


# Every message is a JSON object with "t" = its name. Fields not listed are an error
# (NTWB-06: an unknown input is rejected, never accepted quietly).
MESSAGES = {
    # ---- session ---------------------------------------------------------------------
    "hello": {
        "dir": ["a2h"],
        "doc": "First message on an app connection. The host answers `welcome`, or `error` and closes.",
        "fields": {
            "ntwb": F("version", True, "protocol version the app speaks; the MAJOR must equal the host's"),
            "app": F("name", True, "the app id, as in its manifest"),
            "version": F("str", True, "the app's own version (shown to users)"),
            "token": F("str", False, "NTWB_TOKEN from the environment when the host launched the app; "
                                     "an app started by hand may attach without one if its manifest allows `attach`"),
            "pid": F("int", False, "process id, for the host's process view"),
            "capabilities": F("list[str]", False, "optional features the app uses: see CAPABILITIES"),
        },
    },
    "welcome": {
        "dir": ["h2a"],
        "doc": "The host accepted `hello`. From here on both sides may send any message of their direction.",
        "fields": {
            "ntwb": F("version", True, "protocol version of the host"),
            "session": F("id", True, "this app connection"),
            "host": F("obj", True, "{name, version} of the host"),
            "clients": F("list[str]", True, "clients already waiting for the app; each also gets `client.open`"),
        },
    },
    "ready": {
        "dir": ["h2c"],
        "doc": "The app is connected. Carries the app's retained state, so a client is complete at once.",
        "fields": {
            "ntwb": F("version", True, "protocol version of the host"),
            "client": F("id", True, "this client's id (what the app sees in `call.client`)"),
            "app": F("obj", True, "{id, name, version} of the connected app"),
            "state": F("obj", True, "every retained state key -> its latest data (see `state`)"),
        },
    },
    "status": {
        "dir": ["h2c"],
        "doc": "Where the app's process is: sent on connect and on every change.",
        "fields": {
            "state": F("enum:starting|running|stopped|failed", True, "starting = launched, waiting for `hello`"),
            "detail": F("str", False, "why (exit code, launch error, ...)"),
        },
    },
    "client.open": {
        "dir": ["h2a"],
        "doc": "A client attached to the app.",
        "fields": {
            "client": F("id", True, "the client"),
            "info": F("obj", False, "{peer, controller}: where it connects from"),
        },
    },
    "client.close": {
        "dir": ["h2a"],
        "doc": "A client went away; per-client resources (subscriptions, streams) can be freed.",
        "fields": {"client": F("id", True, "the client")},
    },
    # ---- request / reply ---------------------------------------------------------------
    "call": {
        "dir": ["c2h", "h2a"],
        "doc": "A request. The host adds `client` and forwards it to the app; the app must answer with "
               "`result` carrying the same `id`. Methods are the app's own API (its manifest `api` lists them).",
        "fields": {
            "id": F("id", True, "chosen by the caller, unique among its open calls"),
            "method": F("name", True, "the app method"),
            "params": F("obj", False, "the method's arguments (default {})"),
            "client": F("id", False, "set by the host towards the app; a client never sends it"),
        },
    },
    "result": {
        "dir": ["a2h", "h2c"],
        "doc": "The answer to one `call`. The host routes it to the client that made the call.",
        "fields": {
            "id": F("id", True, "the call's id"),
            "ok": F("bool", True, "false = the call failed; `error` says why"),
            "data": F("any", False, "the method's result when ok"),
            "error": F("str", False, "a sentence a user understands, when not ok"),
        },
    },
    "notify": {
        "dir": ["c2h", "h2a"],
        "doc": "A request without a reply (pointer moves, slider drags). Same routing as `call`.",
        "fields": {
            "method": F("name", True, "the app method"),
            "params": F("obj", False, "the method's arguments (default {})"),
            "client": F("id", False, "set by the host towards the app"),
        },
    },
    # ---- app -> clients ----------------------------------------------------------------
    "event": {
        "dir": ["a2h", "h2c"],
        "doc": "Something happened. To every client of the app, or to one when `client` is set.",
        "fields": {
            "name": F("name", True, "the event's name in the app's API"),
            "data": F("any", False, "payload"),
            "client": F("id", False, "towards the host only: deliver to this client alone"),
        },
    },
    "state": {
        "dir": ["a2h", "h2c"],
        "doc": "Observable state. The host retains the newest `data` per `key` and replays all keys in "
               "`ready`, so a client that (re)connects is complete without asking. `data: null` deletes the key.",
        "fields": {
            "key": F("name", True, "state key in the app's API"),
            "data": F("any", True, "the whole new value (not a delta)"),
        },
    },
    # ---- housekeeping ------------------------------------------------------------------
    "log": {
        "dir": ["a2h"],
        "doc": "A line for the host's log of this app (System > Logs, `arstro-remote apps log`).",
        "fields": {
            "level": F("enum:debug|info|warning|error", True, "severity"),
            "msg": F("str", True, "the line; never secrets"),
        },
    },
    "ping": {
        "dir": ["h2a"],
        "doc": "Liveness check; the app answers `pong` with the same `n`.",
        "fields": {"n": F("int", True, "sequence number")},
    },
    "pong": {
        "dir": ["a2h"],
        "doc": "Answer to `ping`.",
        "fields": {"n": F("int", True, "the ping's number")},
    },
    "error": {
        "dir": ["h2a", "h2c"],
        "doc": "The peer sent something the protocol does not allow (unknown message, bad field, "
               "unknown method...). The offending message was dropped.",
        "fields": {
            "error": F("str", True, "what was wrong"),
            "about": F("str", False, "the offending message type or call id"),
        },
    },
    "bye": {
        "dir": ["a2h", "h2a"],
        "doc": "Orderly end. From the host: the app should save and exit (it is killed after STOP_GRACE_S). "
               "From the app: it is about to exit.",
        "fields": {"reason": F("str", False, "why")},
    },
}

# Blob payloads carry this JSON header (frame type 2 / a binary WebSocket frame).
BLOB_HEADER = {
    "doc": "Binary data (images, audio, files) without base64. App -> host -> clients only.",
    "dir": ["a2h", "h2c"],
    "fields": {
        "stream": F("name", True, "which stream in the app's API, e.g. `preview`, `thumb`"),
        "mime": F("str", True, "media type of the data, e.g. image/jpeg"),
        "meta": F("obj", False, "stream-specific metadata (size, sequence, the call it answers ...)"),
        "client": F("id", False, "towards the host only: deliver to this client alone"),
        "coalesce": F("bool", False, "true for live streams (previews): while this blob waits in a slow client's "
                                    "queue, a newer blob of the same stream replaces it. Default false: every blob "
                                    "is delivered (thumbnails, files)"),
    },
}

CAPABILITIES = {
    "blobs": "the app sends binary streams",
    "attach": "the app may be running before the host (started by hand) and attach to it",
}

# Environment the host sets when it launches an app.
ENVIRONMENT = {
    "NTWB_SOCKET": "path of the host's Unix socket to connect to",
    "NTWB_TOKEN": "one-time token to send in `hello`",
    "NTWB_APP_ID": "the manifest id the host launched",
    "NTWB_VERSION": "the host's protocol version",
    "NTWB_HOST": "name of the host program (arstro-remote)",
    "NTWB_DATA_DIR": "a writable directory for the app's own data under the host's state dir",
}

# The manifest an app installs so the host knows it (NTWB-02).
MANIFEST = {
    "file": "<data dir>/ntwb/apps/<id>/ntwb.json - searched in $XDG_DATA_HOME (default ~/.local/share), "
            "then each of $XDG_DATA_DIRS (default /usr/local/share:/usr/share); the first wins",
    "fields": {
        "ntwb": F("version", True, "protocol version the app speaks"),
        "id": F("name", True, "unique app id; also the directory name and the URL path /apps/<id>/"),
        "name": F("str", True, "display name"),
        "version": F("str", True, "the app's version"),
        "description": F("str", False, "one sentence for the app list"),
        "icon": F("str", False, "path of an SVG/PNG icon, relative to the manifest directory"),
        "exec": F("list[str]", True, "argv to launch the adapter; first element absolute or relative to the manifest dir"),
        "cwd": F("str", False, "working directory (default: the manifest directory)"),
        "env": F("obj", False, "extra environment variables (string values)"),
        "web": F("str", True, "directory with the web UI (index.html), relative to the manifest directory"),
        "api": F("str", False, "path of the app's API description (see APP_API), relative to the manifest directory"),
        "capabilities": F("list[str]", False, "see CAPABILITIES"),
        "single": F("bool", False, "one process for all clients (default true; false is reserved)"),
    },
}

# An app's own API description (manifest `api`). When present, the host refuses calls to
# methods it does not list (NTWB-07).
APP_API = {
    "fields": {
        "ntwb": F("version", True, "protocol version"),
        "app": F("name", True, "app id"),
        "methods": F("obj", True, "method name -> {doc, params: {name: {type, required, doc}}, result: doc}"),
        "events": F("obj", False, "event name -> {doc, data: doc}"),
        "state": F("obj", False, "state key -> {doc, data: doc}"),
        "streams": F("obj", False, "blob stream -> {doc, mime, meta: doc}"),
    },
}

# Host-side timings a peer can rely on.
TIMINGS = {
    "HELLO_TIMEOUT_S": 20,     # a launched app must send hello within this
    "STOP_GRACE_S": 5,         # after `bye`, before SIGTERM (and again before SIGKILL)
    "PING_INTERVAL_S": 15,     # host pings an idle app connection
    "PONG_TIMEOUT_S": 30,      # no pong within this: the connection is dropped
}

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class SpecError(ValueError):
    """A message that does not conform; str() is the sentence sent back in `error`."""


def _check(value, type_, where):
    if type_ == "any":
        return
    if type_.startswith("enum:"):
        options = type_[5:].split("|")
        if value not in options:
            raise SpecError("%s must be one of %s" % (where, ", ".join(options)))
        return
    ok = {
        "str": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "bool": lambda v: isinstance(v, bool),
        "obj": lambda v: isinstance(v, dict),
        "list[str]": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
        "id": lambda v: isinstance(v, str) and bool(_ID.match(v)),
        "name": lambda v: isinstance(v, str) and bool(_NAME.match(v)),
        "version": lambda v: isinstance(v, str) and bool(_VERSION.match(v)),
    }[type_](value)
    if not ok:
        raise SpecError("%s must be %s" % (where, TYPES[type_.split(":")[0]]))


def check_fields(obj, fields, what):
    if not isinstance(obj, dict):
        raise SpecError("%s must be a JSON object" % what)
    for k in obj:
        if k not in fields:
            raise SpecError("%s has no field %r" % (what, k))
    for k, f in fields.items():
        if k not in obj:
            if f["required"]:
                raise SpecError("%s needs field %r" % (what, k))
            continue
        _check(obj[k], f["type"], "%s.%s" % (what, k))


def validate(msg, direction):
    """Check one JSON message travelling in `direction` (a2h, h2a, c2h, h2c). Returns its
    type name or raises SpecError."""
    if not isinstance(msg, dict):
        raise SpecError("a message must be a JSON object")
    t = msg.get("t")
    spec = MESSAGES.get(t) if isinstance(t, str) else None
    if spec is None:
        raise SpecError("unknown message type %r" % (t,))
    if direction not in spec["dir"]:
        raise SpecError("message %r is not allowed %s" % (t, DIRECTIONS[direction]))
    body = {k: v for k, v in msg.items() if k != "t"}
    check_fields(body, spec["fields"], t)
    if t == "hello" and int(_VERSION.match(msg["ntwb"]).group(1)) != MAJOR:
        raise SpecError("this host speaks NTWB %s; version %s is not compatible" % (VERSION, msg["ntwb"]))
    if t == "result" and not msg["ok"] and "error" not in msg:
        raise SpecError("result with ok=false needs field 'error'")
    if t == "call" and direction == "c2h" and "client" in msg:
        raise SpecError("call.client is set by the host, not by a client")
    if t == "notify" and direction == "c2h" and "client" in msg:
        raise SpecError("notify.client is set by the host, not by a client")
    return t


def validate_blob_header(hdr, direction):
    if direction not in BLOB_HEADER["dir"]:
        raise SpecError("blobs are not allowed %s" % DIRECTIONS[direction])
    check_fields(hdr, BLOB_HEADER["fields"], "blob")


def validate_manifest(m):
    check_fields(m, MANIFEST["fields"], "manifest")
    if int(_VERSION.match(m["ntwb"]).group(1)) != MAJOR:
        raise SpecError("manifest speaks NTWB %s, this host %s" % (m["ntwb"], VERSION))
    if not m["exec"]:
        raise SpecError("manifest.exec must not be empty")
    for k, v in (m.get("env") or {}).items():
        if not isinstance(v, str):
            raise SpecError("manifest.env.%s must be a string" % k)
    for c in m.get("capabilities") or []:
        if c not in CAPABILITIES:
            raise SpecError("unknown capability %r" % c)


def validate_app_api(a):
    check_fields(a, APP_API["fields"], "api")
    for name in list(a["methods"]) + list(a.get("events") or {}) + list(a.get("state") or {}) + \
            list(a.get("streams") or {}):
        _check(name, "name", "api name %r" % name)


# ------------------------------------------------------------------------------ rendering
def catalogue():
    """The whole protocol as one JSON-able object (docs/ntwb/api.json)."""
    return {
        "protocol": "ntwb",
        "title": "NTWB - native-to-web bridge",
        "version": VERSION,
        "frames": FRAMES,
        "max_frame": MAX_FRAME,
        "max_blob_header": MAX_BLOB_HEADER,
        "roles": ROLES,
        "directions": DIRECTIONS,
        "types": TYPES,
        "messages": MESSAGES,
        "blob_header": BLOB_HEADER,
        "capabilities": CAPABILITIES,
        "environment": ENVIRONMENT,
        "manifest": MANIFEST,
        "app_api": APP_API,
        "timings": TIMINGS,
    }


def render_json():
    return json.dumps(catalogue(), indent=2, sort_keys=True) + "\n"


def _fields_md(fields):
    out = ["| field | type | required | meaning |", "|---|---|---|---|"]
    for k, f in fields.items():
        out.append("| `%s` | `%s` | %s | %s |" % (k, f["type"].replace("|", "\\|"), "yes" if f["required"] else "no",
                                                   f["doc"].replace("|", "\\|")))
    return out


def render_markdown():
    c = catalogue()
    L = ["# NTWB %s - API reference" % VERSION, "",
         "**Generated** from `server/arstro_remote/ntwb/spec.py` - do not edit; run "
         "`python3 -m arstro_remote.ntwb.spec --write docs/ntwb` (a test fails when this file drifts).",
         "The narrative (flow, rationale, examples) is in [NTWB.md](NTWB.md).", "",
         "## Framing (app <-> host, Unix socket)", "",
         "`[type u8][length u32 big-endian][payload]`, payload at most %d bytes; blob header at most %d bytes." % (
             MAX_FRAME, MAX_BLOB_HEADER), "",
         "| frame | type | payload |", "|---|---|---|"]
    for k, v in FRAMES.items():
        L.append("| %s | %d | %s |" % (k, v["type"], v["payload"]))
    L += ["", "Browser <-> host: WebSocket `/ws/app/<id>` - JSON messages as text frames, blobs as binary frames "
          "carrying the blob payload.", "", "## Roles", ""]
    for k, v in ROLES.items():
        L.append("- **%s** - %s" % (k, v))
    L += ["", "## Types", ""]
    for k, v in TYPES.items():
        L.append("- `%s` - %s" % (k, v))
    L += ["", "## Messages", "",
          "Every message is a JSON object whose `t` is its type. A field not listed is an error, "
          "so is a missing required one; the receiver drops the message and answers `error`.", "",
          "| message | directions |", "|---|---|"]
    for name, m in MESSAGES.items():
        L.append("| [`%s`](#msg-%s) | %s |" % (name, name.replace(".", "-"), ", ".join(DIRECTIONS[d] for d in m["dir"])))
    for name, m in MESSAGES.items():
        L += ["", '<a id="msg-%s"></a>' % name.replace(".", "-"), "### `%s`" % name, "",
              "%s  " % m["doc"], "Directions: %s" % ", ".join(DIRECTIONS[d] for d in m["dir"]), ""]
        L += _fields_md(m["fields"])
    L += ["", "## Blob header", "", BLOB_HEADER["doc"], "",
          "Directions: %s" % ", ".join(DIRECTIONS[d] for d in BLOB_HEADER["dir"]), ""]
    L += _fields_md(BLOB_HEADER["fields"])
    L += ["", "## Capabilities", ""]
    for k, v in CAPABILITIES.items():
        L.append("- `%s` - %s" % (k, v))
    L += ["", "## Environment of a launched app", "", "| variable | meaning |", "|---|---|"]
    for k, v in ENVIRONMENT.items():
        L.append("| `%s` | %s |" % (k, v))
    L += ["", "## Manifest (`ntwb.json`)", "", MANIFEST["file"], ""]
    L += _fields_md(MANIFEST["fields"])
    L += ["", "## App API description (manifest `api`)", "",
          "When an app ships one, the host refuses calls and notifies to methods it does not list.", ""]
    L += _fields_md(APP_API["fields"])
    L += ["", "## Timings", "", "| name | value |", "|---|---|"]
    for k, v in TIMINGS.items():
        L.append("| `%s` | %s |" % (k, v))
    return "\n".join(L) + "\n"


def main(argv=None):
    import argparse
    import os
    ap = argparse.ArgumentParser(prog="python3 -m arstro_remote.ntwb.spec")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", action="store_true")
    g.add_argument("--markdown", action="store_true")
    g.add_argument("--write", metavar="DIR", help="write api.json and API.md into DIR")
    a = ap.parse_args(argv)
    if a.json:
        sys.stdout.write(render_json())
    elif a.markdown:
        sys.stdout.write(render_markdown())
    else:
        os.makedirs(a.write, exist_ok=True)
        for name, text in (("api.json", render_json()), ("API.md", render_markdown())):
            with open(os.path.join(a.write, name), "w") as f:
                f.write(text)
            print("wrote %s" % os.path.join(a.write, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
