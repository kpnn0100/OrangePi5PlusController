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
| TERM-05 | **Full screen.** A shell can fill the whole screen - no app chrome, the tabs a slim strip (TERM-08); the browser's own full screen where it allows it, otherwise the whole window; on touch screens the key row stays. **F11** (never passed to the shell while the terminal is shown) or a small button in the top-right corner leaves it; the shell keeps its size in step (resize). |
| TERM-06 | **Every slot from the app.** The app manages the shells of every A/B slot of the Pi it is connected to, not only the slot its Bluetooth link reaches: `web.info` lists the machine's slots (port, running; to a controller that is already in, also each slot's password) and the app reaches the others over Wi-Fi - the same protocol on `/ws` - with a switch between the slots in the Terminal tab. A slot that is stopped, or out of reach without Wi-Fi, says why. A link to another slot is never remembered as the phone's own. |
| TERM-07 | **A native terminal on the phone.** The app's shell takes the phone's own keyboard (no autocorrect) and a hardware keyboard; a key row adds what a phone lacks (Esc, Tab, sticky Ctrl/Alt, arrows, ^C/^D/^Z, Home/End/PgUp/PgDn, symbols). **Copy**: long-press and drag selects, the Copy key or Ctrl+Shift+C puts it on the clipboard; **Paste**: the Paste key or Ctrl+Shift+V (Ctrl+A, Ctrl+C, Ctrl+V stay with the shell). **Full screen**: only the shell and its key row, the system bars hidden; the corner button, Back or F11 leave it. |
| TERM-08 | **Tabs and split panes, like Tilix.** The web lays the shells out in tabs; a tab holds panes split right or down (a new shell beside the focused one), the dividers dragged to resize, a pane maximized and restored, the focus moved between panes; each pane with several has a title bar (split, maximize, close). Keys: Ctrl+Alt+R split right, Ctrl+Alt+D split down, Ctrl+Shift+T new tab, Ctrl+Shift+W close the pane's shell, Ctrl+Shift+X maximize, Alt+arrows move (a key with nothing to do goes to the shell). The shells stay the server's and shared; the layout is each page's own, so a shell opened elsewhere arrives as a tab of its own. The tab strip - with + and the splits - stays in full screen (TERM-05), and in full screen the page holds the whole keyboard, as a native terminal does. The app keeps its tabs (+, switch, close) in full screen; a phone shows one shell at a time. |
| TERM-09 | **Drag and drop, like Tilix.** A pane dragged by its title bar drops on another pane's left / right / top / bottom half - the shell moves there, splitting that pane (the half is shown while dragging) - on a tab (the shell joins that tab; holding over a tab opens it, to drop into its panes) or on the strip's free space (a tab of its own). A tab dragged along the strip reorders the tabs; a one-shell tab dropped on a pane joins it. Esc cancels; works with a finger. The page's own layout (TERM-08): nothing changes on the server. |

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
| REC-04 | Recording settings: mode H.265 (bitrate, rate control, keyframe interval, MP4/MKV) or RAW `.arh` with optional **extra copies, in the order H.264, H.265, FFV1**: an H.264 MP4 share copy on the VPU (source / 1080p / 720p, quality), HQ H.265 on VPU/x265 with quality, FFV1 on CPU/GPU - made during recording (following the RAW as it is written) or after it. Every controller shows each kind of copy as its own sub-section: its switch in the header (which, when on, also says its options), its options inside, opened and closed by the header (opening is the page's own view state); audio on/off; storage folder; EDID. Settings are validated, persist, and are shared (ARC-03). |
| REC-05 | Guards: no start without signal or with < 3 GB free; automatic stop below 1.5 GB free; a crashed capture process ends the recording with a clear reason and keeps the file. |
| REC-06 | Capture runs only while needed (preview viewers or a recording), to save power; it stops ~5 s after the last viewer leaves. |
| REC-07 | Capabilities (VPU H.264/H.265, x265, FFV1, GPU FFV1, audio) are reported; unavailable options are disabled in every controller. |
| REC-08 | A test source (`simulate`, e.g. `1920x1080@30`) can replace HDMI RX for development and automated tests. |
| REC-09 | **Audio for film** (user request 2026-10-27: *"the goal is making movie with the pi as a remote external recorder"*). The real-time recording is **MOV** by default with the audio kept as it came (16-bit 48 kHz PCM); MKV also keeps PCM; MP4 (plays everywhere) has AAC. The audio comes from the HDMI input or from an **audio interface** (any ALSA capture device, e.g. a USB interface with XLR microphones), chosen in the settings from the list of connected ones; a take that starts the capture itself (nobody watching: the record button, the CLI) waits for the first audio before it starts, so it is never silent. Every take records how long its audio and its video run (**A/V sync**, `drift_ms`); a difference of more than a frame is shown with the take and logged. |
| REC-10 | **Audio meters** in the live view: peak and RMS per channel, ten times a second, from the audio that is recorded (also while not recording); a clipped peak (≥ -1 dBFS) shows red. On/off in the monitoring settings. |
| REC-11 | **Timecode.** Every take starts at the time of day of its first frame (board clock, kept by NTP), as SMPTE timecode at the take's rate (drop-frame `;` at 29.97 / 59.94). It is written where editors read it: a QuickTime timecode track (`tmcd`) in every MOV / MP4 - the recording and its copies - a `TIMECODE` tag in FFV1 files made by ffmpeg, the RAW header and the take's sidecar. Whether the clock is NTP-synchronised is shown (a take records it). |
| REC-12 | **Storage you can trust.** The status gives the data rate the chosen mode writes for the current signal, the free space and the **time left** at that rate (while recording: at the measured rate). A **disk speed test** (about 1 GB, a few seconds, flushed to the device) measures the storage folder's disk once and remembers it per filesystem. A disk measured slower than the mode needs (+15 %) refuses the start with the numbers and what to do, unless the user records anyway (`force`; the guard itself can be turned off). |
| REC-13 | **On-set monitoring** in the live view, set once for every viewer: a **recording tally** (red frame), **frame guides** (2.39, 1.85, 16:9, 4:3, 1:1, 9:16), safe areas, centre cross, **zebras** at a chosen level, **false colour** (one band table for every controller), a **histogram** or **waveform**. The exposure data is measured on the full captured frame (not the compressed preview) four times a second and sent only to viewers that show it. A **LUT** for log footage shows the preview (never the recording) through a built-in curve (S-Log3, V-Log, HLG, any log) or a `.cube` file the user adds. |
| REC-14 | **Focus loupe** (user request 2026-10-27: *"user can click on the image to zoom in (zoom ratio will be in setting) that for checking focus, apply for both web and app"*; then: *"make it like a magnifying glass so that server send both the full view and the zoom, client side show a circle with zoom inside"*): clicking (tapping) the live picture opens a **circular magnifying glass** over it at that point - the whole picture stays as it is around it. Inside the circle: what it covers, magnified by the ratio in the settings (2, 3, 4, 6 or 8), cut from the **full-resolution** frame at its own pixels (not an enlarged preview). Dragging moves the loupe (clicking elsewhere moves it there); a close button on it, Esc (web) or Back (app) closes it. The server sends the viewer its whole picture and, next to it, the loupe's own small stream; the loupe is the viewer's own - other viewers keep their picture; up to 3 loupes at once. |
| REC-15 | **Slate and markers.** Scene, shot and take number name the next take (`S12A_SH3_T04_<date>_<time>`; without a scene `REC_<date>_<time>` as before); the take number counts up after each take (can be turned off). **Markers** are set while recording (with an optional label) at the current timecode. Each take keeps a sidecar `<take>.take.json` with its slate, timecode, markers, notes, good-take flag and A/V sync. |
| REC-16 | **Physical controls on the rig:** a **record button** (a GPIO input, pulled up, active low by default: a push button to ground) starts or stops recording like any controller; a **tally light** (a GPIO output) is on while recording and blinks while a take is being finished. The lines are chosen in the settings (`gpiochipN:LINE`, from the board's header map), requested as `arstro-recorder` so IO Control shows them as used; a problem (busy, no permission) is shown in the status. |

## GAL – Gallery

| ID | Requirement |
|---|---|
| GAL-01 | List takes newest first, **grouped by month and by day** - the board's date of the take, published with each take (`day`, `month`) so every controller groups alike, each group with its count and size. A take groups its files (variants: RAW, H.265, H.264, FFV1, other) with size, resolution, fps, duration, audio and problems. Filter by kind. |
| GAL-02 | A thumbnail per take. |
| GAL-03 | Play a variant where the controller can decode it (HTTP with Range); download any variant. |
| GAL-04 | Convert a variant to a target format – H.264 MP4 (VPU, "share", optional downscale), H.265 (VPU or x265; quality or bitrate), FFV1 (CPU or GPU) – as a background job. The VPU targets run at the encoder's own speed, faster than real time (a 4K H.265 recording to a 1080p H.264 copy: ~110 fps, 720p: ~150 fps, 4K: ~57 fps): a frame the VPU decoded goes to the VPU encoder **in place** (its DMA-BUF), so the CPU stays nearly idle, and a clip it decodes is scaled by the decoder (RGA). An **FFV1 source is decoded by the CPU** - the VPU cannot decode FFV1 - so its conversions run at the CPU decoder's speed (4K 4:2:2: ~12 fps on every core; the GPU's Vulkan decoder: ~5 fps), every frame and the audio kept. A bitrate is checked when the job is asked for: the VPU encodes at most 200 Mbit/s (a job saved with more - before this check - is encoded at 200 and says so). FFV1 from a RAW, with GPU encoding set up (`setup_gpu.sh`), runs on **two CPU encoders and the GPU at once**: each frame goes to whichever has room, and the packets are joined in frame order into one file - one stream, as from a single encoder (all use the same FFV1 settings: Golomb-Rice coder, 256 slices) - at **≥ 15 fps for 4K 4:2:2 read from the SSD** (a camera take: 15-16.5 fps; one encoder before: 9.5). An encoder that fails or hangs gives its frames to the others (or to a fresh one), so the job still finishes. Without GPU encoding, FFV1 on the CPU uses every core (~10 fps for 4K 4:2:2). Golomb-Rice: ~17% larger than the range coder, which is too slow on this CPU; still lossless. |
| GAL-05 | Jobs: list with progress, speed and ETA; cancel; clear finished. Pushed live. |
| GAL-06 | Delete one variant (format) of a take, or the whole take. Refused while that file is being recorded or converted. |
| GAL-07 | Gallery changes (new recording, finished conversion, deletion – from any controller) are pushed to all controllers. |
| GAL-08 | **Verified lossless FFV1 replaces the RAW.** When an FFV1 copy of a RAW take is made (after recording or from the gallery) with *delete RAW after a verified copy* on (setting `raw.ffv1_replace_raw`, default on), the server decodes the copy and compares **every frame, plane by plane, byte for byte, and every audio sample** with the RAW. Only if all of it is identical (same frame count, same size, same pixel layout) the RAW is deleted and the FFV1 becomes the take's main source (marked *verified*); otherwise the RAW is kept and the reason is shown (first differing frame / plane / pixel). The RAW is never deleted while it is recorded or used by another job. Any existing RAW + FFV1 pair can be checked on demand, with or without deleting the RAW, from every controller. |
| GAL-09 | **Interrupted recordings stay usable.** A RAW `.arh` cut off by a crash or a power loss (not finalized; often a zero-filled tail, sometimes a damaged stretch) is never an error: every reader (conversion, lossless check, thumbnail, playback index) skips damage by resyncing to the next valid chunk (type, size and time must fit, and the chunk after it too) instead of stopping. Once nobody writes the file (no open writer, unchanged for 60 s, not being repaired by the other slot) the server **repairs** it by itself: keeps every valid chunk, cuts only what follows the last one, adds the index and records the repair (frames kept, bytes removed) - shown in the gallery and on the job. A frame lost to damage is the previous frame repeated, like a capture drop, so the copy keeps its timing and the lossless check (GAL-08) compares exactly what the encoder got. |
| GAL-10 | **Conversions finish.** Unfinished jobs are saved and resumed after a restart, a crash or a power loss (their half-written outputs are removed first). A job that fails, or whose FFV1 copy fails the lossless check, runs again by itself (up to 3 attempts in all, 10 s / 20 s apart; FFV1 then uses one CPU encoder on fewer cores, without the GPU - less peak current on a weak supply); the job shows its attempt and what happened (repaired RAW, skipped damage, retries). Two jobs (also from the other A/B slot) never write the same output: the second one is refused. |
| GAL-11 | **H.265 plays in the browser.** The web UI plays H.265 (and H.264 in any container) recordings directly: the server streams them as fragmented MP4 (`/api/play`), with the audio as AAC (browsers take no PCM). A browser that decodes the file - its exact codec (RFC 6381 string) **at its size, frame rate and bitrate** (MediaCapabilities: a phone whose decoder stops at 1080p says no to 4K) - gets the video **as it is** - full quality, almost no load on the board. Any other browser gets it **converted to H.264 by the VPU** (15 fps at 4K - what the VPU's H.264 encoder sustains - one viewer at a time; a seek by the same viewer replaces its stream, another viewer is told it is busy). If the device still fails to decode it, the player switches to the conversion at the same position and remembers that on the device; if it decodes neither, it says so and offers a 1080p H.264 copy (a background conversion the device can play itself - the board has no leak-free way to scale 4K live). Play / pause, a seek bar over the whole file, full screen; it shows which of the two it is. The play helper stops itself before the board runs out of memory. The CLI saves the same stream (`gallery stream`). |
| GAL-12 | **Download many at once.** Select takes one by one, a whole day or a whole month (every take of the group), and download their files in one go - all variants, or only the format the kind filter shows; files still being recorded or written are left out with a note. The browser gets **one ZIP** (stored, not recompressed; ZIP64 so files over 4 GB work; its size is known before the first byte, so the download shows real progress), streamed from the board without a temporary copy; the phone saves the files one after another into Downloads/Arstro; the CLI writes them into a folder or one ZIP. |
| GAL-13 | **Take notes and the editor's sheet.** A take can be marked **good**, given **notes**, and its scene / shot / take corrected afterwards, from every controller; the gallery shows the slate, the start timecode, good takes, markers and the A/V sync of each take. The takes - selected, a day or a month - are exported as a **CSV** (one row per file) that DaVinci Resolve imports as clip metadata (*File Name*, *Scene*, *Shot*, *Take*, *Good Take*, *Comments*, start / end / duration timecode, markers, A/V sync, dropped frames). |

## SCR – Remote screen (screen cast + control, like TeamViewer / UltraViewer)

| ID | Requirement |
|---|---|
| SCR-01 | **Remote screen.** The Pi's desktop (X display, with its mouse pointer) is streamed live as H.264 over WebSocket to any number of viewers from one shared encoder - smooth and live first: ≥ 20 fps with the CPU encoder (≥ 15 fps with the VPU, SCR-05) and < 0.5 s behind; a late viewer skips ahead. The capture runs in a helper process (ARC-06) and only while someone watches (stops ~5 s after the last viewer). |
| SCR-02 | **Control from the picture.** A viewer can take control: the mouse maps to the same point of the Pi's screen (move, left/middle/right press and release, drag, wheel), the keyboard types into the Pi (keys with modifiers, text), and local text can be sent as typing. *View only* switches control off. |
| SCR-03 | Quality presets (low ≤ 960 px wide at 15 fps, medium ≤ 1280 px at 30 fps, high native at 30 fps) are a shared, saved setting; the view shows the Pi's screen size and follows a change of it. When there is no desktop the view says why. |
| SCR-04 | All controllers: Web (Screen tab, mouse + keyboard), GUI (Remote › Screen, touch: tap = click, long press = right click, drag, two-finger scroll), CLI (`screen status / quality / encoder / bitrate / save`). |
| SCR-05 | **Encoder and bitrate.** The stream's H.264 encoder is a shared, saved setting: *auto* (default: the hardware VPU when the machine has one, else the CPU; a VPU that fails before its first frame falls back to the CPU), *vpu* or *cpu* (x264). The VPU costs about a third of the CPU's processor time (less heat and current) at a lower frame rate (~20 fps on the Orange Pi 5 Plus); the CPU encoder keeps 30 fps. The bitrate is a shared, saved setting too: *auto* (from the quality preset, scaled by the picture area) or 0.2 - 20 Mbit/s. The stream reports which encoder and bitrate it runs with; a change restarts a running capture. |

## ADM – System

| ID | Requirement |
|---|---|
| ADM-01 | Status: server version, Bluetooth adapter, pairing window, paired devices, connected controllers. |
| ADM-02 | Open or close the pairing window; forget a paired device. |
| ADM-03 | Show web access (URLs, port, password, open or password mode); change the password or the mode. |
| ADM-04 | System info: version, slot, port, board / model, kernel, code / config / log paths, the modules and their state. |
| ADM-05 | Restart the server, reboot or power off the machine from any controller (with a confirmation in the GUI / web, `--yes` in the CLI). |
| ADM-06 | **A/B slots.** Several instances install side by side (`install.sh --slot a\|b`): own code, config, logs, control socket, lock, port (8080 + index) and start-at-boot entry, so a new version is installed and tested in the slot not in use while the other keeps serving. Only one slot owns the app's Bluetooth link (`--activate` moves it and the default `arstro-remote` command); hardware changes one slot makes do not disturb the other (e.g. the HDMI EDID is re-written only when it changes). |
| ADM-08 | **Never deploy into the running session's slot.** `arstro-remote slots` names the slot hosting the calling process (by its process tree: a web-terminal shell is a child of that slot's daemon) and the idle slot to deploy into; `install.sh` refuses to (re)start or uninstall the slot hosting the shell that runs it (`--force-own-session` overrides). |
| ADM-07 | The web UI shows which slot it talks to (next to the host name) and keeps a separate login per port. |

## MOD – Feature modules

| ID | Requirement |
|---|---|
| MOD-01 | Features come in modules - **Monitor, System, Camera, Connection, Terminal, Screen, IO Control, Files, Apps, Internet access** - enabled per machine (`modules` in the slot's config, `install.sh --modules`; default all). The ops of a disabled module are refused with a clear message; hello reports each module's state (enabled / disabled / failed with the reason). |
| MOD-02 | The modules can be changed from any controller (System › Modules, `system modules --set`); the server restarts itself to apply it. |
| MOD-03 | Modules use standard Linux interfaces only; board-specific knowledge lives in a board module (`boards/`, e.g. the Orange Pi 5 Plus 40-pin header map) that only adds information. On an unknown board everything else works. |
| MOD-04 | The controllers group their pages by module and show only the enabled ones (web: sidebar / tab bar by group, sub-tabs inside a group). |

## CAM – Camera

| ID | Requirement |
|---|---|
| CAM-01 | The camera source is chosen from any controller: the board's HDMI input, a V4L2 (USB/UVC) camera, or the test pattern; the choice is saved and survives restarts. Live view, recording, gallery and conversions work the same for every source. |
| CAM-02 | V4L2 cameras - USB / UVC webcams and capture sticks - are found automatically through the V4L2 uAPI itself (no `v4l2-ctl` needed): capture devices with a usable pixel format, with their modes; the best mode up to 1080p is picked unless one is chosen. A USB camera this server cannot open is listed as unavailable with the reason (e.g. the `video` group). |
| CAM-04 | With a USB camera as the source, "from the camera" audio is that camera's own microphone (the sound card on the same USB device), named after it; without one there is no camera audio. Every controller shows which source is live by its name (web: the Recorder's header; CLI: `rec status` › Source), and the HDMI-only EDID setting is shown only for the HDMI input. |
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

## APP – Apps (native programs with a web UI)

| ID | Requirement |
|---|---|
| APP-01 | The server lists the installed NTWB apps: manifests in `<data dir>/ntwb/apps/<id>/ntwb.json` (XDG data dirs) and manifests registered by path (`apps register`). The list is rescanned on every request and whenever an app session is launched (installing or reinstalling needs no restart: a new process runs the app, its exec, web dir and API description as installed now); an app that cannot run is listed with the reason. |
| APP-02 | An app is started and stopped from every controller (web Apps page, CLI `apps launch/stop`); its run state (stopped / starting / running / failed with the reason) and the number of open clients are pushed live (topic `apps`). |
| APP-03 | The app's own web UI is served at `/apps/<id>/` (with `/ntwb/ntwb.js`, the app's icon and API description) and can be opened inside the launcher or in its own tab. |
| APP-04 | One app process - a **session** - serves every client that joins it; a client that joins late gets the session's full retained state at once. An app whose manifest says `single: false` may run several sessions side by side (at most `SESSIONS_PER_APP`), each its own process with its own state: a client opens the default session `main`, joins another by id, or starts a new one; clients of different sessions share nothing. |
| APP-05 | Each app has a log in the slot's state dir (its output, launches, exits, its `log` messages, protocol errors), readable from every controller. |
| APP-06 | Opening an app that is not running starts it. |
| APP-07 | Security: app pages and app WebSockets need the server's password; the app socket is private (0600); a launched app must present its one-time token; attaching without one needs the manifest's `attach` capability. |
| APP-08 | Every app method is reachable without a browser (`apps call`, `apps state`, `apps api`) by the same path a browser uses - so apps are scriptable and testable. |
| APP-10 | **Apps on the phone.** The app lists the NTWB apps of each slot as the server has them now - a newly installed app appears without an app update - and opens one's own web UI inside the app (logged in with that slot's password): one more view of the app's session, with its own layout (NTWB-11). |
| APP-09 | An app's sessions are visible and controllable from every controller: the Apps page lists each session with its run state and number of viewers and can open it, start a new one and stop one; the CLI has `apps sessions ID`, `apps launch ID --session new` and `--session` on `info`/`stop`/`call`/`state`; topic `apps` carries every app's sessions. |

## NTWB – The native-to-web bridge protocol

| ID | Requirement |
|---|---|
| NTWB-01 | **One definition.** The protocol (messages, fields, types, directions, framing, blob header, manifest, environment, timings) is defined once, in `server/arstro_remote/ntwb/spec.py`; the host validates with it and `docs/ntwb/api.json` + `docs/ntwb/API.md` are generated from it. `docs/ntwb/NTWB.md` explains it. Version 1.1.0. |
| NTWB-02 | An app declares itself with a manifest (id, name, version, exec, web dir, optional icon, API description, capabilities). |
| NTWB-03 | The host launches an app with `NTWB_SOCKET`, `NTWB_TOKEN`, `NTWB_APP_ID`, `NTWB_SESSION`, `NTWB_VERSION`, `NTWB_HOST`, `NTWB_DATA_DIR`; the app connects and says `hello`, the host answers `welcome` (naming the session). |
| NTWB-04 | Framing: app ↔ host `[type u8][length u32 BE][payload]` with JSON messages and binary blobs (`[u16 header length][JSON header][data]`); browser ↔ host the same messages over WebSocket (text = JSON, binary = blob). |
| NTWB-05 | Lifecycle: a launched app must say `hello` within 20 s; idle connections are pinged; `bye` asks an app to exit (SIGTERM, then SIGKILL after 5 s each); an exit is reported with its code. |
| NTWB-06 | Unknown input is rejected, never accepted quietly: an unknown message, field or wrong type, a missing field, a message in the wrong direction or a result for no call is dropped and answered with `error` (a bad browser `call` with `result` ok=false). |
| NTWB-07 | An app that ships an API description only receives calls and notifies for the methods it lists; the description is served to controllers. |
| NTWB-08 | Routing: `call` → `result` back to the calling client (the host rewrites call ids, so clients never collide), `notify` without reply, `event` to all clients or one, `state` retained per key and replayed, blobs to all or one - a blob marked `coalesce` (live previews) is replaced by a newer one of its stream while a slow client's queue holds it, every other blob is delivered. |
| NTWB-09 | SDKs speak exactly the defined protocol: the web SDK `ntwb.js`, the Python app SDK `arstro_remote.ntwb.app`, the C++ client arstro `core/Ntwb` (checked against the vendored `api.json`). |
| NTWB-10 | A test fails when the generated reference, the web SDK or the narrative drift from the definition. |
| NTWB-11 | **MVVM - the app is the model, every client is its own view.** An app's observable state leaves only as `state` (the model, as whole values) and `event`s (facts), its pixels as blobs; a client's view state (layout, open panels, scroll, zoom, a drag in flight) stays in that client and never travels as shared state. So any number of clients - each laid out for its own screen - show one session, and a change made in one reaches every other through the model. An app's web UI is a view and view-model built on that model, never a stream of the app's own window (a remote screen gives every client the same pixels, sized for no one). |
| NTWB-12 | Presence: a client learns which session it joined and how many clients share it (`ready.session`, `status.session` + `status.clients`) and is told again whenever that number changes; the web SDK exposes both. |

## RLY – Internet access through the device manager

The device manager (`relay/`, run as a Docker image on a public server) lists the machines that
advertise to it and lets a viewer reach any port of one of them, end-to-end encrypted with
the machine's access key. Design and security: `docs/relay/RELAY.md`; protocol:
`docs/relay/PROTOCOL.md`; for agents: `docs/relay/AGENT_API.md`.

| ID | Requirement |
|---|---|
| RLY-01 | **Advertise.** Module `relay` (Internet access): with a device manager URL and its join token set and Internet access on, the slot keeps one outbound WebSocket to the device manager (no inbound port), reconnects with back-off, and shows its state (off / connecting / online / error with the reason) live in topic `relay` on every controller. |
| RLY-02 | **Identity and access key.** On its first start the slot makes a device id, a secret that binds the id at the device manager, and a 256-bit **access key** (`ak1_…`), all in 0600 files in the slot's config dir. The key is shown by `install.sh --relay`, `relay key` and the Internet access page; it can be replaced, which closes every viewer. |
| RLY-03 | **End-to-end encryption.** Everything between a viewer and the machine is encrypted and authenticated with keys derived from the access key (HKDF-SHA256, AES-256-GCM, fresh nonces per channel; the machine proves the key first, then the viewer); the device manager only forwards ciphertext and never holds the key. One definition (`relay/e2e.py`) with test vectors that the browser implementation must match. |
| RLY-04 | **Any local port.** A viewer reaches any TCP port of the machine (`127.0.0.1:<port>`) as HTTP pages, WebSockets or raw TCP; an optional allow-list limits the ports; an authenticated viewer can list the listening ports. |
| RLY-05 | **Viewers are visible.** Every controller lists the open viewer channels (address, since, streams, bytes) and can disconnect them all; wrong keys are logged and refused for a minute after five in a row. |
| RLY-06 | **CLI.** `relay status / set / key / rotate / kick` manage it; `relay devices / ports / http / forward` reach other machines through a device manager (the standalone client). |
| RLY-07 | **Logging.** Tunnel up / down, viewer channels opened and closed (with address and bytes), wrong keys and setting changes (with who) are logged; the key, the join token and the secret never are. |
| RLY-08 | **Settings from every controller.** Device manager URL, join token (write-only), name, on / off and the port allow-list are set from the web page, the CLI (`relay set`) and `install.sh --relay URL --relay-token T [--relay-name N]`. |
| RLY-20 | **Docker image.** The device manager is one image (any architecture Python runs on, e.g. Oracle's arm64), runs with no configuration as a non-root user with one data volume and one port, serves under a base path (default `/arstro/internal/devices`) and works behind an HTTPS reverse proxy (a compose file with Caddy gives automatic HTTPS). `relay/package.sh` makes a kit for someone who only has Docker: the image for amd64 and arm64, an image-only compose file with Caddy, checksums and `run.sh` (load the image for this CPU, start, show the join token); `PROXY=own` leaves ports 80/443 to an existing proxy such as nginx and only opens the device manager on `RELAY_LISTEN`. |
| RLY-21 | **Device list.** It lists the machines that advertise to it (name, host name, online / offline, last seen, viewers); a machine needs the join token, and a device id stays bound to the machine that registered it first. |
| RLY-22 | **Browser viewer.** Pick a device, enter its access key (kept in the browser as a non-extractable key, remembered or for 12 h, can be forgotten), pick or type a port: the page of that port opens through the encrypted channel (service worker for HTTP, WebSocket shim, cookie jar), so web apps like Arstro Remote work fully. |
| RLY-23 | **Viewer password.** An optional password (`RELAY_PASSWORD`) protects the list and the connections. |
| RLY-24 | **Hardening.** Message size, channels per device and new connections per address are limited; strict security headers; forwarded content is never read or logged. |
| RLY-25 | **Admin.** A CLI inside the container lists devices, forgets one and shows or replaces the join token; a running server picks changes up within 5 s. |
| RLY-27 | **Through any web proxy.** When WebSockets cannot pass (e.g. an nginx without `Upgrade` headers), devices, the client and the browser viewer fall back by themselves to an HTTP transport (long polling: a waiting GET down, small POST batches up, sequence numbers for order and retries) that carries exactly the same messages; plain `proxy_pass` is enough. |
| RLY-28 | **Transfers panel.** In the browser viewer every download and every upload of at least 256 KiB gets a row in one Transfers list on the device page: name, direction, progress, size, current speed, time left, and a cancel button while it runs; finished rows show size, time and average speed, failed ones the reason; several transfers at once are listed together. Upload progress is what the service worker has sent to the device (the page's own upload progress is meaningless there). |
| RLY-26 | **For agents.** A documented API (`docs/relay/AGENT_API.md`) and a standalone Python client, downloadable from the device manager, list devices, check a key, list ports, make HTTP requests and forward a device port to a local one. |

## PHN – Share this phone (Android)

Arstro Remote Client, an Android app of its own (`client_app/`, apart from the Pi's remote control), makes the phone itself a device of the device manager (`docs/phone/PHONE.md`).

| ID | Requirement |
|---|---|
| PHN-01 | **Advertise.** A foreground service advertises the phone to the device manager with the relay protocol (`docs/relay/PROTOCOL.md`: WebSocket or HTTP pipes, end-to-end encryption with the phone's own access key, its own device id and secret), starts at boot, stays reachable while the phone sleeps (a setting, on by default), reconnects at once when the network changes and otherwise with back-off, and shows its state in the app; nothing listens on a local port - the channel's streams reach an in-process router. |
| PHN-02 | **Switches and first run.** On first run a wizard explains every feature and asks for exactly the permissions and special accesses of the features the user turns on; every feature has an on / off switch in the app, shows what it still needs, and can be turned off at any time. A feature that is off or not ready is refused remotely with the reason; the web shows the switches read-only. |
| PHN-03 | **Status.** Battery (level, charging, temperature), storage, memory, network, signal, screen on / locked - live. |
| PHN-04 | **Screen.** Live view of the screen (H.264, encoded only while someone watches, the capture session kept between viewers; opening it turns a dark screen on and it stays on while watched; a screen turned off on the phone is shown as such, with Wake) and remote control: tap, long press, swipe, scroll, typing, Back / Home / Recents / notifications / quick settings / lock / power menu / wake. |
| PHN-05 | **Camera.** Live view of the front or back camera, torch, a photo now, recording a video into the phone's Movies. |
| PHN-06 | **Internet calls.** A voice call between the browser and the person holding the phone: the phone rings full screen (also locked), Answer / Decline, two-way audio with echo cancellation; the phone's camera can be watched alongside. |
| PHN-07 | **Listen.** The phone's microphone, live. |
| PHN-08 | **Messages.** SMS threads and messages, send an SMS, new messages pushed live. |
| PHN-09 | **Contacts and call log.** Search contacts, the recent calls. |
| PHN-10 | **Find my phone.** Location now and a 7-day history (a fix every 15 min), ring loudly also on silent / Do Not Disturb, lost mode (lock now and show a message and a number over the lock screen); works after a reboot without opening the app. |
| PHN-11 | **Files.** Browse the phone's storage, download (with Range), upload any size, delete, new folder, rename. |
| PHN-12 | **Notifications.** The current notifications live, dismiss, reply where the app allows it. |
| PHN-13 | **Clipboard.** Put text on the phone's clipboard; read it while the app is in the foreground (Android does not allow it otherwise - said so). |
| PHN-14 | **Apps.** List the installed apps with icons, open one. |
| PHN-15 | **Audio and tools.** Volume per stream, media keys, speak a text, vibrate, torch, brightness, open a URL. |
| PHN-16 | **Shell.** Run a command in the app's own sandbox (no root). |
| PHN-17 | **Web UI.** The phone's own web UI covers every feature, opens from the device manager in any browser, works over the HTTP transport, at 360 px and on a desktop. |
| PHN-18 | **For agents.** Every feature is an op or a route (`PHONE.md`), reachable with the relay client (`http`, `forward`). |
| PHN-19 | **Security and privacy.** Reachable only through the encrypted channel with the phone's access key; the access key can be shown and replaced in the app; Android's own indicators (screen sharing, camera, microphone) stay visible; logs never hold keys, message, notification or clipboard texts. |

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
| SET-03 | `scripts/build_apk.sh` builds the signed release APK (creates a signing key if none exists) into `release/` - Arstro Remote, or with `--client` Arstro Remote Client, signed with the same key. |
| SET-04 | The Pi offers the latest Android app at `http://<pi>:8080/app.apk` (uploaded by `setup_pi.sh` from `release/`), so a phone on the same network installs or updates it from its browser without a cable; the web UI links to it and the CLI (`web`) prints the link. |
| SET-05 | **One installer** (`install.sh` at the repo root, same as `server/install.sh`) sets the server up on any Debian/Ubuntu-family machine: packages for the chosen modules (sudo, only what is missing), device permissions, the slot's code / config / password / start at boot, then starts it and checks that it answers. `--uninstall` removes a slot. |
| SET-06 | `scripts/setup_pi.sh --slot a\|b` deploys from the dev PC into a slot (device permissions through the Pi's sudo password). |
| SET-07 | The app can be developed and built on the board itself: `scripts/setup_android_dev.sh` installs Flutter, the Android SDK and JDK 17 for the user (sudo only for packages). On ARM64 Linux, the build tools Google publishes for x86-64 only (`gen_snapshot`, `aapt2`, the NDK's `llvm-strip`) run under qemu-user through wrappers that `build_apk.sh` refreshes before every build, Flutter's empty CMake project (it only fetches the NDK; CMake and the NDK's compilers do not run there) is left out, and Gradle's memory is capped on machines with less than 12 GB. The APK is the same size as one built on a PC (stripped native libraries). |
