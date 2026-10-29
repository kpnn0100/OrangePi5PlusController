# Device manager protocol (relay protocol v1)

Normative description of how a **device** (a machine running Arstro Remote with the `relay`
module), the **device manager** (the relay server, `relay/`) and a **viewer** (a browser,
the CLI, an agent's client) talk. Requirements: RLY-* in `docs/requirements.md`. Design and
security: [RELAY.md](RELAY.md). For agents: [AGENT_API.md](AGENT_API.md).

Single definitions - change them first, then this file, then the code:

| What | Defined in | Checked by |
|---|---|---|
| crypto, framing, constants, message names | `server/arstro_remote/relay/e2e.py` (`RELAY_MESSAGES`, `CHANNEL_MESSAGES`) | `server/tests/test_relay.py` (doc sync: every message is in this file), `vectors.json` |
| test vectors | `docs/relay/vectors.json` (generated: `python3 -m arstro_remote.relay.e2e vectors`) | Python tests and `relay/tests/e2e_vectors.mjs` (browser `e2e.js`) |

```
 viewer ──WS B/api/connect/<id>──► device manager ◄──WS B/api/agent (control, long-lived)──── device
                                  (forwards bytes) ◄──WS B/api/agent/data (one per viewer)──┘
        ◄════════════ end-to-end encrypted channel (access key) ═══════════════════════════►
                                                                       device ──► 127.0.0.1:<port>
```

`B` is the base path of the device manager, default `/arstro/internal/devices` (env
`RELAY_BASE`). The relay is reached at `https://<server>/<B>`; the device is configured with
that same URL (`https:` → `wss:`, `http:` → `ws:` for the sockets).

## 1. The device manager's HTTP API

| Route | Auth | What |
|---|---|---|
| `GET B` | - | redirect to `B/` |
| `GET B/` | - | the device list page (`static/index.html`) |
| `GET B/static/<file>` | - | page assets (`app.js`, `e2e.js`, `style.css`, `boot.js`, ...) |
| `GET B/sw.js` | - | the viewer's service worker, header `Service-Worker-Allowed: B/`, `Cache-Control: no-cache` |
| `GET B/shim.js` | - | the script the service worker puts into every proxied HTML page (WebSocket + link shim) |
| `GET B/d/<id>/<port>/...` | - | only reached when the service worker is not in control yet: the **boot page** (`static/boot.html`) - installs the service worker, asks for the key if the browser has none, reloads |
| `GET B/api/info` | - | `{name, version, base, auth: "password"\|"open", authorized}` |
| `POST B/api/login` | - | `{password}` → cookie `arstro_devices` (`Path=B/; HttpOnly; SameSite=Strict; Max-Age=2592000`, `Secure` on https); 401 if wrong |
| `POST B/api/logout` | - | clears the cookie |
| `GET B/api/devices` | viewer | `{devices: [{id, name, hostname, platform, version, slot, web, online, since, last_seen, viewers}]}` (sorted: online first, then name) |
| `GET B/api/connect/<id>` | viewer | WebSocket: a viewer channel (section 3) |
| `GET B/api/agent` | join token | WebSocket: a device's control connection (section 2) |
| `GET B/api/agent/data` | device secret | WebSocket: a device's data connection (section 2) |
| `GET B/api/health` | - | `{"ok": true}` (container health check) |
| `GET B/client/<file>` | - | the standalone Python client for agents: `arstro_relay_client.py`, `e2e.py`, `ws.py` |

**Viewer auth:** only when `RELAY_PASSWORD` is set - the `arstro_devices` cookie (an HMAC of
the password with a key in the data volume) or `Authorization: Bearer <password>`. Without
`RELAY_PASSWORD` the list is open (the page says so). Either way a device is only usable with
its access key.

**Pages:** every `.html` the relay serves has `{{BASE}}` replaced by the base path, so pages
use absolute URLs (the boot page is served at any depth under `B/d/...`).

**Other rules:** viewer WebSockets and logins with an `Origin` of another site get 403. New
viewer and control WebSockets, logins and wrong Bearer passwords count against a rate limit
per address (default 30 per 10 s, then 429); data connections and plain page/API requests do
not. A message is checked whole (fragments are joined; > `MAX_MESSAGE` closes both sides with
1009, protocol errors with 1002 / 1007); close codes are passed to the other side. Rotating
the join token keeps online devices connected; `forget` drops a device, but it registers again
with its own secret unless the join token is rotated too. `since` and `last_seen` are epoch
seconds.

**Headers on every relay response:** `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`; pages: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src
'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none';
base-uri 'none'; object-src 'none'`. Client address: the socket peer, or the first
`X-Forwarded-For` entry when `RELAY_TRUST_PROXY=1` (set it only behind your own reverse proxy).

## 2. Device ↔ device manager

All JSON messages here are **text** WebSocket messages with a `"t"` field. Names:
`RELAY_MESSAGES` in `e2e.py`.

**Control connection** `B/api/agent` - one per device (per slot), kept open:

| Dir | Message | |
|---|---|---|
| D→R | `{"t":"hello","v":1,"id","name","secret","token","version","hostname","platform","slot","web"}` | first message, within 10 s. `id`: `^[a-z0-9][a-z0-9-]{2,39}$`; `name`: 1-64 printable characters; `secret`: ≥ 32 characters; `token`: the relay's join token; `web`: the device's Arstro web port (a hint for the list) |
| R→D | `{"t":"welcome","v":1,"heartbeat":20,"relay":"<version>"}` | accepted |
| R→D | `{"t":"error","error":"..."}` then close | wrong join token / id taken by another device / bad hello / too many devices |
| R→D | `{"t":"open","sid":"<id>","addr":"203.0.113.7"}` | a viewer is waiting: open a data connection for `sid` within 10 s |
| D→R | `{"t":"refuse","sid","error"}` | the device will not take that viewer (the relay sends the viewer the error) |
| D→R | `{"t":"ping"}` every `heartbeat` s → R→D `{"t":"pong"}` | 3 heartbeats of silence: both sides drop the connection; the device reconnects (back-off 1 s → 60 s, ±20 % jitter) |
| R→D | `{"t":"bye","reason"}` | the relay closes it (shutting down, device forgotten, replaced by a newer connection with the same id) |

**Device identity at the relay:** the first `hello` with a new `id` registers it: the relay
stores `sha256(secret)` with it. Later `hello`s and data connections with that `id` must carry
the same `secret` ("this device id belongs to another device"). A second control connection
with the right secret replaces the first (`bye`). `RELAY_JOIN_TOKEN` (or the token generated in
the data volume on the first start, printed in the log) is required for every `hello`.

**Data connection** `B/api/agent/data` - one per viewer channel:

| Dir | Message | |
|---|---|---|
| D→R | `{"t":"data","v":1,"id","secret","sid"}` | first message; `sid` must have been sent to this device less than 10 s ago and not used yet |
| R→D | `{"t":"linked"}` | from now on every message (text or binary) is forwarded unchanged to the viewer and back |
| R→D | `{"t":"error","error"}` then close | unknown / expired sid, wrong secret |

## 3. Viewer ↔ device manager

`B/api/connect/<id>` (viewer auth). The relay checks the device is online and has fewer than
32 channels, sends `open` to the device and waits up to 10 s for its data connection:

| Dir | Message | |
|---|---|---|
| R→V | `{"t":"relay","ok":true}` | joined: everything after this is forwarded unchanged to the device and back |
| R→V | `{"t":"relay","ok":false,"error":"..."}` then close | device offline / unknown / busy / did not answer / refused (`refuse` error) |

When either side of a joined pair closes, the relay closes the other. The relay never reads
or changes forwarded messages; it only enforces the message size limit (`MAX_MESSAGE`, 1 MiB)
and counts bytes.

## 4. The end-to-end channel (viewer ↔ device)

### 4.1 Handshake

Plain **text** messages, forwarded by the relay (`CHANNEL_MESSAGES` in `e2e.py`):

1. V→D `{"t":"hello","v":1,"cn":"<32 random bytes, base64url>"}`
2. D→V `{"t":"hello","v":1,"sn":"<32 random bytes>","proof":"<base64url>"}` or `{"t":"error","error"}` (too many viewers, too many wrong keys - wait a minute)
3. The viewer derives the keys and **checks the device's proof first**. Wrong: it stops and
   reports "wrong access key for this device" - it has sent nothing derived from the key.
4. V→D `{"t":"proof","proof":"<base64url>"}`
5. D→V the first **encrypted** frame: stream 0 `{"t":"ready","v":1,"id","name","hostname","version","slot","ports"}`
   (`ports`: the allow-list, or null = any port), or the text `{"t":"error","error":"wrong key"}` and close.

Keys: `HKDF-SHA256(ikm = access key (32 bytes), salt = cn ‖ sn, info = "arstro-relay/1")` → 96
bytes = **viewer→device key** ‖ **device→viewer key** ‖ **proof key**.
Proofs: `HMAC-SHA256(proof key, "arstro-relay/1 device")` and `... "arstro-relay/1 viewer"`.
The access key text is `ak1_` + base64url(32 bytes) (43 characters). The device makes it on
first use; whitespace in pasted keys is ignored.

### 4.2 Frames

After the handshake every message is **binary** and encrypted; a text message is a protocol
error (the channel is closed).

```
WebSocket binary message = AES-256-GCM(direction key, nonce, plaintext)   (ciphertext ‖ 16-byte tag)
nonce     = 00 00 00 00 ‖ counter u64 BE     (per direction, starts at 0, +1 per frame, never sent)
plaintext = kind u8 ‖ stream u32 BE ‖ flags u8 ‖ payload
kind      1 = JSON object with "t"            2 = DATA (bytes of a stream)
flags     DATA of a ws stream: bit0 FIN = last fragment of a message, bit1 TEXT = text message
```

A frame that does not decrypt ends the channel. DATA payloads are at most `CHUNK` (64 KiB).

### 4.3 Streams

Stream 0 is the channel itself (`ready`, `ping` / `pong`). Every other stream is opened by
the viewer with a JSON frame on a new id (1, 2, 3, ... increasing, never reused in a channel):

| Opened with | Device answers | Then |
|---|---|---|
| `{"t":"http","method","port","path","headers":[[k,v]...],"body":bool}` | `{"t":"res","status","reason","headers":[[k,v]...]}` | V→D body as DATA + `end` (if `body`); D→V response body as DATA + `end` |
| `{"t":"ws","port","path","headers":[[k,v]...],"protocols":[...]}` | `{"t":"open","protocol":""}` | messages both ways as DATA (`TEXT`, `FIN` flags; a message may span frames); `close {code, reason}` from either side ends it |
| `{"t":"tcp","port"}` | `{"t":"open"}` | raw bytes both ways as DATA; `end` = half-close; `close` = abort; done when both sides sent `end` |
| `{"t":"ports"}` | `{"t":"ports","ports":[{"port","addr","proc"}...]}` | ends the stream |

Any stream: the device answers `{"t":"error","error":"..."}` instead (ends the stream) - e.g.
`port 22 is not allowed on this device`, `nothing listens on port 8081`, `too many streams`.
The viewer may send `{"t":"cancel"}` to give a stream up.

**Flow control:** for each stream and direction, the sender keeps at most `WINDOW` (512 KiB)
of DATA payload unacknowledged; it may send one more frame while below the window. The
receiver sends `{"t":"ack","n":<bytes>}` on that stream as its consumer takes data (at least
every `WINDOW / 4` bytes). JSON frames are not counted.

### 4.4 What the device does with a stream

* Only `127.0.0.1:<port>` (then `[::1]:<port>`) - never another host. `port` 1-65535, and in
  the slot's allow-list when one is set.
* **http:** hop-by-hop headers are dropped (`connection`, `keep-alive`, `proxy-*`, `te`,
  `trailer`, `transfer-encoding`, `upgrade`); `Host` becomes `127.0.0.1:<port>`; an `Origin`
  header becomes `http://127.0.0.1:<port>` (so same-origin checks on the device pass);
  `Accept-Encoding` becomes `identity` (a service worker cannot hand compressed bytes to the
  browser); `X-Forwarded-For: <viewer address from the relay>` is added. Response hop-by-hop
  headers are dropped too.
* **ws:** the same header rules, then a WebSocket client handshake to the local port.
* **tcp:** `close` from the device carries a `reason` when the local side failed.
* **ports:** with an allow-list, only allowed ports are listed; one entry per port (the
  wildcard address when it listens on several).
* **Limits:** 16 channels per device, 128 open streams per channel, 15 s for the handshake,
  5 s to connect locally. A **wrong key** is a wrong proof, or a viewer that leaves right after
  the device's hello (a viewer with a wrong key rejects the device's proof and closes, so the
  device never sees a proof). More than 5 wrong keys in 60 s: new channels are refused for
  60 s. Refusals before the handshake ("too many viewers", "too many wrong keys - wait a
  minute") are sent on the control connection as `refuse {sid, error}`, so the viewer gets
  `{"t":"relay","ok":false,"error":...}`.

## 5. The browser viewer

The device manager's pages implement the viewer side in the browser; nothing about it is
needed by other viewers, but every browser behaviour that matters for apps is listed here.

* **Secure context required** (service workers and WebCrypto): https, or `http://localhost`.
* **Key storage:** IndexedDB `arstro-devices`, store `keys`, record per device id:
  `{id, key (CryptoKey, HKDF, non-extractable), key_id, remember, expires}`. Not remembered:
  expires after 12 h. "Forget" deletes it. The raw key text is never stored.
* **Service worker** `B/sw.js`, scope `B/`. For each fetch:
  1. path under `B/d/<id>/<port>/` → device `<id>`, `port`, path = the rest (with its leading `/`) + query;
  2. else a same-origin URL **outside** `B/` whose requesting page (the fetch's client) is under
     `B/d/<id>/<port>/` → that device and port, path = the URL's path + query (navigations to
     such URLs never reach a worker scoped to `B/`: the shim rewrites them first);
  3. else the network.
  It keeps one channel per device while it runs and sends the request as an `http` stream.
  Request bodies are sent with their exact `Content-Length` (a page cannot see it, and some
  servers refuse chunked uploads). **Responses:** `Location` values pointing at a loopback
  address (`127.0.0.1` / `localhost` / `[::1]`, any port → that port of the same device) or at
  absolute paths are rewritten into `B/d/<id>/<port>/...`; statuses below 200 (e.g. 101)
  become 502 (a worker cannot build them); `Clear-Site-Data` (it would wipe the stored keys)
  and `Service-Worker-Allowed` from the device are dropped; `Set-Cookie` goes into the worker's cookie jar
  (IndexedDB store `cookies`, per device and port) and the jar's cookies go out as `Cookie`
  on the device's requests (a service worker cannot set real cookies); HTML responses get
  `<script src="B/shim.js"></script>` right after `<head>`. No key for the device: a
  navigation goes to the network (the boot page asks for the key), other requests get 401.
* **Shim** (`B/shim.js`, runs first in every proxied page): replaces `window.WebSocket` for
  same-origin URLs with a class that opens its own channel and a `ws` stream (same API:
  `readyState`, `binaryType`, `bufferedAmount`, `protocol`, `send`, `close`, `on*` and
  `addEventListener`), and rewrites same-origin absolute links, iframes, forms and
  `window.open` (and `history.pushState`, and in Chrome `location.href = "/x"`) outside `B/`
  into `B/d/<id>/<port>/...`. It loads `B/static/e2e.js` and `B/static/viewer.js` itself on the
  first WebSocket. Closing a shimmed WebSocket fires `close` right after the `close` frame is
  sent. Device pages cannot register their own service worker (it would take `B/d/` away).
* **Forget** deletes the device's stored key and its cookie jar.
* **Limits of the browser viewer:** every device page shares the device manager's origin, so a
  page of one device could use the keys this browser holds for other devices (it cannot read
  them: they are non-extractable). Apps that need their own service worker or real cookies
  readable by JavaScript do not work through the browser viewer - use the client (port forward).

## 6. Constants

| Name | Value | Where |
|---|---|---|
| `VERSION` | 1 | hello `v` |
| `CHUNK` | 64 KiB | largest DATA payload |
| `WINDOW` | 512 KiB | flow-control window per stream and direction |
| `MAX_MESSAGE` | 1 MiB | largest WebSocket message (relay, device, viewer) |
| heartbeat | 20 s | control connection |
| channels per device | 32 at the relay, 16 at the device | |
| streams per channel | 128 | device |
| pending viewer | 10 s | relay waits for the data connection |
