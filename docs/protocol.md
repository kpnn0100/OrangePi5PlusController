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
| CLI on the Pi | unix socket `$XDG_RUNTIME_DIR/arstro-remote-<slot>.sock` (0600; `ARSTRO_SOCKET` overrides) | file permissions |
| CLI remote, web UI | WebSocket `ws://<pi>:<port>/ws` (slot A 8080, B 8081, ...) | password (see *HTTP*) |
| Anyone, over the internet | through the device manager (module `relay`): the same HTTP / WebSocket, reached on `127.0.0.1:<port>` by an end-to-end encrypted channel ([relay/PROTOCOL.md](relay/PROTOCOL.md)) | the machine's access key, then the password |

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
terminals, term_keep_sec, slot` and **`modules`** `[{name, title, description, state:
enabled|disabled|failed, note, ops}]` (MOD-01: ops of a module that is not enabled answer
`the <Module> module is not enabled on this server`), and **`state`**: a snapshot of every
state topic. `features` lists only what the enabled modules provide.

### State topics (ARC-03)

Pushed to every session whenever they change (whoever changed them), newest value wins.

| Topic | Data |
|---|---|
| `recorder` | status: `available, caps, signal, capture, recording, preview, disk, mode, mode_text, last, simulate`, and for film work: `storage` `{path, free, total, need, rate, seconds_left, speed, measured, ok, warning, testing}` (bytes, bytes/s; `ok` null = disk not measured, REC-12), `audio_inputs` `[{id: hdmi\|alsa:<card>, name, usb?}]` (REC-09), `monitor` `{analysis, zooms, lut, audio_source, luts: [{id, name, builtin}], lut_error, bands: [{from, to, colour, meaning}]}` (REC-13), `clock_synced` (true/false/null, REC-11), `take` `{next, tc, markers}` (`next`: what the slate makes of the next take's name, e.g. `S12A_SH3_T04` or `REC` - the date and time follow; the current take's start timecode and marker count, REC-15), `gpio` `{button: {pin, ok, error, presses}, tally: {pin, ok, error, on}}` (REC-16); `recording` also has `tc` and `av` `{video_s, frames, audio_s?, drift_ms?}` |
| `recorder.settings` | the recording settings (see `recorder.settings.set`) |
| `jobs` | list of jobs `{id, title, codec, state, source, output, progress, fps, eta, live, error, options, verify, attempt, max_attempts, note}`; `state`: queued, running, **verifying** (FFV1 being compared with its RAW), **retrying** (failed, runs again shortly - GAL-10), done, failed, cancelled; `note`: what happened on the way (RAW repaired, damage skipped, retries, resumed after a restart - GAL-09/10); `verify`: `{ok, frames, audio_bytes, detail, raw_deleted?}` or null; `codec` `verify` = an on-demand check |
| `gallery` | summary `{version, takes, files, size, folder}`; fetch `gallery.list` when `version` changes |
| `wifi` | `{device, enabled, connected, ssid, signal, ip, state, connection}` |
| `terminals` | shared shells `[{term, cols, rows, viewers, attached, opened_by, ephemeral, age, detached_for}]` |
| `pairing` | Bluetooth `{ready, alias, address, pairing_open, pairing_remaining, paired_devices}` |
| `controllers` | connected controllers `[{session, controller, kind, peer, since}]` |
| `web` | `{enabled, port, urls, auth: "password" \| "open"}` (never the password) |
| `screen` | remote screen `{available, display, state: stopped\|starting\|live\|error, viewers, screen: [w, h], stream: {width, height, fps, bitrate, quality, encoder: vpu\|cpu}, quality, encoder: auto\|vpu\|cpu, bitrate (0 = auto), error}` |
| `net` | `{devices: [...net.devices], connections: [...net.connections]}` (polled every ~30 s and after every change) |
| `io.gpio` | `{held: [{chip, line, pin, mode, bias, drive, active_low, edge, debounce_us, value, events}], events: [{chip, line, pin, edge: rising\|falling, t}]}` (latest 20 events) |
| `io.pwm` | the `io.pwm.list` chips after a change |
| `apps` | the `apps.list` apps (run state, clients) |
| `relay` | Internet access: `{enabled, url, name, id, key_id, state: off\|connecting\|online\|error, error, since, relay_version, token_set, ports, viewers: [{n, addr, since, streams, bytes_in, bytes_out}], wrong_keys}` - never the key, the join token or the secret |

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
| `recorder.start` / `recorder.stop` | start: `force?` (record although the disk was measured too slow, REC-12) | `{file, ...}` |
| `recorder.mark` | `label?` | a marker in the take being recorded `{tc, frames, t, label}` (REC-15) |
| `recorder.disk_test` | | measures the storage folder's disk: `{speed, bytes, seconds, at, device, fs, mount, direct, figures}` (REC-12); refused while recording |
| `recorder.inputs` | | `{inputs: [{id, name, usb?}], current}`: the audio inputs (REC-09) |
| `recorder.luts` | | `{luts: [{id, name, builtin}], current}` (REC-13) |
| `recorder.lut.add` | `name`, `text` (the .cube file's content) | checked (3D or 1D .cube), saved as `cube:<name>` |
| `recorder.lut.delete` | `lut` (`cube:<name>`) | the list; the monitoring LUT is turned off if it was this one |
| `recorder.settings.get` | | settings |
| `recorder.settings.set` | `settings` (a partial object, e.g. `{"h265": {"bitrate": 60}}`) | validated, saved, published |
| `recorder.edid` | `value` (4k60, 4k30, 1080p, keep) | |
| `recorder.preview.quality` | `quality` (low, medium, high) | |
| `recorder.source` | `simulate` (`"1920x1080@30"`) or null for HDMI RX | status |
| `camera.sources` | | `{sources: [{id: hdmi\|test\|v4l2:/dev/videoN, kind, title, device, available, modes?, note}], current}` |
| `camera.select` | `source`, `mode?` (V4L2: `"MJPG 1280x720@30"`), `spec?` (test: `"1920x1080@30"`) | recorder status (`source`, `camera`); saved in config.json (CAM-01) |
| `recorder.test_signal` | `present` (test source only) | status |
| `gallery.list` | `kind?` (RAW, H.265, H.264, FFV1, VIDEO) | `{folder, version, takes}` |
| `gallery.get` | `take` | one take `{id, title, created, day, month, size, duration, kinds, recording, items, slate, meta}` - `slate` "Scene 12A · Shot 3 · Take 4" or null, `meta` the sidecar's summary `{scene, shot, take, tc, rate, good, notes, markers: [{tc, frames, t, label}], drift_ms, audio_source, clock_synced, drops}` or null (REC-15, GAL-13); `day` (`YYYY-MM-DD`) and `month` (`YYYY-MM`) are the board's date of the take, so every controller groups alike (GAL-01); an H.265/H.264 item carries `mse` (its RFC 6381 codec string, e.g. `hvc1.1.6.L150.B0`, GAL-11) |
| `gallery.bundle` | `takes?`, `files?`, `day?`, `month?`, `kind?` (default `all`) | `{id, name, url, files, size, skipped: [{file, why}], expires}`: the files to download together (GAL-12) - files still recorded or written are left out in `skipped`; `url` (`/api/bundle/<id>.zip`) serves them as one ZIP for an hour |
| `gallery.targets` | | `{targets: [{id, title, available, description, options}]}`; `options.bitrate_max` (Mbit/s): 200 for the VPU targets |
| `gallery.convert` | `file, target, quality?, scale?, preset?, bitrate?, rc?, chroma?, replace_raw?` | the new job (`bitrate` Mbit/s, 0.1 - `bitrate_max`; `rc` vbr / cbr); `replace_raw` (FFV1 from RAW, default = setting `raw.ffv1_replace_raw`): check the copy, then delete the RAW |
| `gallery.verify` | `file` (the take's FFV1 or RAW), `delete_raw` | a *verify* job: decodes the FFV1 and compares every frame plane and audio sample with the RAW byte for byte; with `delete_raw` the RAW is deleted only if all of it is identical (GAL-08) |
| `gallery.delete` | `file` | refused while it is recorded or converted |
| `gallery.annotate` | `take`, any of `good` (bool), `notes`, `scene`, `shot`, `take_no` | the take's `meta` (GAL-13) |
| `gallery.csv` | `takes?`, `day?`, `month?` | `{name, rows, csv}`: one row per file, Resolve's metadata columns (GAL-13) |
| `gallery.delete_take` | `take` | |
| `jobs.list` / `jobs.clear` / `jobs.cancel` | `job` (cancel: the job's number - never `id`, which is the request's own number) | `{jobs}` / job |
| `screen.status` | | the `screen` topic |
| `screen.settings.set` | `settings: {quality?, encoder?, bitrate?}` - quality low ≤ 960 px 15 fps, medium ≤ 1280 px 30 fps, high native 30 fps; encoder `auto` (VPU when present) / `vpu` / `cpu` (x264); bitrate bits/s, 0 = from the quality, else 200000 - 20000000 (SCR-05) | saved, shared; only the given keys change; a running capture restarts |
| `admin.status` | | `{version, bluetooth, input, web, recorder, sessions}` |
| `admin.pair` | `seconds` (0 closes) | status |
| `admin.unpair` | `address` | |
| `web.info` | | `{enabled, port, urls, auth, app, token, slot, slots}` - `token` is the password; `slots`: every A/B slot of the machine `[{slot, port, running, self, auth, token}]` (TERM-06: the app opens the others over Wi-Fi; the shared `web` state carries `slots` without `auth`/`token`) |
| `web.set_password` | `password` (5..256 chars) | info; signs out the other web/remote sessions |
| `web.rotate_token` | | a new random password |
| `web.set_auth` | `required` (bool) | info; `false` = open mode |
| `system.info` | | `{version, slot, instance, port, pid, hostname, machine, kernel, python, board, model, uptime, paths: {code, install, config, logs, socket}, bluetooth, modules, log_level}` |
| `system.modules` / `system.modules.set` | `modules` (list), `restart?` (default true) | saved to config.json; the server restarts (MOD-02) |
| `system.restart` / `system.reboot` / `system.poweroff` | | the server exits 0 (the launcher restarts it) / `systemctl reboot\|poweroff` |
| `log.files` | | `{dir, files: [{name, path, size, mtime}]}` |
| `log.tail` | `file?` (default `arstro-remote`), `lines?` (≤ 2000), `grep?`, `level?` | `{file, path, lines}` |
| `log.level` | `level?` (debug, info, warning, error), `save?` | `{level}` |
| `log.mark` | `text` | writes a marker line (WARNING) |
| `net.status` / `net.devices` / `net.connections` | | the `net` topic / `{devices}` / `{connections}` |
| `net.connection.get` | `uuid` | `{name, uuid, type, interface, autoconnect, ipv4: {method, addresses, gateway, dns, never_default}, ipv6, mtu}` |
| `net.connection.set` | `uuid`, any of `name, autoconnect, interface, ipv4_method (auto\|manual\|shared\|disabled\|link-local), addresses, gateway, dns, mtu` | the profile (validated first; reactivate to apply) |
| `net.connection.add_ethernet` | `interface, name?, ipv4_method?, addresses?, gateway?, dns?` | the new profile |
| `net.connection.up` / `.down` / `.delete` | `uuid` | `{message}` |
| `net.device.connect` / `net.device.disconnect` | `device` | `{message}` |
| `bt.status` / `bt.power` | `on` (power) | `{present, address, name, alias, powered, discoverable, pairable, discovering}` |
| `bt.devices` / `bt.scan` | `seconds?` (scan, 3..30) | `{devices: [{address, name, alias, icon, paired, trusted, connected, rssi}]}` |
| `bt.pair` / `bt.connect` / `bt.disconnect` / `bt.trust` / `bt.untrust` / `bt.remove` | `address` | the device |
| `io.info` | | `{board, model, header, gpio: [chips], i2c: [buses], spi, uart, pwm, leds, adc, problems: [text]}` |
| `io.gpio.chips` / `io.gpio.lines` | `chip` (lines: path, number or label) | `{chips}` / `{chip, label, lines: [{line, name, consumer, used, direction, bias, drive, active_low, edge, pin, held, value?}]}` |
| `io.gpio.header` | | `{board, pins: [{pin, name, alt, gpio?: {bank, line, number}, power?, chip, state, held}], enable_hint}` |
| `io.gpio.request` | `chip, line, mode (input\|output), bias (as-is\|pull-up\|pull-down\|disabled), drive (push-pull\|open-drain\|open-source), active_low, edge (none\|rising\|falling\|both), debounce_us, value` | the line; a held line is reconfigured |
| `io.gpio.set` / `io.gpio.get` / `io.gpio.release` / `io.gpio.clear_events` | `chip, line, value` (set) | `{chip, line, value}` |
| `io.i2c.buses` / `io.i2c.scan` | `bus, first?, last?` (scan) | `{buses: [{bus, path, name, dt, access}]}` / `{bus, found, busy}` |
| `io.i2c.transfer` | `bus, addr, write?` (hex `"10 ff"` or list), `read?` (count), `ten_bit?` | `{read: "hex", bytes}` (write, repeated start, read) |
| `io.i2c.dump` | `bus, addr, start?, count?, reg_bytes?` | `{start, data, bytes}` |
| `io.spi.devices` / `io.spi.transfer` | `device, tx (hex), mode, speed_hz, bits, lsb_first?, cs_high?, delay_us?` | `{devices}` / `{rx, bytes}` |
| `io.uart.ports` | | `{ports: [{port, path, driver, kind, dt, console, access}], bauds}` |
| `io.uart.open` | `port, baud, data_bits, parity (none\|even\|odd), stop_bits, flow (none\|rtscts\|xonxoff)` | `{term, settings}` - a *serial console* in the terminal pool (`term.attach` / TERM frames like a shell; `terminals` lists it with `kind: "serial", port, settings, rx_bytes, tx_bytes`) |
| `io.uart.config` / `io.uart.modem` / `io.uart.break` / `io.uart.send` | `term` + settings / `dtr?, rts?` / - / `hex` or `text` | `{settings}` / `{dtr, rts, cts, dsr, cd, ri}` / `{}` / `{sent}` |
| `io.pwm.list` / `io.pwm.set` / `io.pwm.unexport` | `chip, channel, period_ns \| freq_hz, duty_ns \| duty_pct, polarity, enabled` | `{chips}` / the channel |
| `io.led.list` / `io.led.set` | `name, brightness?, trigger?` | `{leds}` / the LED |
| `io.adc.read` | | `{devices: [{device, name, channels: [{channel, raw, scale, mv}]}]}` |
| `files.roots` / `files.list` | `path?, hidden?` (list) | `{roots}` / `{path, parent, root, entries: [{name, path, type, size, mtime, mode, target?}], writable}` |
| `files.stat` / `files.read` / `files.write` | `path` (+ `max?, tail?` / `text`) | entry / `{text, size, truncated}` / entry |
| `files.mkdir` / `files.rename` / `files.delete` | `dir + name` or `path` / `path, to` / `path, recursive?` | entry / entry / `{deleted}` |
| `apps.list` | | `{apps: [{id, name, version, description, icon, origin, problem, single, session: "main", state: stopped\|starting\|running\|failed, detail, pid, clients, started, running_version, api, capabilities, sessions: [{session, state, detail, pid, clients, started, running_version}]}], ntwb, socket, search, registered}` (rescans; the entry's own state is `main`'s, `clients` counts the viewers of every session) |
| `apps.sessions` | `app` | `{app, single, limit, sessions: [{session, state, detail, pid, clients, started, running_version}]}` (APP-09) |
| `apps.info` / `apps.api` | `app`, `session?` | the app + manifest, argv, log path, state keys, sessions / its API description |
| `apps.launch` | `app`, `session?` (`new` starts another), `wait?` | the session (APP-02, APP-04) |
| `apps.stop` | `app`, `session?` (default: every session), `wait?` | the (first) stopped session |
| `apps.call` | `app, method, params?, session?, timeout?` | the method's result - the same route a browser call takes (APP-08) |
| `apps.state` | `app, key?, session?` | `{state, session}` or `{key, data}` - the session's retained NTWB state |
| `apps.log` | `app, lines?, grep?` | `{id, path, lines}` |
| `apps.register` / `apps.unregister` | `path` (ntwb.json or its dir) | the app / `{registered}` |
| `files.upload_check` | `dir, name, overwrite?` | `{path, exists}` - call before a big upload |
| `relay.status` | | the `relay` topic (RLY-01) |
| `relay.set` | `url?, token?, name?, enabled?, ports?` (`ports`: null / `"any"` = any, a list or `"8080, 22"`; an empty url or name resets it; switching on needs url + token) | the `relay` topic; a changed url / token / name / on-off reconnects, a new url or switching off also disconnects the viewers (RLY-08) |
| `relay.key` | | `{key, key_id}` - the access key (RLY-02) |
| `relay.rotate` | | `{key, key_id, closed}` - a new access key; every viewer is disconnected (RLY-02) |
| `relay.kick` | | `{closed}` - disconnects every viewer (RLY-05) |

Gallery items carry `verified` (`{raw, frames, checked}` for an FFV1 proven identical to its RAW, else null).
Take ids are `REC_YYYYMMDD_HHMMSS`; its files are `<take>.arh` (RAW), `<take>_H265.mp4|mov|mkv`,
`<take>_H264[_720p|_1080p].mp4`, `<take>_FFV1.mkv`.

## HTTP (web server, port 8080 for slot A, 8081 for B, ...)

| Route | |
|---|---|
| `GET /` , `/assets/*` | the web UI (no password needed to load) |
| `GET /api/ping` | `{name, version, hostname, authorized, auth, slot, app}` (`app`: `{url, version}` or null) |
| `GET /app.apk` | the Android app uploaded by `setup_pi.sh` (no password; 404 if none) |
| `POST /api/login` `{password}` / `POST /api/logout` | sets / clears the HttpOnly `arstro_token_<port>` cookie (derived from the password; per port so A/B slots keep separate logins) |
| `GET /ws` | WebSocket session (the protocol above) |
| `GET /ws/preview` | live H.264 preview (below) |
| `GET /api/play/<file>?mode=copy\|h264&t=S` | GAL-11: an H.265/H.264 recording as a fragmented MP4 stream from S s (keyframe before it, timestamps from 0), audio AAC; `copy` = the video as it is, `h264` = VPU conversion at 15 fps (one at a time: the same viewer's next request replaces its stream, others get 409 *busy*); no length (`Connection: close`) |
| `GET /ws/screen` | the Pi's desktop, same messages as `/ws/preview`; control it with `in.move_to` (absolute desktop pixels), `in.btn`, `in.scroll`, `in.key`, `in.text` |
| `GET /api/media/<file>` | a recording, HTTP Range; `?download=1` |
| `GET /api/thumb/<take>` | JPEG thumbnail |
| `GET /ws` with `Authorization: Bearer <password>` | the control channel for programs (CLI `--url`, the app's link to another slot, TERM-06): binary messages carry exactly the Bluetooth frames |
| `GET /api/bundle/<id>.zip` | the files of a `gallery.bundle` as one ZIP, streamed: entries stored (not recompressed), ZIP64 where a value passes 32 bits, exact `Content-Length` before the first byte; 404 once expired (GAL-12) |
| `POST /api/op/<op>` | one op, JSON body → `{ok, data \| error}` |
| `GET /api/files/download?path=P` | a file of the Files module (HTTP Range); `&inline=1` to show instead of save |
| `GET /apps/<id>/[file]` | an NTWB app's web UI; `/apps/<id>/icon`, `/apps/<id>/api.json`; unauthenticated → 302 to the login |
| `GET /ws/app/<id>[?session=<id>\|new]` | WebSocket: an NTWB client of one session of the app (default `main`; `new` starts one) - see [ntwb/NTWB.md](ntwb/NTWB.md), [ntwb/API.md](ntwb/API.md) |
| `GET /ntwb/ntwb.js` | the NTWB web SDK |
| `PUT /api/files/upload?dir=D&name=N[&overwrite=1]` | raw body (Content-Length required) streamed to `D/N` → `{ok, data: entry}` |

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

**Film work (REC-10, REC-13, REC-14).** A viewer that sends nothing gets nothing more. A viewer
may send text:

- `{"type":"hello","want":["levels","analysis"]}` - then gets
  - `levels`: text `{"type":"levels","peak":[dB...],"rms":[dB...]}` ten times a second (one value
    per channel, dBFS; recorded audio, also when not recording);
  - `analysis`: binary `[version u8 = 2][kind u8 = 1][json length u16 BE][JSON][payload]` four
    times a second while the settings show zebras, false colour or a scope. JSON
    `{gw, gh, ww, wl, ire_max: 110, range}`; payload = `hist` (128 bytes: share per 1 % IRE,
    0..127 %, sqrt-scaled 0..255) + `wave` (`wl` x `ww` bytes: row i = IRE [i, i+1) x 110 / wl,
    **row 0 = 0 %**, column = 1/ww of the width, sqrt-scaled 0..255) + `map` (`gw` x `gh` bytes,
    rows from the top: each cell's level as IRE x 2, i.e. 200 = 100 %). Zebras = cells at or
    over `monitor.zebra_level`; false colour = the `bands` of the `recorder` topic. Measured on
    the full captured frame, not the preview.
- `{"type":"zoom","on":true,"x":0.5,"y":0.5,"size":0.4}` - a **loupe** (REC-14) around that point
  (0..1 of the whole picture); `size` = the loupe circle's diameter as a share of the picture's
  displayed height (0.02..1). The whole picture keeps coming as before; next to it the viewer gets
  the loupe's own stream: binary `[version u8 = 3][flags u8: bit0 keyframe][pts_us u64 BE][Annex-B
  access unit]` - a square crop of the full-resolution frame, side = size x picture height /
  `monitor.zoom` source pixels (a multiple of 8, at least 64; scaled down past 1080), i.e. what
  the circle covers, `monitor.zoom` times larger. Text reply `{"type":"zoom","on":true,"ratio","x",
  "y","size","width","height","fps","src_width","src_height","crop":[left,top,w,h]}` (width x
  height = the loupe stream's size; its stream starts with a keyframe whenever they change -
  a new size or ratio). Sending another point moves it: only a new `zoom` comes back, the stream
  goes on. `{"type":"zoom","on":false}` - or a refusal `{"type":"zoom","on":false,"error"}`
  (e.g. 3 loupes open already) - ends it. The loupe is per viewer.
