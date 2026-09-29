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
| ARC-05 | **No environment-specific values in code or tests** (addresses, serials, user names, SSIDs, passwords, tokens). They come from arguments, environment variables or local config files (`local.env`, `~/.config/arstro-remote-<slot>/`). |
| ARC-06 | **Fault isolation.** HDMI capture/encoding and X11 input run in helper processes. A crash there never stops the daemon, Bluetooth sessions or shells; the helper is restarted automatically. |
| ARC-07 | **Startup.** Each installed slot starts at boot - with the desktop session (autostart + supervisor), or as a systemd user service on machines without a desktop (`install.sh --boot`) - restarts if it exits, and needs no root at runtime. |
| ARC-08 | **Runs on any Linux machine.** Only standard Linux interfaces are used (NetworkManager, BlueZ, V4L2, GPIO character device, i2c-dev, spidev, termios, sysfs PWM/LED/IIO, X11); what a feature needs but the machine lacks turns that feature off with a reason instead of failing the server. |

## SEC – Access and security

| ID | Requirement |
|---|---|
| SEC-01 | Bluetooth pairing is only possible inside the pairing window (default 600 s after start; `admin.pair`). Outside it the adapter is neither pairable nor discoverable. |
| SEC-02 | The Bluetooth service accepts only trusted devices (bonded inside the window). |
| SEC-03 | Web and remote-CLI access need the **access password** (≥ 5 characters; asked by `install.sh` for a new slot, Enter = `admin`, which the installer warns about; a new slot copies the password of an installed one; stored 0600 in the slot's config dir, never in the repo). It can be changed, or replaced by a random one, from any controller; that signs out the other web/remote sessions. An **open mode** (no password) can be switched on explicitly (user decision 2026-09-28: simple access on the home LAN). Requests from other web sites are refused (Origin must match Host); in open mode only LAN host names are accepted (no DNS rebinding). |
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
| GAL-08 | **Verified lossless FFV1 replaces the RAW.** When an FFV1 copy of a RAW take is made (after recording or from the gallery) with *delete RAW after a verified copy* on (setting `raw.ffv1_replace_raw`, default on), the server decodes the copy and compares **every frame, plane by plane, byte for byte, and every audio sample** with the RAW. Only if all of it is identical (same frame count, same size, same pixel layout) the RAW is deleted and the FFV1 becomes the take's main source (marked *verified*); otherwise the RAW is kept and the reason is shown (first differing frame / plane / pixel). The RAW is never deleted while it is recorded or used by another job. Any existing RAW + FFV1 pair can be checked on demand, with or without deleting the RAW, from every controller. |

## SCR – Remote screen (screen cast + control, like TeamViewer / UltraViewer)

| ID | Requirement |
|---|---|
| SCR-01 | **Remote screen.** The Pi's desktop (X display, with its mouse pointer) is streamed live as H.264 over WebSocket to any number of viewers from one shared encoder - smooth and live first: ≥ 20 fps and < 0.5 s behind; a late viewer skips ahead. The capture runs in a helper process (ARC-06) and only while someone watches (stops ~5 s after the last viewer). |
| SCR-02 | **Control from the picture.** A viewer can take control: the mouse maps to the same point of the Pi's screen (move, left/middle/right press and release, drag, wheel), the keyboard types into the Pi (keys with modifiers, text), and local text can be sent as typing. *View only* switches control off. |
| SCR-03 | Quality presets (low ≤ 960 px wide at 15 fps, medium ≤ 1280 px at 30 fps, high native at 30 fps) are a shared, saved setting; the view shows the Pi's screen size and follows a change of it. When there is no desktop the view says why. |
| SCR-04 | All controllers: Web (Screen tab, mouse + keyboard), GUI (Remote › Screen, touch: tap = click, long press = right click, drag, two-finger scroll), CLI (`screen status / quality / save`). |

## ADM – System

| ID | Requirement |
|---|---|
| ADM-01 | Status: server version, Bluetooth adapter, pairing window, paired devices, connected controllers. |
| ADM-02 | Open or close the pairing window; forget a paired device. |
| ADM-03 | Show web access (URLs, port, password, open or password mode); change the password or the mode. |
| ADM-04 | System info: version, slot, port, board / model, kernel, code / config / log paths, the modules and their state. |
| ADM-05 | Restart the server, reboot or power off the machine from any controller (with a confirmation in the GUI / web, `--yes` in the CLI). |
| ADM-06 | **A/B slots.** Several instances install side by side (`install.sh --slot a\|b`): own code, config, logs, control socket, lock, port (8080 + index) and start-at-boot entry, so a new version is installed and tested in the slot not in use while the other keeps serving. Only one slot owns the app's Bluetooth link (`--activate` moves it and the default `arstro-remote` command); hardware changes one slot makes do not disturb the other (e.g. the HDMI EDID is re-written only when it changes). |
| ADM-07 | The web UI shows which slot it talks to (next to the host name) and keeps a separate login per port. |

## MOD – Feature modules

| ID | Requirement |
|---|---|
| MOD-01 | Features come in modules - **Monitor, System, Camera, Connection, Terminal, Screen, IO Control, Files** - enabled per machine (`modules` in the slot's config, `install.sh --modules`; default all). The ops of a disabled module are refused with a clear message; hello reports each module's state (enabled / disabled / failed with the reason). |
| MOD-02 | The modules can be changed from any controller (System › Modules, `system modules --set`); the server restarts itself to apply it. |
| MOD-03 | Modules use standard Linux interfaces only; board-specific knowledge lives in a board module (`boards/`, e.g. the Orange Pi 5 Plus 40-pin header map) that only adds information. On an unknown board everything else works. |
| MOD-04 | The controllers group their pages by module and show only the enabled ones (web: sidebar / tab bar by group, sub-tabs inside a group). |

## CAM – Camera

| ID | Requirement |
|---|---|
| CAM-01 | The camera source is chosen from any controller: the board's HDMI input, a V4L2 (USB/UVC) camera, or the test pattern; the choice is saved and survives restarts. Live view, recording, gallery and conversions work the same for every source. |
| CAM-02 | V4L2 cameras are found automatically (USB capture devices with a usable pixel format) with their modes; the best mode up to 1080p is picked unless one is chosen. |
| CAM-03 | The gallery is part of the Camera group (web: Camera › Live / Gallery). |

## NET – Connection

| ID | Requirement |
|---|---|
| NET-01 | List the network interfaces (type, state, addresses, gateway, DNS, cable, MAC) and connect / disconnect an Ethernet interface. |
| NET-02 | List the saved NetworkManager connections (Ethernet, Wi-Fi, ...) and edit one: name, auto-connect, IPv4 method (DHCP / static / shared / off), addresses, gateway, DNS, MTU - validated before anything is changed. |
| NET-03 | Activate, deactivate, delete a connection; add an Ethernet profile (DHCP or static). |
| NET-04 | Bluetooth devices the machine uses (keyboards, mice, speakers...): scan, pair (+ trust), connect, disconnect, forget. |
| NET-05 | Bluetooth adapter status and power on / off. |

## IO – IO Control (board bring-up and debugging)

| ID | Requirement |
|---|---|
| IO-01 | The installer gives the user access to the IO devices (udev rules + groups gpio / i2c / spi / dialout, logind uaccess for the desktop user). Missing access is reported per interface with the fix. |
| IO-02 | GPIO: list the chips and every line (name, consumer, direction, bias, drive, active-low, edge) through the GPIO character device (uAPI v2). |
| IO-03 | GPIO: request a line as input (bias, active-low, edge events with debounce) or output (push-pull / open-drain / open-source, level); set, read, release. Held lines and timestamped edge events are pushed live to every controller (topic `io.gpio`). |
| IO-04 | I2C: list buses (with their device-tree names), scan like i2cdetect (claimed addresses shown as UU), write-then-read transfers with repeated start, register dumps. |
| IO-05 | SPI: full-duplex transfers on spidev devices with mode, speed and word size. |
| IO-06 | PWM: export channels, set frequency / period, duty cycle, polarity, enable (sysfs PWM). |
| IO-07 | LEDs: brightness and trigger (sysfs LEDs). |
| IO-08 | ADC: every IIO voltage channel, raw and mV, refreshed live. |
| IO-09 | UART: open a serial port (baud, data bits, parity, stop bits, flow control) as a serial console in the shared terminal pool - every controller attaches like to a shell; send hex, send break, read and set the modem lines (DTR/RTS/CTS/DSR/CD/RI), change the settings live. |
| IO-10 | On a known board the pin header is shown pin by pin (power, ground, GPIO with live state and alternate functions) and a pin opens its GPIO controls; the page says how to enable a bus function (device-tree overlay). |

## FILE – Files

| ID | Requirement |
|---|---|
| FILE-01 | Browse the shared folders (`files_roots`, default the user's home); nothing outside them is reachable (symlinks and `..` resolved). |
| FILE-02 | Upload files of any size (streamed to disk, atomic, free space checked; existing files only replaced when asked). |
| FILE-03 | Download any file of the shared folders. |
| FILE-04 | Create folders, rename, delete (folders only recursively when asked), view and edit small text files. |

## LOG – Logging

| ID | Requirement |
|---|---|
| LOG-01 | Every slot keeps rotating log files in its state dir (`arstro-remote.log` 5 × 5 MB, plus launcher / recorder / screen / crash logs) with time, level, component and thread; the level (debug / info / warning) is set in the config or at run time. |
| LOG-02 | Every failed op is logged as a warning with the op, the controller and the reason (the traceback at debug level); uncaught exceptions in any thread are logged with their traceback; a hard crash leaves a Python stack in `crash.log`. Secrets are never logged (SEC-05). |
| LOG-03 | Logs are readable from any controller: list, tail with a filter, follow live, download, change the level, write a marker line. |

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
| SET-05 | **One installer** (`install.sh` at the repo root, same as `server/install.sh`) sets the server up on any Debian/Ubuntu-family machine: packages for the chosen modules (sudo, only what is missing), device permissions, the slot's code / config / password / start at boot, then starts it and checks that it answers. `--uninstall` removes a slot. |
| SET-06 | `scripts/setup_pi.sh --slot a\|b` deploys from the dev PC into a slot (device permissions through the Pi's sudo password). |
