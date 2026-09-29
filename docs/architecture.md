# Architecture

**One server, three controllers that stay in sync.** The `arstro-remote` daemon on the
board (Orange Pi 5 Plus, or any Linux machine - ARC-08) owns every piece of hardware and state (ARC-01). The Android app, the CLI and the
web UI are thin controllers: they send ops and render the state events the server pushes
(ARC-02, ARC-03). A change made anywhere shows up everywhere within a second.

```
 Android app ──Bluetooth RFCOMM (control)──┐
     └────────Wi-Fi HTTP/WS (media)───┐    │
 Browser ─────HTTP + WebSocket────────┤    │
 CLI (remote) ─WebSocket──────────────┤    │
 CLI (on Pi) ──unix socket────────────┼────┤
                                      ▼    ▼
 ┌──────────────────────── arstro-remote daemon (python, desktop user) ─────────────────┐
 │  web/server.py        bluez.py (RFCOMM profile, pairing window)    control socket    │
 │        │                     │                                          │             │
 │        └──────────► Session (one per connection: ops, replies, events) ◄┘             │
 │                         │                  ▲ state events                           │
 │   Hub (state topics) ◄──┼──────────────────┘  recorder · recorder.settings · jobs    │
 │                         │                     gallery · wifi · terminals · pairing   │
 │                         │                     controllers · web                     │
 │   TerminalPool (shared PTYs, replay ring)   stats.py   wifi.py (nmcli)              │
 │   RecorderService ── worker IPC ──► recorder worker process (GStreamer, HDMI RX)    │
 │        │   └── JobManager ──► transcode processes (VPU / x265 / FFV1 / GPU FFV1)     │
 │        └── Preview fan-out ──► /ws/preview viewers (one encoder, many viewers)       │
 │   input helper process (X11 XTEST)                                                   │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

## Modules (MOD-01..04)

Features are grouped in modules; `config.json` → `modules` (null = all) decides which start.
`modules.py` maps op prefixes to modules; `Session.dispatch` refuses ops of a disabled module
and routes the rest to the owning service (`ROUTES`). Every service has `start()`,
`shutdown()` and `handle(session, op, msg)`.

| Module | Ops | Service (daemon attribute) | Standard interface |
|---|---|---|---|
| monitor | `stats.*` | `StatsCollector` | psutil, /proc, /sys |
| system | `admin.*`, `web.*`, `system.*`, `log.*` | `SystemService` (`system`), `WebServer` (`web`) | log files, systemctl |
| camera | `recorder.*`, `gallery.*`, `jobs.*`, `camera.*` | `RecorderService` (`recorder`) | V4L2 + GStreamer, ffmpeg |
| connection | `wifi.*`, `net.*`, `bt.*` | Session ops + `ConnectionService` (`conn`) | NetworkManager (nmcli), BlueZ (bluetoothctl) |
| terminal | `term.*` | `TerminalPool` (`terms`) | PTYs |
| screen | `screen.*`, `in.*` | `ScreenService` (`screen`), input helper | X11 (ximagesrc, XTEST) |
| io | `io.*` | `IoService` (`io`, `hwio/`) | GPIO chardev uAPI v2, i2c-dev, spidev, termios, sysfs PWM / LEDs / IIO |
| files | `files.*` + `/api/files/*` | `FilesService` (`files`) | the file system, inside `files_roots` |

Board-specific knowledge is only *information* in `boards/` (detected by the device-tree
`compatible`): the Orange Pi 5 Plus 40-pin header (pin → GPIO chip/line, alternate
functions generated from the board's pinctrl groups). Controller names (`i2c2`, `pwm14`,
`serial9`) come from the device tree's `aliases` / `__symbols__` (`hwio/dt.py`), which is standard.
A UART opened by `io.uart.open` is a `SerialTerminal` in the shared `TerminalPool`, so every
controller attaches to it like to a shell.

## Slots A/B (ADM-06)

```
 slot A  ~/.local/share/arstro-remote-a/  ~/.config/arstro-remote-a/  ~/.local/state/arstro-remote-a/
         $XDG_RUNTIME_DIR/arstro-remote-a.sock|.lock   port 8080   autostart arstro-remote-a.desktop
 slot B  ... arstro-remote-b ...                                    port 8081   autostart arstro-remote-b.desktop
```

`ARSTRO_SLOT` (set by the autostart entry / systemd unit / `arstro-remote-<slot>` wrapper)
selects the instance in `paths.py`; without it the paths are the plain `arstro-remote` ones of
an old, unslotted install. Both slots run at boot. Only the *active* slot (`install.sh
--activate`, `~/.config/arstro-remote-active`) owns the app's Bluetooth link and the plain
`arstro-remote` command. Recordings live in the recorder's storage folder (shared); the
Android app offered at `/app.apk` is shared (`~/.local/share/arstro-remote/app`). Shared
hardware is handled carefully: the HDMI EDID is re-written only when it changes (a write
re-plugs the source), capture only runs while someone watches or records.

## Logging (LOG-01..03)

`<state>/arstro-remote.log` (rotating, 5 × 5 MB; format `time level component [thread]: msg`),
`launcher.log`, `recorder.log`, `screen.log` (helper processes' stderr) and `crash.log`
(faulthandler: Python stacks of a hard crash). Every failed op is a WARNING with op, session
and controller (traceback at DEBUG); uncaught exceptions of any thread are logged. `log.*`
ops read them from any controller; `log.level` changes the level live.

## Processes (fault isolation, ARC-06)

| Process | Role | If it dies |
|---|---|---|
| `arstro-remote run` | the daemon: sessions, hub, shells, Wi-Fi, Bluetooth, web | the launcher restarts it within seconds (ARC-07) |
| `recorder.worker` | HDMI RX capture pipeline: tee → preview branch (H.264), recording branch (H.265 / RAW `.arh`) | the service ends the recording with a reason, keeps the file, restarts the worker with back-off |
| `recorder.transcode` | one per conversion job | the job is marked failed; nothing else notices |
| `input_x11 --serve` | mouse/keyboard via XTEST | respawned on the next input op |
| `screen.worker` | remote screen: X desktop (`ximagesrc`, with the pointer) → x264 → the daemon; only while someone watches | the service retries with back-off and tells the viewers why |
| `arstro-remote-launcher` | per-slot supervisor started by the desktop autostart entry or systemd user unit; restarts the daemon (quickly after a requested restart, with back-off after crashes) | - (it is the session's child) |

The daemon never loads GStreamer: capture and encoding live in the worker, and the gallery
uses `ffprobe`/`ffmpeg` subprocesses. The daemon runs in the auto-login desktop session because
that is where polkit allows `nmcli` Wi-Fi changes and where the X display is.

## State and sync (ARC-03)

* Every mutable thing is a **topic** in the `Hub`. Whoever changes it calls
  `hub.publish(topic, data)`; the hub drops unchanged values (JSON fingerprint) and every
  session queues a `state` event. Several updates before a session's sender runs collapse into
  the newest, so a slow link never falls behind.
* `hello` returns a snapshot of all topics, so a controller that (re)connects is complete at once.
* Controllers never keep their own copy of server state; they render the topic.

## Media path (REC-02, CON-02)

HDMI RX (`rk_hdmirx`, V4L2) → GStreamer tee in the worker:

* **preview** (smooth and live first): leaky 1-frame queue → nearest-neighbour scale (reads only
  the pixels it keeps - capture memory is uncached) → `x264enc` ultrafast/zerolatency, one slice
  per frame, keyframe every 0.5 s → access units over the worker IPC → `Preview` fan-out
  (per-viewer queue of 8 frames and a 64 KB socket buffer; a late viewer skips to the next,
  immediately requested keyframe) → `/ws/preview`. Measured from a 4K 4:2:2 source: 30 fps at
  360p/720p, 28 fps at 1080p. (Without x264 the VPU encoder is used in VBR mode; its CBR mode
  re-encodes frames and manages only ~22 fps.) Browsers decode with WebCodecs (secure context)
  or MSE (jmuxer, kept at the live edge); the app decodes with MediaCodec into a Flutter
  `SurfaceProducer` texture.
* **recording**: `mpph265enc` to MP4/MKV, or RAW `.arh` (uncompressed frames + audio) with
  optional HQ H.265 / FFV1 copies made during or after recording by transcode jobs.

The app gets the web addresses and the password over Bluetooth (`web.info`), probes
`/api/ping` on each address and uses the first that answers; if none does it says why.

## Security (SEC-01..05)

* Bluetooth: the adapter is discoverable/pairable only in the pairing window; only bonded,
  trusted devices may connect.
* Local CLI: unix socket 0600 in the user's runtime dir.
* Web / remote CLI: the access password (user-chosen, random until set, 0600 file); the browser
  keeps a cookie derived from it; Origin must match Host; open mode is an explicit choice and
  then only LAN host names are accepted (DNS rebinding).
* Passwords are never logged; secrets are never in the repo (`test_repo_policy.py`).

## Repository

| Path | |
|---|---|
| `install.sh`, `server/install.sh` | the one installer (slots, modules, password, packages, permissions, boot) |
| `server/arstro_remote/` | the daemon + CLI (`python3 -m arstro_remote`) |
| `server/arstro_remote/modules.py`, `system.py`, `files.py`, `netconf.py` | module table, System/Logs, Files, Connection services |
| `server/arstro_remote/hwio/` | IO Control: `gpio.py`, `i2c.py`, `spi.py`, `uart.py`, `sysfs.py` (PWM, LEDs, ADC), `dt.py`, `service.py` |
| `server/arstro_remote/boards/` | board modules (pin headers) |
| `server/arstro_remote/recorder/` | recorder service, worker, capture pipeline, jobs, gallery library |
| `server/arstro_remote/web/` | HTTP/WebSocket server and the web UI (`static/`, no build step) |
| `server/tests/` | requirement-tagged tests (`harness.py`; `test_modules.py` starts its own throwaway instance), browser tests in `tests/web/` |
| `app/` | Flutter Android app (`lib/src/ui/recorder/` = Recorder tab, native code in `android/.../kotlin`) |
| `scripts/` | `setup_all.sh`, `setup_pi.sh`, `build_apk.sh`, `dev_tunnel.sh`, `req_coverage.py` |
| `docs/` | requirements, parity, protocol, architecture, traceability |
| `legacy/hdmi-recorder/` | the original standalone recorder, kept for reference |
