# Parity: every feature from every controller (ARC-04)

GUI = Android app, CLI = `arstro-remote` (on the Pi, or `--url` from anywhere),
Web = the web UI on `http://<pi>:<port>/` (slot A 8080, B 8081), grouped by module
(Monitor, Camera, Screen, Terminal, Connection, IO Control, Files, Apps, Internet access, System). All three call the same ops (`docs/protocol.md`)
and all three are updated live by the same state events (ARC-03).
**Keep this table in sync with every change** (the implement skill checks it).

| Feature | Req | GUI (app) | CLI | Web |
|---|---|---|---|---|
| Live system stats | STAT-01/02 | Dashboard | `stats`, `stats --watch` | Monitor |
| Wi-Fi status, scan, connect (saved/new/hidden) | WIFI-01..03 | Wi-Fi tab | `wifi status / scan / saved / connect SSID [--ask] [--hidden]` | Wi-Fi |
| Wi-Fi disconnect, forget, radio | WIFI-04..06 | Wi-Fi tab | `wifi disconnect / forget NAME / radio on\|off` | Wi-Fi |
| Shared shells: list, open, attach, close | TERM-01..04 | Terminal tab | `term list / open [--ephemeral] / attach ID / close ID`, `term run "cmd"` | Terminal |
| Full-screen shell (F11) | TERM-05, TERM-07 | Terminal › ⛶ (immersive), the tabs stay; leave: the bar's button, Back, F11 | - ⁴ | Terminal › Full screen, F11; leave: F11 or the strip's button |
| Tabs and split panes (Tilix-like), drag and drop to move a shell | TERM-08, TERM-09 | tabs: + / switch / close, also in full screen; splits: - (one shell fits a phone) | - ⁴ (a terminal multiplexer such as tmux) | Terminal: + / ◫ / ⊟ in the tab strip and each pane's bar; Ctrl+Alt+R/D, Ctrl+Shift+T/W/X, Alt+arrows; dividers drag; drag a pane's bar onto a pane / tab / the strip, drag tabs to reorder |
| Shells of every A/B slot | TERM-06 | Terminal › *Slot A · Bluetooth* / *Slot B · Wi-Fi* | `arstro-remote-a term …` / `arstro-remote-b term …` (or `--slot a\|b`) | each slot's own page (`:8080`, `:8081`) |
| Copy / paste in a shell | TERM-07 | long-press + drag, Copy / Paste keys, Ctrl+Shift+C / Ctrl+Shift+V | the terminal the CLI runs in | the browser's selection and clipboard |
| Remote screen: watch the Pi desktop live, control it from the picture | SCR-01..04 | Remote › Screen (tap = click, long press = right click, drag, two-finger scroll; keyboard panel types) | `screen status`, `screen quality low\|medium\|high`, `screen save FILE [--seconds N]` ¹ | Screen tab (mouse, wheel, keyboard, Keys, Type text, full screen) |
| Remote screen: encoder (auto / VPU / CPU) and bitrate | SCR-05 | ³ | `screen encoder auto\|vpu\|cpu`, `screen bitrate 2.5M\|800k\|auto`, `screen status` | Screen tab › Stream (encoder, bitrate, what runs now) |
| Mouse + keyboard | INP-01..04 | Remote tab (touchpad, buttons, drag lock, scroll strip, keys) | `input move X Y [--absolute] / click / down / up [BUTTON] / scroll DY [DX] / key K [--mods ctrl,alt] / type TEXT / pointer` | Remote (pad, buttons, keys, capture mode) |
| Recorder status + signal diagnosis | REC-01 | Recorder › Live | `rec status`, `watch recorder` | Recorder |
| Live preview | REC-02 | Recorder › Live (native H.264 decoder) | `rec preview FILE [--seconds N]` saves the stream ¹ | Recorder (WebCodecs / MSE) |
| Preview quality | REC-02 | Live (360p/720p/1080p) | `rec quality low\|medium\|high` | Recorder |
| Start / stop recording | REC-03 | Record button | `rec start [--duration S]`, `rec stop` | Record button |
| Recording settings (extra copies: H.264 / H.265 / FFV1, each a collapsible sub-section) | REC-04 | Settings sheet | `rec settings`, `rec set key=value ...` (`raw.h264=true raw.h264_scale=720 raw.h264_quality=high`) | Settings sheet |
| EDID | REC-04 | Settings sheet | `rec edid 4k60\|4k30\|1080p\|keep` | Settings sheet |
| Test source | REC-08 | Settings sheet › Source | `rec source --test WxH@FPS \| --hdmi` | Settings sheet › Source |
| Audio for film: MOV/MKV with PCM, the audio input (HDMI or an audio interface), A/V sync per take | REC-09 | Settings sheet › H.265 › Container; › Audio › Input; Gallery › take sheet shows the A/V sync | `rec set h265.container=mov`, `rec inputs`, `rec input alsa:<card>`; sync in `gallery show` | Settings sheet › H.265 › Container; › Audio › Input; Live › Last recording and Gallery show the A/V sync when off by more than a frame |
| Audio meters | REC-10 | Live › meters under the picture | - ¹ | Live › meters under the picture (peak hold, red ≥ -1 dBFS) |
| Timecode (time of day) in every file, clock sync shown | REC-11 | Live › start TC while recording (a warning when the clock is not synced); Gallery › TC on each take | `rec status` (Take line: start TC, clock sync); `gallery show` | Live › TC badge while recording, record panel; Gallery › TC on each take |
| Disk speed test, slow-disk guard, time left | REC-12 | Live › Storage card (time left, disk speed, Test disk); *Record anyway?* on a too-slow disk; Settings sheet › Storage guard | `rec disktest`, `rec status` (time left, disk speed, warning), `rec start --force`, `rec set storage_guard=false` | Live › Status › Time left, disk speed, Test disk; *Record anyway?* on a too-slow disk; Settings sheet › Storage guard |
| On-set monitoring: tally, guides, safe areas, centre, zebras, false colour, histogram / waveform, LUT | REC-13 | Live (tally frame, overlays, scope); Settings sheet › Monitoring (pick or delete a LUT - adding a .cube is web / CLI only: the app has no file picker) | `rec set monitor.guides=2.39 monitor.zebra=true monitor.scope=waveform ...`, `rec luts`, `rec lut-add FILE`, `rec lut-rm cube:NAME` | Live (overlay on the picture, scope under it); Settings sheet › Monitoring (incl. Add .cube…) |
| Focus loupe: a magnifying glass over the live picture, ratio in the settings, per viewer | REC-14 | Live › tap the picture: a circle magnifies that point, floating above the finger (drag to move, tap elsewhere to move it there; × or Back closes), also in full screen; Settings sheet › Monitoring › Focus loupe | `rec set monitor.zoom=3` (the ratio) - ¹ | Live › click the picture: a circle magnifies that point (drag it to move, click elsewhere to move it there; × or Esc closes; on touch it floats above the finger); Settings sheet › Monitoring › Focus loupe |
| Slate (scene / shot / take, counts up) and markers | REC-15 | Settings sheet › Slate; Live › *Set marker* while recording | `rec slate [--scene S --shot H --take N --[no-]auto]`, `rec mark [label]` | Settings sheet › Slate; Live › record panel › Marker (Shift-click / long-press: with a label) |
| Record button and tally light on GPIO | REC-16 | Settings sheet › Record button & tally (header-pin picker) | `rec set gpio.button=gpiochipN:L gpio.tally=gpiochipN:L`, `rec status` (GPIO line) | Settings sheet › Record button & tally (header-pin picker when IO Control is on) |
| Good take, notes, slate fixed afterwards; the editor's CSV | GAL-13 | Gallery › take sheet (Good, Scene/Shot/Take/Notes, markers); Select › CSV (saved to Downloads/Arstro) | `gallery note TAKE [--good] [--notes T] [--scene S --shot H --take-no N]`, `gallery csv [TAKE...] [--day D \| --month M] [--out FILE]` | Gallery › take sheet (Good, Scene/Shot/Take/Notes, markers); selection bar › CSV |
| Gallery list + filter, grouped by month and day | GAL-01 | Recorder › Gallery (month and day headings) | `gallery list [--kind K]` (month / day headings), `gallery show TAKE` | Gallery (month and day headings) |
| Thumbnails | GAL-02 | Gallery | - ¹ | Gallery |
| Play | GAL-03 | Take sheet (video player) | - ² (download and play locally) | Take dialog |
| Download | GAL-03 | Take › Download to phone (Downloads/Arstro) | `gallery download FILE [--out PATH]` | Take › download |
| Download many at once (takes, a day, a month) | GAL-12 | Gallery › Select (or long-press a take; a heading's box takes the day / month) › Download - files one after another into Downloads/Arstro | `gallery download TAKE... \| --day D \| --month M [--kind K] [--out DIR \| --zip [FILE]]` | Gallery › Select (or a heading's box) › Download ZIP - one streamed ZIP |
| Convert (H.264 share + downscale, H.265 VPU/x265, FFV1 CPU/GPU) | GAL-04 | Take › Convert | `gallery targets`, `gallery convert FILE --to TARGET [--scale 720] [--quality Q] [--bitrate MBPS] [--wait]` | Take › Convert |
| Jobs: progress, cancel, clear | GAL-05 | Gallery › Conversions | `jobs`, `jobs cancel ID`, `jobs clear` | Gallery › Conversions |
| Verified lossless FFV1 replaces the RAW (setting, per conversion, on demand) | GAL-08 | Settings sheet › *Delete RAW after a verified copy*; Convert switch; FFV1 › *Check against the RAW…* | `rec set raw.ffv1_replace_raw=true`, `gallery convert … --keep-raw`, `gallery verify FFV1 [--delete-raw] [--wait]` | Settings sheet; Convert switch; FFV1 › check button |
| Interrupted RAW repaired; conversions resumed and retried (state, attempt, note) | GAL-09, GAL-10 | automatic; Gallery shows the job state ³ | automatic; `jobs` shows `retrying`, the attempt and the note; `gallery list` / `gallery show` the item note | automatic; Gallery › Conversions shows *Retry n/3* and the note; the take shows the repair note |
| Play H.265 (as it is, or converted to H.264 by the VPU) with seeking | GAL-11 | ³ (the app plays H.264 copies; H.265 streaming pending) | `gallery stream FILE OUT.mp4 [--mode copy\|h264] [--start S]` ¹ | Take sheet: player with *H.265 original* / *H.264 · 15 fps*, seek bar, full screen |
| Delete one format / whole take | GAL-06 | Take sheet | `gallery delete FILE`, `gallery delete-take TAKE [--yes]` | Take dialog |
| Server + Bluetooth status | ADM-01 | Settings › Bluetooth, About | `status` | System |
| Pairing window, forget a phone | ADM-02 | Settings › Bluetooth | `pair [SECONDS]`, `unpair ADDRESS` | System |
| Web access: URLs, password, open mode | ADM-03 | Settings › Web access | `web [--show] [--set-password] [--rotate] [--open \| --require-password]` | System |
| Connected controllers | CON-05 | Settings › Connected controllers | `status`, `watch controllers` | System |
| Install / update the app without a cable | SET-04 | (open `http://<pi>:8080/app.apk` in the phone browser) | `web` prints the link | Login page and System › Android app |
| Camera source: HDMI input, USB (V4L2) camera, test pattern | CAM-01/02 | Settings sheet › Source (HDMI / test) ³ | `camera sources`, `camera select hdmi\|test\|v4l2:/dev/videoN [--mode "MJPG 1280x720@30"]` | Camera › Live › Settings › Source |
| Which source is live (by name); a USB camera's own microphone as "from the camera" audio | CAM-04 | ³ | `rec status` › Source, Audio in | Camera › Live: the header, the audio meter's title; Settings › Audio input |
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
| Apps: list, start, stop, log, register | APP-01/02/05, APP-10 | Apps (top bar): list per slot, open ³ | `apps list / launch ID / stop ID / log ID / register PATH / unregister PATH` | Apps |
| Use an app (its own web UI) | APP-03/04/06, APP-10 | Apps (top bar) › an app (in-app web view, per slot) | `apps call ID METHOD '{json}'`, `apps state ID [KEY]`, `apps api ID` | Apps › Open (inside the shell) or `/apps/<id>/` |
| App sessions: list, start another, join, stop one; who is viewing | APP-04/09, NTWB-12 | ³ | `apps sessions ID`, `apps launch ID --session new`, `--session S` on `info/stop/call/state` | Apps › the card's sessions (Open / New session / Stop), or `/apps/<id>/?session=S` |
| The NTWB protocol reference | NTWB-01 | - | `apps spec [--json]` | `docs/ntwb/API.md` |
| Internet access: device manager, on / off, name, port allow-list, status | RLY-01, RLY-08 | ³ | `relay status`, `relay set --url U --token T [--name N] [--enable\|--disable] [--ports 8080,22\|any]`, `install.sh --relay U --relay-token T` | Internet access |
| Access key: show, replace | RLY-02 | ³ | `relay key`, `relay rotate --yes` | Internet access › Access key |
| Who is connected through the device manager, disconnect them | RLY-05 | ³ | `relay status`, `relay kick` | Internet access › Viewers |
| Reach another machine through a device manager (list, ports, HTTP, port forward) | RLY-06, RLY-26 | - (the device manager's page works in the phone's browser) | `relay devices / check / ports / http / forward` (or the standalone client) | the device manager's page `https://<server>/arstro/internal/devices/` |
| Any op (tools, scripts) | ARC-02 | - | `call OP '{json}'` | `POST /api/op/OP` |

¹ A terminal cannot show video: the CLI saves the live H.264 stream to a file (play it with
any player, e.g. `ffplay`). ² Same reason: the CLI lists and downloads, a player shows it.
⁴ The CLI runs in a terminal already: `term attach ID` fills whatever terminal window it is in.
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
