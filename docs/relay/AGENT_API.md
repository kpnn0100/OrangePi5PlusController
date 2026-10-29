# Agent API: remote-control a computer through the device manager (RLY-26)

For AI agents and scripts that must work on a machine they cannot reach directly. You need:

| What | Where it comes from |
|---|---|
| The device manager URL, e.g. `https://abc.example.com/arstro/internal/devices` | the person who runs it |
| The machine's **device id** | `devices` below, or the device list page |
| The machine's **access key** `ak1_…` | the machine's owner (`arstro-remote relay key` on the machine) |
| The device manager's viewer password | only if it has one (`RELAY_PASSWORD`) |
| The app's own login on the machine, e.g. the Arstro Remote web password | the machine's owner |

**Handle the access key like a password with root-like reach:** it opens every port of the
machine. Never print it, log it, commit it or put it in a URL; pass it with `--key-file` or
the `ARSTRO_RELAY_KEY` environment variable. Do not replace (rotate) it unless the owner asks.

## 1. Get the client

Python 3.8+ with `cryptography` (`apt install python3-cryptography` or `pip install cryptography`).

* From a checkout of this repo: `server/arstro_remote/relay/client.py`, or the CLI on a
  machine that has Arstro Remote: `arstro-remote relay <command> ...` (same commands).
* Standalone: download the three files into one folder and run the first:
  ```
  B=https://abc.example.com/arstro/internal/devices
  mkdir -p arstro-client && cd arstro-client
  for f in arstro_relay_client.py e2e.py ws.py pipe.py; do curl -fsSO "$B/client/$f"; done
  python3 arstro_relay_client.py --help
  ```
  These files come from the device manager's server. If you do not trust its operator, take
  them from the repo instead (see [RELAY.md](RELAY.md), security model).

**Proxies without WebSockets:** nothing to do. The client tries a WebSocket and, if the device
manager's proxy does not pass them, switches to the HTTP transport by itself (slower per message,
same features). `ARSTRO_RELAY_TRANSPORT=http` skips the WebSocket attempt, `ws` forbids the fallback.

Settings: `--relay URL` or `ARSTRO_RELAY_URL`; `--key ak1_…`, `--key-file FILE` or
`ARSTRO_RELAY_KEY`; `--password PW` or `ARSTRO_RELAY_PASSWORD`.

## 2. Commands

| Command | What it does | Output |
|---|---|---|
| `devices [--json]` | lists the machines | id, online/offline, name, Arstro web port hint |
| `check ID [--json]` | opens a channel and checks the key | the machine's `ready` info |
| `ports ID [--json]` | lists the machine's listening TCP ports | port, address, process |
| `http ID PORT [PATH] [-X M] [-H 'K: V'] [-d BODY\|@file] [-i]` | one HTTP request to `127.0.0.1:PORT` on the machine | the body on stdout (`-i`: status and headers first) |
| `forward ID PORT [--listen 127.0.0.1:18080]` | serves a local TCP port that leads to the machine's PORT, until stopped | "forwarding …" on stderr |

Exit codes: 0 ok, 1 HTTP status ≥ 400 (`http`), 2 refused / unreachable / bad input, 3 wrong
access key. Errors are one line on stderr, e.g. `error: device offline`,
`error: wrong access key for this device`, `error: port 22 is not allowed on this device`,
`error: nothing listens on port 8081`.

## 3. Recipes

Start a forward once and then use ordinary tools against `127.0.0.1` - every request and
WebSocket goes through one encrypted channel:

```bash
export ARSTRO_RELAY_URL=https://abc.example.com/arstro/internal/devices
export ARSTRO_RELAY_KEY="$(cat ~/.secrets/labpi.key)"
python3 arstro_relay_client.py devices
python3 arstro_relay_client.py forward labpi-a-7k2m 8080 --listen 127.0.0.1:18080 &   # Arstro Remote
```

**Arstro Remote (the machine's full API, `docs/protocol.md`)** - with its web password `PW`:

```bash
# the CLI, from any machine that has this repo (cd server; python3 -m arstro_remote ...)
arstro-remote --url http://127.0.0.1:18080 --token "$PW" system info
arstro-remote --url http://127.0.0.1:18080 --token "$PW" term run "uname -a && df -h"
arstro-remote --url http://127.0.0.1:18080 --token "$PW" files ls          # then: files get PATH
arstro-remote --url http://127.0.0.1:18080 --token "$PW" screen save desk.h264 --seconds 3
arstro-remote --url http://127.0.0.1:18080 --token "$PW" call stats.get '{}'

# or plain HTTP: any op through the REST shim
curl -s -X POST http://127.0.0.1:18080/api/op/stats.get -H "Authorization: Bearer $PW" -d '{}'
```

**One request without a forward:**

```bash
python3 arstro_relay_client.py http labpi-a-7k2m 8080 /api/ping
python3 arstro_relay_client.py http labpi-a-7k2m 8080 /api/op/system.info -H "Authorization: Bearer $PW" -d '{}'
```

**SSH or any other TCP service** (if the slot's allow-list permits the port):

```bash
python3 arstro_relay_client.py forward labpi-a-7k2m 22 --listen 127.0.0.1:2222 &
ssh -p 2222 user@127.0.0.1
```

**From Python:**

```python
from arstro_relay_client import Relay        # or: from arstro_remote.relay.client import Relay
relay = Relay("https://abc.example.com/arstro/internal/devices")
ch = relay.connect("labpi-a-7k2m", open("labpi.key").read())
print(ch.info)                               # {'t': 'ready', 'name': ..., 'hostname': ..., 'ports': ...}
print(ch.ports())
status, reason, headers, body = ch.http("GET", 8080, "/api/ping")
s = ch.tcp(22)                               # raw stream: s.send_data(b"..."), s.next() -> ("data", flags, bytes)
ch.close()
```

## 4. The device manager's own HTTP API

Base `B` = the device manager URL. Viewer password, when set: `Authorization: Bearer <password>`.

| Request | Answer |
|---|---|
| `GET B/api/info` | `{name, version, base, auth: "password"\|"open", authorized}` |
| `GET B/api/devices` | `{devices: [{id, name, hostname, platform, version, slot, web, online, since, last_seen, viewers}]}` |
| `GET B/api/health` | `{"ok": true}` |
| `GET B/client/<file>` | the client files (`arstro_relay_client.py`, `e2e.py`, `ws.py`, `pipe.py`) |
| `WS B/api/connect/<id>` | a viewer channel (needs the key - use the client, or implement [PROTOCOL.md](PROTOCOL.md) section 4) |

Implementing the channel in another language: follow [PROTOCOL.md](PROTOCOL.md) and check your
code against [vectors.json](vectors.json) (same key and nonces must give the same keys,
proofs and ciphertexts).
