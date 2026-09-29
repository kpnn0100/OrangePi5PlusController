# Requirements – OrangePi5PlusController

Single source of truth for what the system must do. Every change starts here:
new behaviour gets a requirement (or an edit of one) **before** it is implemented,
and every requirement names the tests that prove it (`docs/traceability.md` is generated
from the test files by `scripts/req_coverage.py`).

IDs are stable: never renumber, mark removed ones `(withdrawn)`.
"All controllers" = GUI (Android app), CLI (`arstro-remote`), Web UI.

## ARC – Architecture

| ID | Requirement |
|---|---|
| ARC-01 | **One server.** The `arstro-remote` daemon on the Pi owns all hardware and state (Bluetooth, Wi-Fi, shells, mouse/keyboard, HDMI RX, recordings, jobs). Controllers never touch hardware directly. |
| ARC-02 | **Three controllers** – GUI, CLI and Web UI – use the same protocol and the same op names (`docs/protocol.md`). |
| ARC-03 | **Live sync.** Every state change (recorder, recording, preview, jobs, gallery, recorder settings, Wi-Fi, terminals, pairing, connected controllers) is pushed as a `state` event to every connected controller within 1 s, whichever controller or process caused it. |
| ARC-04 | **Parity.** Every feature in this document is usable from all three controllers. The matrix in `docs/parity.md` lists how; exceptions must be listed there with a reason. |
| ARC-05 | **No environment-specific values in code or tests** (addresses, serials, user names, SSIDs, passwords, tokens). They come from arguments, environment variables or local config files (`local.env`, `~/.config/arstro-remote/`). |
| ARC-06 | **Fault isolation.** HDMI capture/encoding and X11 input run in helper processes. A crash there never stops the daemon, Bluetooth sessions or shells; the helper is restarted automatically. |
| ARC-07 | **Startup.** The daemon starts with the desktop session at boot (autostart + supervisor), restarts if it exits, and needs no root at runtime. |

## SEC – Access and security

| ID | Requirement |
|---|---|
| SEC-01 | Bluetooth pairing is only possible inside the pairing window (default 600 s after start; `admin.pair`). Outside it the adapter is neither pairable nor discoverable. |
| SEC-02 | The Bluetooth service accepts only trusted devices (bonded inside the window). |
| SEC-03 | Web and remote-CLI access need the **access password** (≥ 8 characters; random until the user sets their own; stored 0600 in the config dir, never in the repo). It can be changed, or replaced by a random one, from any controller; that signs out the other web/remote sessions. An **open mode** (no password) can be switched on explicitly (user decision 2026-09-28: simple access on the home LAN). Requests from other web sites are refused (Origin must match Host); in open mode only LAN host names are accepted (no DNS rebinding). |
| SEC-04 | The local control socket is 0600 in the user's runtime directory. |
| SEC-05 | The access password and Wi-Fi passwords are never written to logs. |

## CON – Controller connections

| ID | Requirement |
|---|---|
| CON-01 | The GUI connects over Bluetooth RFCOMM, reconnects automatically with back-off and resumes shells byte-exact. |
| CON-02 | The GUI uses Wi-Fi (the web server) for media – preview, thumbnails, playback – after getting the server's addresses and token over Bluetooth. It shows why media is unavailable (e.g. phone not on the Pi's network). |
| CON-03 | The Web UI asks for the password once and remembers it (HttpOnly cookie derived from the password, not the password itself); a wrong or changed password shows the login again. In open mode there is no login. The app never asks: it gets the password over Bluetooth. |
| CON-04 | The CLI works on the Pi (local socket, no token) and from any machine (`--url`, `--token` / `ARSTRO_URL`, `ARSTRO_TOKEN`). |
| CON-05 | Every controller shows which other controllers are connected (`controllers` state). |

## STAT – Monitor

| ID | Requirement |
|---|---|
| STAT-01 | Snapshot: hostname, OS, kernel, uptime, IP addresses, gateway, Wi-Fi SSID/signal, CPU total/per core/frequency/load, temperatures, memory, swap, disks, GPU/NPU/DDR load, fan, network rates, top processes. |
| STAT-02 | Live updates every 2 s while a controller shows them. |

## WIFI – Wi-Fi

| ID | Requirement |
|---|---|
| WIFI-01 | Show status (device, radio, connection, SSID, signal, IP). |
| WIFI-02 | Scan networks (one entry per SSID, strongest first, security, saved flag). |
| WIFI-03 | Connect: saved network, new network with password, hidden network. Errors are explained (wrong password, not found, not authorised). |
| WIFI-04 | Disconnect. |
| WIFI-05 | Forget a saved network. |
| WIFI-06 | Radio on/off. |
| WIFI-07 | Wi-Fi status changes are pushed to all controllers (ARC-03). |

## TERM – Terminals

| ID | Requirement |
|---|---|
| TERM-01 | Open a login shell on a PTY (`xterm-256color`), write, resize, close; exit code reported. |
| TERM-02 | **Shared shells.** All controllers see the same list of shells and can attach to any; several viewers mirror one shell live. |
| TERM-03 | Shells survive a dropped link: a shell nobody views is kept `term_keep_sec` (600 s); re-attaching replays exactly the missed output. `ephemeral` shells die with their opener. |
| TERM-04 | The shell list (opened, closed, exited, viewers) is pushed to all controllers. |

## INP – Mouse and keyboard

| ID | Requirement |
|---|---|
| INP-01 | Pointer: relative move (fractions accumulate), absolute move, buttons down/up/click/double, scroll (vertical + horizontal). |
| INP-02 | Keys by name with modifiers (ctrl, alt, shift, super, altgr), text typing including Unicode; order is preserved. |
| INP-03 | Buttons and keys held by a controller are released when it disconnects. |
| INP-04 | GUI touchpad: the pad only moves; separate press-and-hold buttons (drag = hold + move), drag lock, scroll strip and two-finger scroll. Web: same pad with mouse/touch. |

## REC – Recorder (HDMI RX)

| ID | Requirement |
|---|---|
| REC-01 | Signal status: present or not, resolution, frame rate, pixel format, and a plain-language reason when there is no picture. |
| REC-02 | **Live preview** as an H.264 stream over WebSocket, to any number of viewers from one shared encoder. **Smooth and live beats sharp:** from any HDMI source (up to 4K, any format) the preview reaches the viewer at ≥ 25 fps with no gap over 100 ms (95 %), and a viewer that falls behind skips ahead to the newest picture instead of lagging (players stay < 0.5 s behind real time). Quality preset (low 360p / medium 720p / high 1080p) is a shared setting. A new viewer gets a picture within 2 s (keyframe on join). |
| REC-03 | Start and stop recording from any controller. Recording state, elapsed time, size, data rate and dropped frames are pushed live (ARC-03). |
| REC-04 | Recording settings: mode H.265 (bitrate, rate control, keyframe interval, MP4/MKV) or RAW `.arh` with optional extra copies (HQ H.265 on VPU/x265 with quality; FFV1 on CPU/GPU; during or after recording); audio on/off; storage folder; EDID. Settings are validated, persist, and are shared (ARC-03). |
| REC-05 | Guards: no start without signal or with < 3 GB free; automatic stop below 1.5 GB free; a crashed capture process ends the recording with a clear reason and keeps the file. |
| REC-06 | Capture runs only while needed (preview viewers or a recording), to save power; it stops ~5 s after the last viewer leaves. |
| REC-07 | Capabilities (VPU H.264/H.265, x265, FFV1, GPU FFV1, audio) are reported; unavailable options are disabled in every controller. |
| REC-08 | A test source (`simulate`, e.g. `1920x1080@30`) can replace HDMI RX for development and automated tests. |

## GAL – Gallery

| ID | Requirement |
|---|---|
| GAL-01 | List takes newest first. A take groups its files (variants: RAW, H.265, H.264, FFV1, other) with size, resolution, fps, duration, audio and problems. Filter by kind. |
| GAL-02 | A thumbnail per take. |
| GAL-03 | Play a variant where the controller can decode it (HTTP with Range); download any variant. |
| GAL-04 | Convert a variant to a target format – H.264 MP4 (VPU, "share", optional downscale), H.265 (VPU or x265; quality or bitrate), FFV1 (CPU or GPU) – as a background job. |
| GAL-05 | Jobs: list with progress, speed and ETA; cancel; clear finished. Pushed live. |
| GAL-06 | Delete one variant (format) of a take, or the whole take. Refused while that file is being recorded or converted. |
| GAL-07 | Gallery changes (new recording, finished conversion, deletion – from any controller) are pushed to all controllers. |

## ADM – System

| ID | Requirement |
|---|---|
| ADM-01 | Status: server version, Bluetooth adapter, pairing window, paired devices, connected controllers. |
| ADM-02 | Open or close the pairing window; forget a paired device. |
| ADM-03 | Show web access (URLs, port, password, open or password mode); change the password or the mode. |

## UX – Look and feel

| ID | Requirement |
|---|---|
| UX-01 | Dark, minimal, consistent design in GUI and Web: one accent colour, rounded cards, clear hierarchy, no clutter. |
| UX-02 | Smooth animations for navigation, sheets and state changes (≤ 300 ms, no jank). The record button animates between idle and recording. |
| UX-03 | Layouts work from a small phone (360×640) to large screens (1920×1080) without overflow. |

## SET – Setup and build

| ID | Requirement |
|---|---|
| SET-01 | `scripts/setup_all.sh` sets everything up in one go: checks the dev tools, sets up the Pi over SSH (password asked once), builds the APK and optionally installs it. Settings come from arguments or `local.env`. |
| SET-02 | `scripts/setup_pi.sh` installs or updates the Pi side over SSH (key installed, packages via sudo, groups, auto-login option, service restart, verification). |
| SET-03 | `scripts/build_apk.sh` builds the signed release APK (creates a signing key if none exists) into `release/`. |
| SET-04 | The Pi offers the latest Android app at `http://<pi>:8080/app.apk` (uploaded by `setup_pi.sh` from `release/`), so a phone on the same network installs or updates it from its browser without a cable; the web UI links to it and the CLI (`web`) prints the link. |
