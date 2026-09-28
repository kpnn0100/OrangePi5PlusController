# Architecture

**One server, three controllers that stay in sync.** The `arstro-remote` daemon on the
Orange Pi owns every piece of hardware and state (ARC-01). The Android app, the CLI and the
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

## Processes (fault isolation, ARC-06)

| Process | Role | If it dies |
|---|---|---|
| `arstro-remote run` | the daemon: sessions, hub, shells, Wi-Fi, Bluetooth, web | the launcher restarts it within seconds (ARC-07) |
| `recorder.worker` | HDMI RX capture pipeline: tee → preview branch (H.264), recording branch (H.265 / RAW `.arh`) | the service ends the recording with a reason, keeps the file, restarts the worker with back-off |
| `recorder.transcode` | one per conversion job | the job is marked failed; nothing else notices |
| `input_x11 --serve` | mouse/keyboard via XTEST | respawned on the next input op |
| `arstro-remote-launcher` | supervisor started by the desktop autostart entry | - (it is the session's child) |

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

* **preview**: scale (RGA zero-copy for NV12 dmabuf, else CPU) → `mpph264enc` CBR, GOP = 1 s
  (software `x264enc` when there is no Rockchip VPU) → access units over the worker IPC →
  `Preview` fan-out → `/ws/preview`. Browsers decode with WebCodecs (secure context) or MSE
  (jmuxer); the app decodes with MediaCodec into a Flutter `SurfaceProducer` texture.
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
| `server/arstro_remote/` | the daemon + CLI (`python3 -m arstro_remote`) |
| `server/arstro_remote/recorder/` | recorder service, worker, capture pipeline, jobs, gallery library |
| `server/arstro_remote/web/` | HTTP/WebSocket server and the web UI (`static/`, no build step) |
| `server/tests/` | requirement-tagged tests (`harness.py`), browser tests in `tests/web/` |
| `app/` | Flutter Android app (`lib/src/ui/recorder/` = Recorder tab, native code in `android/.../kotlin`) |
| `scripts/` | `setup_all.sh`, `setup_pi.sh`, `build_apk.sh`, `dev_tunnel.sh`, `req_coverage.py` |
| `docs/` | requirements, parity, protocol, architecture, traceability |
| `legacy/hdmi-recorder/` | the original standalone recorder, kept for reference |
