# Protocol (v2)

One protocol for every controller (ARC-02). The app speaks it over Bluetooth RFCOMM,
the CLI over the local unix socket or a WebSocket, the web UI over a WebSocket. The
server code is `server/arstro_remote/protocol.py` + `session.py`; the Dart side is
`app/lib/src/proto/frames.dart`; the browser side is `connection` in
`server/arstro_remote/web/static/js/core.js`.

## Transports

| Controller | Transport | Auth |
|---|---|---|
| App | Bluetooth RFCOMM, SPP UUID `a57e0001-7c2b-4d1e-9f3a-5e7a1b2c3d4e` | trusted (bonded) device |
| App (developer) | TCP `tcp:host:port` to the control socket through an SSH tunnel (`scripts/dev_tunnel.sh`) | the socket owner |
| CLI on the Pi | unix socket `$XDG_RUNTIME_DIR/arstro-remote.sock` (0600; `ARSTRO_SOCKET` overrides) | file permissions |
| CLI remote, web UI | WebSocket `ws://<pi>:8080/ws` | password (see *HTTP*) |

## Frames

Every message is a frame: `[type u8][length u32 BE][payload]`, payload ≤ 8 MiB.

| Type | Name | Payload | Direction |
|---|---|---|---|
| `0x01` | JSON | UTF-8 JSON object | both |
| `0x02` | TERM | `[term u8][bytes]` keyboard input for a shell | to server |
| `0x03` | TERM_OUT | `[term u8][offset u64 BE][bytes]` shell output; `offset` = stream position of the first byte | to controller |
| `0x04` | VIDEO | `[flags u8][pts_us u64][Annex-B AU]` (recorder worker IPC only, never sent to controllers) | internal |

Over WebSocket each binary message carries frames (a message may hold several frames or a
part of one); a *text* message is accepted as one JSON op (handy for tools).
Controllers must skip frame types they do not know.

## Requests, replies, events

```json
→ {"op": "recorder.start", "id": 7}
← {"id": 7, "ok": true, "data": {...}}          or  {"id": 7, "ok": false, "error": "no HDMI signal: ..."}
→ {"op": "in.move", "dx": 3, "dy": -1}          (no id: a notification, no reply)
← {"ev": "state", "topic": "recorder", "data": {...}}
```

The first op of a session is `hello` `{app, version, device?, client_id?}`; `app` is
`arstro-android`, `arstro-web` or `arstro-cli` and sets the controller type. The reply holds
`name, version, proto (=2), hostname, user, features, session, controller, input,
terminals, term_keep_sec` and **`state`**: a snapshot of every state topic.

### State topics (ARC-03)

Pushed to every session whenever they change (whoever changed them), newest value wins.

| Topic | Data |
|---|---|
| `recorder` | status: `available, caps, signal, capture, recording, preview, disk, mode, mode_text, last, simulate` |
| `recorder.settings` | the recording settings (see `recorder.settings.set`) |
| `jobs` | list of conversion jobs `{id, title, codec, state, source, output, progress, fps, eta, live, error, options}` |
| `gallery` | summary `{version, takes, files, size, folder}`; fetch `gallery.list` when `version` changes |
| `wifi` | `{device, enabled, connected, ssid, signal, ip, state, connection}` |
| `terminals` | shared shells `[{term, cols, rows, viewers, attached, opened_by, ephemeral, age, detached_for}]` |
| `pairing` | Bluetooth `{ready, alias, address, pairing_open, pairing_remaining, paired_devices}` |
| `controllers` | connected controllers `[{session, controller, kind, peer, since}]` |
| `web` | `{enabled, port, urls, auth: "password" \| "open"}` (never the password) |

Other events: `stats` `{data}` (after `stats.subscribe`), `term.exit` `{term, code, reason?}`.

## Ops

| Op | Arguments | Result |
|---|---|---|
| `hello` | `app, version, device?, client_id?` | see above |
| `ping` | `t?` | `{t, echo}` |
| `state.get` | `topics?` | `{topic: data}` |
| `stats.get` | | system snapshot (STAT-01) |
| `stats.subscribe` / `stats.unsubscribe` | `interval_ms` (500..60000) | `stats` events |
| `wifi.status` / `wifi.scan` / `wifi.saved` | `rescan?` (scan) | status / `{networks}` |
| `wifi.connect` | `ssid, password?, hidden?` | `{message}` |
| `wifi.disconnect` | | |
| `wifi.forget` | `uuid` or `name` | |
| `wifi.radio` | `enabled` | |
| `term.list` | | `{terminals}` |
| `term.open` | `cols, rows, ephemeral?` | `{term}` (the opener is a viewer) |
| `term.attach` | `term, since?, cols?, rows?` | `{term, start, replayed, gap, cols, rows}` - sent **before** the replayed TERM_OUT bytes; `gap` = the ring no longer holds `since`, reset the screen |
| `term.detach` / `term.close` | `term` | |
| `term.resize` | `term, cols, rows` | |
| `term.input` | `term, data` (string) | JSON alternative to TERM frames |
| `in.move` / `in.move_to` | `dx, dy` / `x, y` | relative (fractions accumulate) / absolute |
| `in.btn` | `b` (left, middle, right, back, forward), `a` (down, up, click, double) | |
| `in.scroll` | `dx, dy` (dy > 0 = down; fractions accumulate) | |
| `in.key` | `k` (X keysym or alias: enter, esc, tab, up, pageup, volumeup, ...), `mods` (ctrl, alt, shift, super, altgr), `a` (press, down, up) | |
| `in.text` | `s` | types Unicode text |
| `in.pointer` | | `{x, y}` |
| `recorder.status` | | the `recorder` topic |
| `recorder.start` / `recorder.stop` | | `{file, ...}` |
| `recorder.settings.get` | | settings |
| `recorder.settings.set` | `settings` (a partial object, e.g. `{"h265": {"bitrate": 60}}`) | validated, saved, published |
| `recorder.edid` | `value` (4k60, 4k30, 1080p, keep) | |
| `recorder.preview.quality` | `quality` (low, medium, high) | |
| `recorder.source` | `simulate` (`"1920x1080@30"`) or null for HDMI RX | status |
| `recorder.test_signal` | `present` (test source only) | status |
| `gallery.list` | `kind?` (RAW, H.265, H.264, FFV1, VIDEO) | `{folder, version, takes}` |
| `gallery.get` | `take` | one take `{id, title, created, size, duration, kinds, recording, items}` |
| `gallery.targets` | | `{targets: [{id, title, available, description, options}]}` |
| `gallery.convert` | `file, target, quality?, scale?, preset?, bitrate?, rc?, chroma?` | the new job |
| `gallery.delete` | `file` | refused while it is recorded or converted |
| `gallery.delete_take` | `take` | |
| `jobs.list` / `jobs.clear` / `jobs.cancel` | `id` (cancel) | `{jobs}` / job |
| `admin.status` | | `{version, bluetooth, input, web, recorder, sessions}` |
| `admin.pair` | `seconds` (0 closes) | status |
| `admin.unpair` | `address` | |
| `web.info` | | `{enabled, port, urls, auth, app, token}` - `token` is the password |
| `web.set_password` | `password` (8..256 chars) | info; signs out the other web/remote sessions |
| `web.rotate_token` | | a new random password |
| `web.set_auth` | `required` (bool) | info; `false` = open mode |

Take ids are `REC_YYYYMMDD_HHMMSS`; its files are `<take>.arh` (RAW), `<take>_H265.mp4|mov|mkv`,
`<take>_H264[_720p|_1080p].mp4`, `<take>_FFV1.mkv`.

## HTTP (web server, default port 8080)

| Route | |
|---|---|
| `GET /` , `/assets/*` | the web UI (no password needed to load) |
| `GET /api/ping` | `{name, version, hostname, authorized, auth, app}` (`app`: `{url, version}` or null) |
| `GET /app.apk` | the Android app uploaded by `setup_pi.sh` (no password; 404 if none) |
| `POST /api/login` `{password}` / `POST /api/logout` | sets / clears the HttpOnly `arstro_token` cookie (derived from the password) |
| `GET /ws` | WebSocket session (the protocol above) |
| `GET /ws/preview` | live H.264 preview (below) |
| `GET /api/media/<file>` | a recording, HTTP Range; `?download=1` |
| `GET /api/thumb/<take>` | JPEG thumbnail |
| `POST /api/op/<op>` | one op, JSON body → `{ok, data \| error}` |

Credentials: the cookie, `Authorization: Bearer <password>`, or `?token=<password>`. Requests
whose `Origin` does not match `Host` get 403; in open mode the `Host` must be an IP address or
a local name.

## Live preview `/ws/preview` (REC-02)

Text messages: `{"type":"config","codec":"h264","width","height","fps","bitrate","quality"}` and
`{"type":"state","state":"starting|live|no-signal|stopped","detail"}`.
Binary messages: `[version u8 = 1][flags u8: bit0 keyframe][pts_us u64 BE][Annex-B access unit]`
- keyframes (every 0.5 s, one slice per picture) carry SPS/PPS. One encoder serves every viewer;
a new viewer gets a keyframe right away; a viewer more than ~8 frames behind skips to the next
keyframe (requested at once) - live beats complete. The encoder stops ~5 s
after the last viewer leaves (REC-06).
