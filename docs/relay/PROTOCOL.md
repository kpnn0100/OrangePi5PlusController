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
| `GET B/client/<file>` | - | the standalone Python client for agents: `arstro_relay_client.py`, `e2e.py`, `ws.py`, `pipe.py` |

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
base-uri 'none'; object-src 'none'`. Client address: the socket peer, or the last
`X-Forwarded-For` entry (the one the proxy added) when `RELAY_TRUST_PROXY=1` (set it only behind your own reverse proxy).

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
  same-origin URLs with a class of the same API (`readyState`, `binaryType`, `bufferedAmount`,
  `protocol`, `send`, `close`, `on*` and `addEventListener`) whose sockets are `ws` streams of
  the **service worker's one channel per device**: each socket posts `{t: "ws", sock, id, port,
  path, protocols}` with a `MessagePort` to the controlling worker, which opens the stream and
  relays over the port (worker → page: `open`, `msg`, `sent`, `closesent`, `close`, `fail`; page
  → worker: `send`, `ack` every 64 KiB, `close`, `drop`; the worker pulls the next message only
  while < 512 KiB posted are unacknowledged). Every 10 s (and on traffic) the page posts
  `{t: "alive", socks}`, which keeps the worker running; sockets the worker does not know fail
  with 1006 so the app reconnects; sockets of pages gone or silent for 45 s are closed. It also
  rewrites same-origin absolute links, iframes, forms and `window.open` (and
  `history.pushState`, and in Chrome `location.href = "/x"`) outside `B/` into
  `B/d/<id>/<port>/...`. Closing a shimmed WebSocket fires `close` right after the `close`
  frame is sent. Device pages cannot register their own service worker (it would take `B/d/`
  away).
* **Downloads:** a browser download (`<a download>` clicked, or such an anchor clicked from
  script without being in the page) never reaches the service worker - the browser would save
  the device manager's boot page. The shim takes the click over: it `fetch`es the URL (through
  the worker and the channel), hands the bytes to the browser's blob store every 16 MiB (large
  blobs live on disk), shows the progress on the page, then saves the blob as a normal download
  under the `Content-Disposition` name (else the `download` attribute, else the URL's last
  part). A failed download is shown on the page. Very large files are better fetched with the
  client's `forward`.
* **Transport:** `ArstroE2E.transport` starts from sessionStorage `arstro-relay-transport`; a
  device manager that needed pipes is remembered in sessionStorage `arstro-relay-pipes <B>`
  (only after a pipe opened); in `auto` a WebSocket that has not opened after 10 s counts as
  failed. Tests register the worker as `sw.js?transport=ws|http`; it also answers
  `{t: "status"}` (its channels) for diagnostics.
* **Forget** deletes the device's stored key and its cookie jar.
* **Limits of the browser viewer:** every device page shares the device manager's origin, so a
  page of one device could use the keys this browser holds for other devices (it cannot read
  them: they are non-extractable). Apps that need their own service worker or real cookies
  readable by JavaScript do not work through the browser viewer - use the client (port forward).

## 6. HTTP transport (when WebSockets cannot pass)

Some reverse proxies do not pass WebSockets (an nginx without the `Upgrade` lines answers the
relay's WebSocket routes with the relay's own `426 Upgrade Required`). Every WebSocket of
sections 2 and 3 - `B/api/agent`, `B/api/agent/data`, `B/api/connect/<id>` - can instead be a
**pipe**: the same messages (text and binary, in order, with a close code), carried by plain
HTTP requests. Nothing above the transport changes: the relay runs the same handlers, the
end-to-end channel of section 4 is the same, the relay still sees only ciphertext.

**Choosing:** a client tries the WebSocket first; if it fails before it opens (any handshake
answer but 101, or the connection breaks), it opens a pipe for the same route instead and keeps
using pipes for that device manager while it runs. `ARSTRO_RELAY_TRANSPORT=ws|http|auto`
(Python) and `ArstroE2E.transport = "ws"|"http"|"auto"` (browser) force one (tests).

**Open** - `POST B/api/pipe/open`, JSON body `{"path": "/api/agent" | "/api/agent/data" |
"/api/connect/<id>"}`, with the same auth, Origin and rate-limit checks the WebSocket route
makes before its upgrade (same 401 / 403 / 404 / 429 answers). → `200 {"pipe": "<id>", "max_up":
262144, "poll": 25}`. The id (128 random bits, base64url) is the pipe's only credential: never
logged in full. The relay then runs that route's handler with the pipe; its first messages
(e.g. `relay` / `welcome` / `linked`) wait in the pipe for the first down request.

**Down** - `GET B/api/pipe/<id>/down?ack=<n>` - `n` = how many down messages the client has
received so far. The relay forgets messages with a lower sequence number, then answers at once
with every queued message from `n` on (at most 1 MiB, at least one whole message), or waits up
to `PIPE_POLL` (25 s) for one, else `204`. Body: records
`kind u8 | seq u32 BE | length u32 BE | payload`, kind 1 = text (UTF-8), 2 = binary, 8 = close
(payload: code u16 BE + UTF-8 reason). Headers `Content-Type: application/octet-stream`,
`Cache-Control: no-store`, `X-Accel-Buffering: no`, `Content-Length`. `410 {"error"}` when the
pipe no longer exists (closed and its close delivered, or dropped). A newer down request ends
an older one still waiting (`204`). The client re-polls right after every answer, so a down
request is (nearly) always waiting - that is how the relay knows the client is alive.

**Up** - `POST B/api/pipe/<id>/up?seq=<n>`, `Content-Type: application/octet-stream`, body =
records `kind u8 | length u32 BE | payload` (kinds 1, 2, 8), at most `PIPE_MAX_UP` (256 KiB;
a single message must fit). `n` = the index of the first record among all up messages of the
pipe (0-based). The relay applies records it has not applied yet, in order (a retried request
is harmless) → `200 {"next": <count applied>}`; a gap (`n` > count - only if the relay lost records it
had confirmed; a client keeps its last confirmed batch for this) → `409 {"next": count}`
(resend from there); `410` when the pipe is gone; `413` / `400` for a body too large / broken.
The client sends one up request at a time, batching what is queued. The relay holds the answer
while more than `PIPE_BUFFER` (2 MiB) of up messages wait to be forwarded (backpressure).

**Close** - a close record (kind 8) up, or down from the relay; after it the pipe accepts
nothing more. A pipe with no request (down or up) for `PIPE_IDLE` (60 s) is dropped like a
broken connection (code 1006). The relay stops reading the other side while more than
`PIPE_BUFFER` of down messages wait unacknowledged.

**Details clients follow:** a single up message must fit one up request (≤ `PIPE_MAX_UP` minus
the 5-byte record head - the channel's frames are ≤ `CHUNK` + 64, so pipes are not a general
1 MiB-message WebSocket upward); `poll` in the open answer may be a decimal number. Closing works
like a WebSocket close: the side that closes sends a close record, the other answers with one;
the client acknowledges the relay's close record with its next down request (`ack` past it),
after which down and up answer `410`. Up requests after the relay's close answer `200` and are
dropped until the close is acknowledged. A pipe whose close was handed out is removed once
acknowledged, 10 s later, or after `PIPE_IDLE`. A pipe whose client vanished without closing
(a closed tab, a stopped worker) holds its channel slot until `PIPE_IDLE`.

**Proxies:** plain `proxy_pass` is enough - no `Upgrade` headers, default buffering and
`proxy_read_timeout 60s` work (answers end within 25 s), and up requests stay under nginx's
default `client_max_body_size 1m`. Browsers use at most one pipe per device (the shim's
WebSockets go through the service worker's channel), which keeps HTTP/1.1's six connections
per host enough. WebSockets stay faster (no request per batch); use them when the proxy allows.
The proxy must pass the browser's `Host` (nginx: `proxy_set_header Host $host;`), or set
`X-Forwarded-Host` with `RELAY_TRUST_PROXY=1`: otherwise the Origin check refuses logins and
viewers with 403.

## 7. Constants

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
| `PIPE_MAX_UP` | 256 KiB | largest up request of the HTTP transport |
| `PIPE_POLL` | 25 s | longest wait of a down request |
| `PIPE_IDLE` | 60 s | a pipe without requests is dropped |
| `PIPE_BUFFER` | 2 MiB | queued bytes per direction before the sender waits |
