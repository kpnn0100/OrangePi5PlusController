# Parity: every feature from every controller (ARC-04)

GUI = Android app, CLI = `arstro-remote` (on the Pi, or `--url` from anywhere),
Web = the web UI on `http://<pi>:8080/`. All three call the same ops (`docs/protocol.md`)
and all three are updated live by the same state events (ARC-03).
**Keep this table in sync with every change** (the implement skill checks it).

| Feature | Req | GUI (app) | CLI | Web |
|---|---|---|---|---|
| Live system stats | STAT-01/02 | Dashboard | `stats`, `stats --watch` | Monitor |
| Wi-Fi status, scan, connect (saved/new/hidden) | WIFI-01..03 | Wi-Fi tab | `wifi status / scan / saved / connect SSID [--ask] [--hidden]` | Wi-Fi |
| Wi-Fi disconnect, forget, radio | WIFI-04..06 | Wi-Fi tab | `wifi disconnect / forget NAME / radio on\|off` | Wi-Fi |
| Shared shells: list, open, attach, close | TERM-01..04 | Terminal tab | `term list / open [--ephemeral] / attach ID / close ID`, `term run "cmd"` | Terminal |
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
| Delete one format / whole take | GAL-06 | Take sheet | `gallery delete FILE`, `gallery delete-take TAKE [--yes]` | Take dialog |
| Server + Bluetooth status | ADM-01 | Settings › Bluetooth, About | `status` | System |
| Pairing window, forget a phone | ADM-02 | Settings › Bluetooth | `pair [SECONDS]`, `unpair ADDRESS` | System |
| Web access: URLs, password, open mode | ADM-03 | Settings › Web access | `web [--show] [--set-password] [--rotate] [--open \| --require-password]` | System |
| Connected controllers | CON-05 | Settings › Connected controllers | `status`, `watch controllers` | System |
| Any op (tools, scripts) | ARC-02 | - | `call OP '{json}'` | `POST /api/op/OP` |

¹ A terminal cannot show video: the CLI saves the live H.264 stream to a file (play it with
any player, e.g. `ffplay`). ² Same reason: the CLI lists and downloads, a player shows it.

## Exceptions (and why)

| Where | What | Why |
|---|---|---|
| CLI | No picture (preview, thumbnails, playback) | A text terminal cannot show video; the CLI saves streams/files instead. |
| GUI | Media needs Wi-Fi | Bluetooth is too slow for video; the app explains when the phone cannot reach the Pi over Wi-Fi (CON-02). Control keeps working over Bluetooth. |
| Web | No Bluetooth pairing of the browser itself | The browser connects over the network; it can still open/close the pairing window for phones. |
