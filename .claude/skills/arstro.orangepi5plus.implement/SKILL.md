---
name: arstro.orangepi5plus.implement
description: Implement or change anything in OrangePi5PlusController - the Orange Pi 5 Plus server (Bluetooth, Wi-Fi, shells, input, HDMI-RX recorder, gallery), the Android app, the web UI or the CLI. Load it before touching server/, app/, the web UI or the scripts. It enforces the project rules - requirements first, the feature in all three controllers (GUI, CLI, web) kept in sync by the one server, tests, docs - and ends every implementation with a commit and a push.
---

# Implementing in OrangePi5PlusController

**One server, three controllers.** The `arstro-remote` daemon on the Pi owns all hardware and
state. The Android app (GUI), the CLI and the web UI only send ops and render the state the
server pushes. A feature is done only when **all three** can use it and a change made in one
shows up in the others (ARC-02/03/04). Read `docs/architecture.md` once per session.

## The workflow (do every step, in order)

1. **Requirements first.** Open `docs/requirements.md`. Find the requirement(s) the task touches.
   New behaviour → add a requirement with the next free ID in its section (never renumber;
   removed ones are marked `(withdrawn)`). Changed behaviour → edit the requirement text.
   If the request conflicts with a requirement, say so and ask before changing it.
2. **Check consistency** with what exists: `docs/parity.md` (how each feature is reached),
   `docs/protocol.md` (ops, topics, frames). Reuse existing ops/topics/UI patterns; name new
   ops `area.verb` like the others.
3. **Server** (`server/arstro_remote/`): the op, the state it changes, and the `hub.publish(...)`
   of that state (see *Patterns*). Validate input, raise `OpError`/`ValueError` with a sentence a
   user understands. Log who did it (`session.controller`), never secrets.
4. **All three controllers** - same feature, same words:
   * GUI: `app/lib/src/...` (Recorder tab: `ui/recorder/`; system items: `ui/system_section.dart`)
   * Web: `server/arstro_remote/web/static/js/views/*.js` (no build step; plain ES modules)
   * CLI: `server/arstro_remote/cli.py` (subcommand + `--json` output)
   Each one must follow the live state (subscribe to the topic), not a private copy.
   If something truly cannot exist in one controller, add it to *Exceptions* in `docs/parity.md`
   with the reason.
5. **Tests**, tagged with the requirement IDs (see the `arstro.orangepi5plus.test` skill):
   server behaviour in `server/tests/test_*.py` (`@test("REQ-ID")`), web in
   `server/tests/web/ui_test.mjs`, app layout/logic in `app/test/*.dart` (`// covers: REQ-ID`).
   Run what you touched and everything cheap: `flutter analyze && flutter test`,
   `python3 server/tests/test_repo_policy.py`, `scripts/check_scripts.sh`.
6. **Docs**: update `docs/parity.md` (the row for the feature, all three columns),
   `docs/protocol.md` (new ops/topics/fields), README if user-visible, then
   `python3 scripts/req_coverage.py --check` (regenerates `docs/traceability.md`; must pass).
7. **Deploy + verify on the Pi** when it is reachable (see *Deploy*), run the affected suites there.
8. **Commit and push** - every implementation ends with this:
   `git add -A && git commit -m "<what and why, requirement IDs>" && git push`.
   Before committing run `python3 server/tests/test_repo_policy.py` (no addresses, serials, user
   names, passwords, keys). Never commit `local.env`, `app/android/key.properties`, `*.jks`.

**Definition of done:** requirement written · server + GUI + CLI + web · live sync works
(change in one, see it in the others) · tests tagged and green · parity/protocol/traceability
updated · deployed or clearly reported as not deployable · committed and pushed.

## Map

| Path | What |
|---|---|
| `server/arstro_remote/daemon.py` | main loop, config, sessions, services start |
| `server/arstro_remote/hub.py` | state topics; `publish(topic, data)` pushes to every session |
| `server/arstro_remote/session.py` | one connection: `op_*` handlers, dispatch, state queue |
| `server/arstro_remote/recorder/service.py` | recorder/gallery/jobs ops, worker supervision, `TARGETS` |
| `server/arstro_remote/recorder/{worker,capture}.py` | GStreamer capture process: preview + recording branches |
| `server/arstro_remote/recorder/{jobs,transcode,library,thumbs,settings}.py` | conversions, gallery scan, thumbnails, settings schema |
| `server/arstro_remote/web/server.py` + `auth.py` | HTTP/WS, password, cookie, origin checks, `web.*` ops |
| `server/arstro_remote/web/static/` | web UI: `js/core.js` (connection, store, toolkit), `js/views/*.js`, `app.css` |
| `server/arstro_remote/cli.py` | CLI controller (local socket or `--url`) |
| `app/lib/src/remote_client.dart` | link, requests, `topic(name)` state notifiers, `media` (Wi-Fi link) |
| `app/lib/src/media.dart` | `MediaLink` (web address + password over BT), `PreviewController` |
| `app/android/.../MediaBridge.kt` | MediaCodec preview decoder into a texture, DownloadManager |
| `scripts/` | `setup_all.sh`, `setup_pi.sh`, `build_apk.sh`, `dev_tunnel.sh`, `req_coverage.py`, `check_scripts.sh` |

## Patterns

**New op** - `session.py`: `def op_area_verb(self, msg)` for core ops; recorder/gallery/jobs ops
live in `RecorderService` as `op_recorder_...(self, session, msg)`; `web.*` in `WebServer.handle`.
Slow ops (I/O, subprocess) belong to a prefix in `SLOW_PREFIXES` (worker pool); `in.*` run in
order on the input thread.

**New shared state** - pick a topic (or add one: document it in `docs/protocol.md`), call
`self.ctx.hub.publish("topic", data)` after every change, from whichever thread. Controllers:
app `client.topic('topic')` (a `ValueNotifier`), web `store.on("topic", fn)`, CLI
`ctl.client.state["topic"]` / `watch topic`. Never make a controller poll for it.

**Settings** - add the key to `recorder/settings.py` `DEFAULTS` and `SCHEMA` (validated there);
the app settings sheet, the web settings sheet and `rec set key=value` all go through
`recorder.settings.set`.

**App UI** - dark, minimal, one accent (`0xFF7C9CFF`, same as the web), rounded cards, ≤ 300 ms
animations (UX-01..03). New tab: `_dests` + `pages` in `home_page.dart` (hidden tabs run with
`TickerMode(enabled: false)`). Always check 360×640 to 1920×1080 with `layout_test.dart`.

**Web UI** - build DOM with `h()` (never `innerHTML` with server data - XSS), views are
`{id, title, icon, mount(el) -> cleanup}`, call ops with `conn.call(op, args)` / `run(...)`,
subscribe with `store.on(...)` and unsubscribe in the cleanup.

**Protocol changes** - frames in `protocol.py` and `app/lib/src/proto/frames.dart` (+ the
byte-for-byte test in `frames_test.dart`) and `core.js`; bump `PROTO_VERSION`/`kProtoVersion`
only for incompatible changes (the app refuses a mismatch). Controllers skip unknown frame types.

## Deploy

```bash
scripts/setup_pi.sh user@<pi>            # copy server/, install, restart the in-session daemon (+ --test)
# quick iteration on a Pi you already set up:
rsync -az --delete --exclude __pycache__ --exclude node_modules server/ user@<pi>:arstro-remote-src/ \
  && ssh user@<pi> 'cd ~/arstro-remote-src && ./install.sh'
./build_apk.sh [--install SERIAL]        # app; release key in app/android (gitignored)
```
Get the Pi address, user and phone serial from the user, `local.env` or the environment -
never write them into files of the repo.

## Rules and gotchas (each cost real time)

- The daemon must run **inside the auto-login desktop session** (autostart launcher), not from
  SSH or systemd: polkit only lets the active session change Wi-Fi, XTEST needs the display.
  `install.sh` restarts it through the running launcher.
- `sudo` on the Pi: only when needed, password fed on stdin (`sudo -S`), never stored in files,
  scripts, memory or commits. Don't reboot a shared Pi or disable services you did not create
  without asking.
- **Never `pkill -f`/`pgrep -f` with a pattern that also appears in your own command line** - it
  kills your shell. Match PIDs via `/proc/<pid>/cmdline` or use anchored patterns.
- Unix socket paths max ~100 bytes (SSH ControlPath, `ARSTRO_SOCKET` in deep scratch dirs).
- HDMI RX: avoid stress tests (the board has reset under heavy HDMI load); use the test source
  (`rec source --test 1920x1080@30`) for automated tests and switch back afterwards.
- GStreamer launch strings: two capsfilters in a row (`! caps ! caps !`) do not parse - merge them.
  swscale cannot read NV16 (convert rows yourself, see `thumbs.py`). `mp4mux` takes no PCM
  (encode AAC). Fractional fps confuse MPP rate control (`capssetter` to an integer).
- The recorder worker re-execs itself to repair a blacklisted MPP registry; do that before
  opening IPC fds.
- Terminals: `term.attach` replies **before** the replayed bytes; output frames carry absolute
  offsets; controllers drop what they already have. Keep both properties.
- Web: static files are served with ETag + `no-cache`; the page has a strict CSP (no inline
  scripts; `blob:` only for media). WebCodecs exists only in secure contexts (https/localhost);
  plain http on the LAN uses the MSE path.
- App: preview only while the Live view is visible and the app is in front (REC-06). Media URLs
  need `android:usesCleartextTraffic` (LAN http). The layout test's `ErrorLog` must be restored
  before failing, and `scrollUntilVisible`/`pumpAndSettle` hang while the record button pulses -
  drag manually.
- Everything machine-specific comes from arguments / env / `local.env` / `~/.config/arstro-remote/`
  (ARC-05). `test_repo_policy.py` enforces it.
