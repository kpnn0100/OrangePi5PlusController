# OrangePi5PlusController

Control an Orange Pi 5 Plus from an **Android app**, a **web page** or the **command line**:
system monitor, Wi-Fi, shared terminals, mouse & keyboard, and an **HDMI-RX recorder** with a
live H.264 preview, recording (H.265 or RAW) and a gallery with conversion.

One server runs on the Pi. The three controllers use the same protocol and stay in sync: start
a recording from the CLI and the record button turns red in the app and on the web page.

| | Android app | Web UI | CLI |
|---|---|---|---|
| Reaches the Pi over | Bluetooth (control) + Wi-Fi (video) | the network, `http://<pi>:8080/` | the Pi itself, or `--url` from anywhere |
| Sign-in | pairing (Bluetooth) | password, once per browser | none on the Pi / password remotely |

## Quick start

On a Linux/macOS/WSL machine with this repository:

```bash
cp local.env.example local.env        # optional: your Pi address, phone serial
./setup_all.sh orangepi@<pi-address> --web-password --install
```

This checks your tools, logs in to the Pi once with its password (then installs an SSH key),
installs packages and the server, sets the web password, builds the signed APK into `release/`
and installs it on the phone attached with USB debugging. Steps can be skipped
(`--no-pi`, `--no-apk`); `./setup_all.sh --help` lists everything.

Only parts of it:

```bash
scripts/setup_pi.sh orangepi@<pi-address>     # install / update the Pi side (also --autologin, --reboot, --test)
./build_apk.sh [--install SERIAL]             # build release/arstro-remote-vX.Y.Z.apk
```

The server starts with the Pi's desktop session (auto-login) and restarts itself if it stops.
On first use of the recorder, run `server/scripts/setup_hdmirx.sh` on the Pi if it has no HDMI RX
device yet (needs sudo and a reboot).

## Using it

**App** - open Arstro Remote, tap *Scan*, pick `Arstro-<hostname>` (new phones can pair during the
first 10 minutes after the Pi boots, or after `arstro-remote pair`). Tabs: Dashboard, **Recorder**
(Live + Gallery), Wi-Fi, Terminal, Remote. The live picture, thumbnails and playback come over
Wi-Fi: keep the phone on the same network as the Pi (the app tells you when it is not).
Settings holds web access (password), the pairing window and the connected controllers.

**Web** - open `http://<pi-address>:8080/` and sign in with the password
(`arstro-remote web --show` on the Pi prints it; change it with `arstro-remote web --set-password`).
Everything the app does, in the browser.

**CLI** - on the Pi: `arstro-remote status`. From another machine (in `server/`):
`python3 -m arstro_remote --url http://<pi-address>:8080 --token <password> status`
(or `ARSTRO_URL` / `ARSTRO_TOKEN`). A few commands:

```bash
arstro-remote rec status | start | stop            # recorder
arstro-remote rec set mode=raw raw.ffv1=true       # settings (shared with app and web)
arstro-remote gallery list                         # recordings
arstro-remote gallery convert REC_<take>.arh --to h264-vpu --scale 720 --wait
arstro-remote term run "uptime"                    # one command in a fresh shell
arstro-remote watch recorder jobs                  # live state changes
```

`docs/parity.md` shows how every feature is reached from each controller.

## Development

| | |
|---|---|
| Requirements | `docs/requirements.md` - every change starts here |
| How each feature is reached | `docs/parity.md` |
| Wire protocol | `docs/protocol.md` |
| Architecture | `docs/architecture.md` |
| Requirement → test map | `docs/traceability.md` (`scripts/req_coverage.py --check`) |

Tests (all tagged with the requirements they prove):

```bash
# on the Pi (server running)
cd ~/arstro-remote-src/tests && python3 test_daemon.py && python3 test_sync_web.py && python3 test_recorder.py
# anywhere
python3 server/tests/test_repo_policy.py && scripts/check_scripts.sh
cd app && flutter analyze && flutter test
cd server/tests/web && npm install && ARSTRO_URL=http://<pi>:8080/ ARSTRO_TOKEN=<password> node ui_test.mjs
```

Without Bluetooth (developer): `scripts/dev_tunnel.sh up <user@pi>` forwards the control socket
and the web port to a USB-attached phone; connect the app to `tcp:127.0.0.1:7788`.
The server also runs on a PC with a test pattern (`python3 -m arstro_remote run --no-bluetooth
--simulate 1280x720@30`): the preview then uses x264 instead of the Pi's VPU.

For agents: the skills in `.claude/skills/arstro.orangepi5plus.implement` and
`.claude/skills/arstro.orangepi5plus.test` describe the workflow (requirements first, all three
controllers, tests, commit and push).

Nothing machine-specific is committed (addresses, serials, passwords): scripts take arguments,
environment variables or `local.env` (gitignored); the signing key stays in `app/android/`
(gitignored). `server/tests/test_repo_policy.py` checks this.
