# OrangePi5PlusController

Control an Orange Pi 5 Plus - or any Linux machine - from an **Android app**, a **web page** or the
**command line**. Features come in modules you switch on per machine:

| Module | What |
|---|---|
| **Monitor** | CPU, memory, temperatures, disks, network, processes |
| **System** | server info, logs (live tail, level, markers), modules, pairing, web access, restart / reboot |
| **Camera** | HDMI input, USB (V4L2) camera or test pattern: live H.264 preview, recording (H.265 or RAW), gallery with conversion and verified-lossless FFV1 |
| **Connection** | Wi-Fi, Ethernet and every NetworkManager profile (DHCP / static), Bluetooth devices |
| **Terminal** | shared shells |
| **Screen** | watch and control the desktop (like TeamViewer), remote keyboard and mouse |
| **IO Control** | GPIO (header view, edges), I2C scan / transfer / dump, SPI, UART consoles, PWM, LEDs, ADC - a bring-up and debugging bench |
| **Files** | browse, upload, download, edit |

One server runs on the Pi. The three controllers use the same protocol and stay in sync: start
a recording from the CLI and the record button turns red in the app and on the web page.

| | Android app | Web UI | CLI |
|---|---|---|---|
| Reaches the Pi over | Bluetooth (control) + Wi-Fi (video) | the network, `http://<pi>:8080/` (slot B: 8081) | the Pi itself, or `--url` from anywhere |
| Sign-in | pairing (Bluetooth) | password, once per browser | none on the Pi / password remotely |

## Install the server on a machine (one script)

On the machine itself, as the user the server should run as (the desktop user, not root):

```bash
git clone <this repository> && cd OrangePi5PlusController
./install.sh                                   # slot A, port 8080, all modules; asks for the web password (Enter = admin)
./install.sh --modules monitor,system,terminal,io,files     # only what this machine needs
./install.sh --slot b                          # a second instance on port 8081 next to A (try a new version)
./install.sh --help                            # --password, --bluetooth, --boot desktop|systemd|none, --activate, --uninstall ...
```

It installs the missing packages (sudo), gives the user access to GPIO / I2C / SPI / serial / PWM /
LEDs (udev rules + groups), installs the code into `~/.local/share/arstro-remote-<slot>`, writes
the slot's config and password, registers the start at boot (desktop autostart, or a systemd user
service with `--boot systemd` on machines without a desktop), starts it and checks it answers.

**A/B slots.** Two slots run side by side, each with its own code, config, logs, port and boot entry.
Install a new version into the slot you are not using (`--slot b`), test it on its port, then make
it the active one (`./install.sh --slot b --activate`: the plain `arstro-remote` command and the
app's Bluetooth link move to it). Commands per slot: `arstro-remote-a`, `arstro-remote-b`
(or `arstro-remote --slot b`). Logs: `arstro-remote-b log -f`, or System › Logs in the web UI,
or `~/.local/state/arstro-remote-b/`.

## Quick start from a dev PC (server + app)

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
scripts/setup_pi.sh orangepi@<pi-address>     # install / update the Pi side (also --slot b, --autologin, --reboot, --test)
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
arstro-remote gallery verify REC_<take>_FFV1.mkv --delete-raw --wait   # prove the FFV1 is lossless, free the RAW
arstro-remote term run "uptime"                    # one command in a fresh shell
arstro-remote watch recorder jobs                  # live state changes
arstro-remote io header                            # the pin header with live GPIO states
arstro-remote io i2c scan 2                        # i2cdetect-like scan
arstro-remote io uart open ttyS3 --baud 115200     # serial console (Ctrl+] detaches, shared with the web)
arstro-remote files put firmware.bin ~/uploads     # upload / download (files get PATH)
arstro-remote net set <uuid> --method manual --addresses 192.0.2.50/24 --gateway 192.0.2.1
arstro-remote log -f --grep error                  # follow the server log
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
cd server/tests && ARSTRO_SLOT=b python3 test_daemon.py && ARSTRO_SLOT=b python3 test_sync_web.py && ARSTRO_SLOT=b python3 test_recorder.py
python3 server/tests/test_modules.py               # starts its own throwaway instance (modules, IO, files, logs)
# anywhere
python3 server/tests/test_repo_policy.py && scripts/check_scripts.sh
cd app && flutter analyze && flutter test
cd server/tests/web && npm install && ARSTRO_URL=http://<pi>:8080/ ARSTRO_TOKEN=<password> node ui_test.mjs
```

Without Bluetooth (developer): `scripts/dev_tunnel.sh up <user@pi>` forwards the control socket
and the web port to a USB-attached phone; connect the app to `tcp:127.0.0.1:7788`.
The server also runs on a PC with a test pattern (`python3 -m arstro_remote run --no-bluetooth
--simulate 1280x720@30`): the preview then uses x264 instead of the Pi's VPU.

For agents: the skills `.claude/skills/arstro.embedded_server.implement` (workflow: requirements
first, all controllers, tests, commit and push), `.claude/skills/arstro.embedded_server.test`
(which suite proves what, how to run it against a slot) and `.claude/skills/arstro.embedded_server.deploy`
(the A/B procedure: install into the idle slot, verify, switch) describe how to work here.

Nothing machine-specific is committed (addresses, serials, passwords): scripts take arguments,
environment variables or `local.env` (gitignored); the signing key stays in `app/android/`
(gitignored). `server/tests/test_repo_policy.py` checks this.
