# Parity: every feature from every controller (ARC-04)

GUI = Android app, CLI = `arstro-remote` (on the Pi, or `--url` from anywhere),
Web = the web UI on `http://<pi>:<port>/` (slot A 8080, B 8081), grouped by module
(Monitor, Camera, Screen, Terminal, Connection, IO Control, Files, System). All three call the same ops (`docs/protocol.md`)
and all three are updated live by the same state events (ARC-03).
**Keep this table in sync with every change** (the implement skill checks it).

| Feature | Req | GUI (app) | CLI | Web |
|---|---|---|---|---|
| Live system stats | STAT-01/02 | Dashboard | `stats`, `stats --watch` | Monitor |
| Wi-Fi status, scan, connect (saved/new/hidden) | WIFI-01..03 | Wi-Fi tab | `wifi status / scan / saved / connect SSID [--ask] [--hidden]` | Wi-Fi |
| Wi-Fi disconnect, forget, radio | WIFI-04..06 | Wi-Fi tab | `wifi disconnect / forget NAME / radio on\|off` | Wi-Fi |
| Shared shells: list, open, attach, close | TERM-01..04 | Terminal tab | `term list / open [--ephemeral] / attach ID / close ID`, `term run "cmd"` | Terminal |
| Remote screen: watch the Pi desktop live, control it from the picture | SCR-01..04 | Remote › Screen (tap = click, long press = right click, drag, two-finger scroll; keyboard panel types) | `screen status`, `screen quality low\|medium\|high`, `screen save FILE [--seconds N]` ¹ | Screen tab (mouse, wheel, keyboard, Keys, Type text, full screen) |
| Mouse + keyboard | INP-01..04 | Remote tab (touchpad, buttons, drag lock, scroll strip, keys) | `input move X Y [--absolute] / click / down / up [BUTTON] / scroll DY [DX] / key K [--mods ctrl,alt] / type TEXT / pointer` | Remote (pad, buttons, keys, capture mode) |
| Recorder status + signal diagnosis | REC-01 | Recorder › Live | `rec status`, `watch recorder` | Recorder |
| Live preview | REC-02 | Recorder › Live (native H.264 decoder) | `rec preview FILE [--seconds N]` saves the stream ¹ | Recorder (WebCodecs / MSE) |
| Preview quality | REC-02 | Live (360p/720p/1080p) | `rec quality low\|medium\|high` | Recorder |
| Start / stop recording | REC-03 | Record button | `rec start [--duration S]`, `rec stop` | Record button |
| Recording settings | REC-04 | Settings sheet | `rec settings`, `rec set key=value ...` | Settings sheet |
| EDID | REC-04 | Settings sheet | `rec edid 4k60\|4k30\|1080p\|keep` | Settings sheet |
| Test source | REC-08 | Settings sheet › Source | `rec source --test WxH@FPS \| --hdmi` | Settings sheet › Source |
| Gallery list + filter | GAL-01 | Recorder › Gallery | `gallery list [--kind K]`, `gallery show TAKE` | Gallery |
| Thumbnails | GAL-02 | Gallery | - ² | Gallery |
| Play | GAL-03 | Take sheet (video player) | - ² (download and play locally) | Take dialog |
| Download | GAL-03 | Take › Download to phone (Downloads/Arstro) | `gallery download FILE [--out PATH]` | Take › download |
| Convert (H.264 share + downscale, H.265 VPU/x265, FFV1 CPU/GPU) | GAL-04 | Take › Convert | `gallery targets`, `gallery convert FILE --to TARGET [--scale 720] [--quality Q] [--bitrate MBPS] [--wait]` | Take › Convert |
| Jobs: progress, cancel, clear | GAL-05 | Gallery › Conversions | `jobs`, `jobs cancel ID`, `jobs clear` | Gallery › Conversions |
| Verified lossless FFV1 replaces the RAW (setting, per conversion, on demand) | GAL-08 | Settings sheet › *Delete RAW after a verified copy*; Convert switch; FFV1 › *Check against the RAW…* | `rec set raw.ffv1_replace_raw=true`, `gallery convert … --keep-raw`, `gallery verify FFV1 [--delete-raw] [--wait]` | Settings sheet; Convert switch; FFV1 › check button |
| Delete one format / whole take | GAL-06 | Take sheet | `gallery delete FILE`, `gallery delete-take TAKE [--yes]` | Take dialog |
| Server + Bluetooth status | ADM-01 | Settings › Bluetooth, About | `status` | System |
| Pairing window, forget a phone | ADM-02 | Settings › Bluetooth | `pair [SECONDS]`, `unpair ADDRESS` | System |
| Web access: URLs, password, open mode | ADM-03 | Settings › Web access | `web [--show] [--set-password] [--rotate] [--open \| --require-password]` | System |
| Connected controllers | CON-05 | Settings › Connected controllers | `status`, `watch controllers` | System |
| Install / update the app without a cable | SET-04 | (open `http://<pi>:8080/app.apk` in the phone browser) | `web` prints the link | Login page and System › Android app |
| Camera source: HDMI input, USB (V4L2) camera, test pattern | CAM-01/02 | Settings sheet › Source (HDMI / test) ³ | `camera sources`, `camera select hdmi\|test\|v4l2:/dev/videoN [--mode "MJPG 1280x720@30"]` | Camera › Live › Settings › Source |
| Network interfaces, Ethernet connect / disconnect | NET-01 | ³ | `net devices` | Connection › Network |
| Connection profiles: edit IPv4 (DHCP / static / DNS / MTU), activate, delete, add Ethernet | NET-02/03 | ³ | `net connections / show UUID / set UUID --method manual --addresses A/24 --gateway G --dns D / up / down / delete UUID`, `net add-ethernet IFACE ...` | Connection › Network (tap a connection) |
| Bluetooth devices: scan, pair, connect, forget; adapter power | NET-04/05 | ³ | `bt status / power on\|off / scan / devices / pair / connect / disconnect / trust / remove ADDR` | Connection › Bluetooth |
| GPIO: chips, lines, request (bias, drive, edge, debounce), set / get / release, edge events | IO-02/03 | ³ | `io gpio chips / lines CHIP / set CHIP LINE 0\|1 / get / watch [--edge] / release` | IO Control › Pins (header + all lines) |
| Pin header of the board | IO-10 | ³ | `io header` | IO Control › Pins |
| I2C: buses, scan, transfer, register dump | IO-04 | ³ | `io i2c buses / scan BUS / read BUS ADDR [REG] --count N / write BUS ADDR HEX / dump BUS ADDR` | IO Control › I2C |
| SPI transfer | IO-05 | ³ | `io spi devices / xfer DEV HEX [--mode --speed --bits]` | IO Control › SPI |
| UART consoles (shared), hex send, break, modem lines, settings | IO-09 | ³ | `io uart ports / open PORT [--baud ...]` (interactive, Ctrl+] detaches), `io uart send TERM --hex H \| --text T`, `term attach ID` | IO Control › UART |
| PWM, LEDs, ADC | IO-06..08 | ³ | `io pwm list / set CHIP [CH] --freq HZ --duty PCT on\|off`, `io led [NAME --brightness N --trigger T]`, `io adc` | IO Control › PWM & LEDs, ADC |
| IO access problems + fixes | IO-01 | ³ | `io info` | a note on each IO page |
| Files: browse, upload, download, rename, delete, new folder, view / edit text | FILE-01..04 | ³ | `files ls [PATH] / get PATH / put FILE DIR [--force] / mv / rm [-r] / mkdir / cat / roots` | Files (drag and drop upload) |
| Server logs: tail, follow, filter, level, marker, download | LOG-01..03 | ³ | `log [-n N] [--grep T] [-f] [--file NAME] / log --list / log --level debug [--save]` | System › Logs |
| System info (slot, board, paths, modules) | ADM-04 | ³ | `system info` | System › Server, Modules |
| Modules on / off | MOD-01/02 | ³ | `system modules [--set monitor,system,...]` | System › Modules |
| Restart server, reboot, power off | ADM-05 | ³ | `system restart / reboot --yes / poweroff --yes` | System › Modules card |
| Which slot, A/B side by side | ADM-06/07 | ³ | `--slot a\|b` or `arstro-remote-a` / `arstro-remote-b`; `system info` | host line shows the slot; own login per port |
| Which slot hosts this session, which is idle | ADM-08 | - (a phone hosts no shell) | `slots [--current\|--idle]` | - (the browser is not a process on the board) |
| Apps: list, start, stop, log, register | APP-01/02/05 | ³ | `apps list / launch ID / stop ID / log ID / register PATH / unregister PATH` | Apps |
| Use an app (its own web UI) | APP-03/04/06 | ³ | `apps call ID METHOD '{json}'`, `apps state ID [KEY]`, `apps api ID` | Apps › Open (inside the shell) or `/apps/<id>/` |
| App sessions: list, start another, join, stop one; who is viewing | APP-04/09, NTWB-12 | ³ | `apps sessions ID`, `apps launch ID --session new`, `--session S` on `info/stop/call/state` | Apps › the card's sessions (Open / New session / Stop), or `/apps/<id>/?session=S` |
| The NTWB protocol reference | NTWB-01 | - | `apps spec [--json]` | `docs/ntwb/API.md` |
| Any op (tools, scripts) | ARC-02 | - | `call OP '{json}'` | `POST /api/op/OP` |

¹ A terminal cannot show video: the CLI saves the live H.264 stream to a file (play it with
any player, e.g. `ffplay`). ² Same reason: the CLI lists and downloads, a player shows it.
³ **Pending in the app** (see *Exceptions*): the Android app is built on the dev PC and has not
been updated for the 2.1 modules yet; until then the phone uses the web UI (same features,
works on a phone screen).

## Exceptions (and why)

| Where | What | Why |
|---|---|---|
| CLI | No picture (preview, thumbnails, playback) | A text terminal cannot show video; the CLI saves streams/files instead. |
| GUI | Media needs Wi-Fi | Bluetooth is too slow for video; the app explains when the phone cannot reach the Pi over Wi-Fi (CON-02). Control keeps working over Bluetooth. |
| GUI | Connection (Ethernet / profiles / Bluetooth devices), IO Control, Files, Logs, modules, system info, restart / reboot, V4L2 camera choice | Added to the server, CLI and web in 2.1; the app update (built on the dev PC with Flutter) is pending. The web UI covers them on a phone meanwhile. |
| Web | No Bluetooth pairing of the browser itself | The browser connects over the network; it can still open/close the pairing window for phones. |
