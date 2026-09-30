---
name: arstro.embedded_server.implement
description: Implement or change anything in the Arstro embedded server (OrangePi5PlusController) - the modular server for Orange Pi 5 Plus / any Linux board (Monitor, System, Camera, Connection, Terminal, Screen, IO Control, Files; A/B slots; installer), its web UI, CLI and Android app. Load it before touching server/, app/, the web UI, install.sh or the scripts. It enforces the project rules - requirements first, the feature in every controller kept in sync by the one server, standard Linux interfaces with board specifics in board modules, logging, tests, docs - and ends every implementation with a commit, a push and a deploy into the idle A/B slot.
---

# Implementing in the Arstro embedded server

**One server, three controllers.** The `arstro-remote` daemon owns all hardware and state. The
Android app (GUI), the CLI and the web UI only send ops and render the state the server pushes.
A feature is done only when **every controller** can use it and a change made in one shows up
in the others (ARC-02/03/04). Read `docs/architecture.md` once per session.

**Modules.** Features live in modules (Monitor, System, Camera, Connection, Terminal, Screen,
IO Control, Files - `server/arstro_remote/modules.py`), switched on per machine. Everything uses
**standard Linux interfaces** (ARC-08, MOD-03); anything board-specific is *information* in
`server/arstro_remote/boards/` (see *Patterns*), never a hard dependency.

**A/B slots.** The server is installed in slots (A = 8080, B = 8081, ADM-06). You deploy into
the slot that is **not** serving the user's (or your own) session - see the
`arstro.embedded_server.deploy` skill. Never restart or reinstall the slot whose web terminal
you are running in.

## The workflow (do every step, in order)

1. **Requirements first.** Open `docs/requirements.md`, find the requirement(s) the task touches.
   New behaviour → a requirement with the next free ID in its section (never renumber; removed
   ones are marked `(withdrawn)`). Changed behaviour → edit the text. A conflict with a
   requirement → say so and ask before changing it.
2. **Consistency**: `docs/parity.md` (how each feature is reached), `docs/protocol.md` (ops,
   topics, HTTP routes). Reuse patterns; name ops `area.verb` with the module's prefix.
3. **Server** (`server/arstro_remote/`): the op in the module's service, the state it changes,
   `hub.publish(...)` of that state (see *Patterns*). Validate input first, raise `OpError` /
   `ValueError` with a sentence a user understands. **Log** who did what (`session.num`,
   `session.controller`) at INFO for changes, never secrets (SEC-05); failures are logged by
   `Session._handle` automatically (LOG-02).
4. **Every controller** - same feature, same words:
   * Web: `server/arstro_remote/web/static/js/views/*.js` (plain ES modules, no build step); a
     page belongs to a group in `js/main.js` `GROUPS` (group = module).
   * CLI: `server/arstro_remote/cli.py` (subcommand + `--json` output; `add_module_parsers`).
   * GUI: `app/lib/src/...` - the app is built on the dev PC (Flutter is not on the board). If
     you cannot build it, add the feature to the *Pending in the app* note / Exceptions in
     `docs/parity.md` and tell the user; don't claim it is in the app.
   Each controller follows the live state (subscribe to the topic), not a private copy.
5. **Tests**, tagged with the requirement IDs (`arstro.embedded_server.test` skill):
   `server/tests/test_*.py` (`@test("REQ-ID")`), `server/tests/web/ui_test.mjs`, `app/test/*.dart`
   (`// covers: REQ-ID`), `scripts/check_scripts.sh` (`# covers:`). Hardware-writing tests are
   opt-in (env var), never drive real pins by default.
6. **Docs**: `docs/parity.md` (the row, all columns), `docs/protocol.md` (ops / topics / routes),
   `docs/architecture.md` if the structure changed, README if user-visible, then
   `python3 scripts/req_coverage.py --check` (regenerates `docs/traceability.md`; must pass).
7. **Deploy into the idle slot and verify** (`arstro.embedded_server.deploy`): first
   `arstro-remote slots --idle` names it, then install there and run the affected suites against
   it (`ARSTRO_SLOT=<idle> ...`).
8. **Commit and push** - every implementation ends with this:
   `git add -A && git commit -m "<what and why, requirement IDs>" && git push`.
   Before committing: `python3 server/tests/test_repo_policy.py` (no addresses, serials, user
   names, passwords, keys - use RFC 5737 addresses like 192.0.2.10 in examples). Never commit
   `local.env`, `app/android/key.properties`, `*.jks`.

**Definition of done:** requirement written · server + web + CLI (+ app or a documented pending
note) · live sync works · logged · tests tagged and green · parity / protocol / traceability
updated · deployed into the idle slot and verified · committed and pushed.

## Map

| Path | What |
|---|---|
| `install.sh` → `server/install.sh` | the one installer: slot, port, password, modules, packages (`scripts/install_deps.sh`), device permissions (`scripts/setup_io_perms.sh`), boot entry, start + check |
| `server/scripts/arstro-remote-launcher` | per-slot supervisor (`ARSTRO_SLOT`), restarts the daemon |
| `server/arstro_remote/paths.py` | slot → instance name, config/state/install dirs, socket, lock, `DEFAULT_CONFIG` |
| `server/arstro_remote/modules.py` | module table (name → title, op prefixes), `enabled(config)`, `describe()` |
| `server/arstro_remote/daemon.py` | main loop, services per module (`_start_services`), logging setup, `request_stop` |
| `server/arstro_remote/session.py` | one connection: core `op_*`, `dispatch` (module check + `ROUTES` to services), hello |
| `server/arstro_remote/hub.py` | state topics; `publish(topic, data)` pushes to every session |
| `server/arstro_remote/system.py` | `system.*`, `log.*`, uncaught-exception + crash logging |
| `server/arstro_remote/netconf.py` | `net.*` (nmcli profiles, devices), `bt.*` (bluetoothctl) |
| `server/arstro_remote/files.py` | `files.*`; HTTP upload/download in `web/server.py` |
| `server/arstro_remote/hwio/` | IO Control: `gpio.py` (chardev uAPI v2 via ioctl), `i2c.py`, `spi.py`, `uart.py` (`SerialTerminal`), `sysfs.py` (PWM/LED/ADC), `dt.py` (device-tree names), `service.py` (`io.*`) |
| `server/arstro_remote/boards/` | board modules: `detect()` by DT `compatible`; `orangepi5plus.py` header map |
| `server/arstro_remote/recorder/` | Camera: service (`recorder.*`, `gallery.*`, `jobs.*`, `camera.*`), worker, capture, v4l2 (HDMI RX, `V4l2Camera`, EDID) |
| `server/arstro_remote/screen/` | remote screen service + worker |
| `server/arstro_remote/web/` | HTTP/WS server, auth; `static/js/main.js` groups, `core.js`, `views/*.js` |
| `server/arstro_remote/cli.py` | CLI controller (local socket of the slot, or `--url`) |
| `app/lib/src/...` | Flutter app (built on the dev PC) |

## Patterns

**New op in a module** - put it in the module's service as `op_area_verb(self, session, msg)`
(the service's `handle()` dispatches by name) or, for core ops, in `session.py`. A new prefix:
add it to `modules.py` (`MODULES[...]` prefixes), `session.py` `ROUTES` (prefix → daemon
attribute) and `SLOW_PREFIXES` if it can block (I/O, subprocess).

**New module** - `modules.py` entry, a service class (`start()`, `shutdown()`, `handle()`),
`daemon._start_services` (`self._start("name", "attr", factory, "what")`), a group in the web
`main.js` `GROUPS`, CLI subcommands, requirements section, `install_deps.sh` packages, tests in
`test_modules.py` (the throwaway instance) or a suite of its own.

**New shared state** - pick or add a topic (document it in `docs/protocol.md`), call
`self.ctx.hub.publish("topic", data)` after every change. Web `store.on("topic", fn)`, CLI
`ctl.client.state["topic"]` / `watch topic`, app `client.topic('topic')`. No polling in controllers.

**Hardware access** - standard interfaces only: character devices through `fcntl.ioctl` with
`ctypes` structs (assert their sizes), sysfs files, subprocesses of standard tools
(`nmcli`, `bluetoothctl`, `v4l2-ctl`). Turn `EACCES` into "permission denied - run install.sh
(group X), then log in again". Board-specific facts (pin names, which overlay enables what) go
into a board module; generate them from the board's device tree where possible (the OPi 5 Plus
header table was generated from its pinctrl groups - don't hand-type roles).

**Web UI** - DOM with `h()` (never `innerHTML` with server data - XSS), a view is
`{id, title, icon, mount(el, opts) -> cleanup}`, add it to a group in `main.js`; ops with
`conn.call` / `run`, state with `store.on` (unsubscribe in cleanup). Must work from 360 px wide
(check with `server/tests/web/page_smoke.py ... 390x844`).

**Logging** - `log = logging.getLogger("arstro.<area>")`; INFO for user-visible changes with
who did it, DEBUG for details, never passwords or tokens. Helper processes write to their own
log in the slot's state dir.

## Rules and gotchas (each cost real time)

- **Slots**: never restart / reinstall the slot that hosts your session (its terminal dies with
  it). **Ask, don't assume:** `arstro-remote slots` (or `cd server && python3 -m arstro_remote
  slots`) marks the slot hosting this shell and prints the one to deploy into; the session moves
  between A and B over time. `pkill -f 'python3 -m arstro_remote run'` would hit *every* slot - stop a slot by the pid
  in its lock file (`$XDG_RUNTIME_DIR/arstro-remote-<slot>.lock`) or `system restart`.
- Two slots share the hardware: the camera worker re-writes the HDMI EDID only when it changed
  (a write re-plugs the source and would glitch the other slot's recording); only the active
  slot owns the Bluetooth RFCOMM profile (`bluetooth_enabled`).
- The daemon runs **inside the desktop session** (autostart) so polkit lets it drive
  NetworkManager and XTEST has the display; on headless machines `install.sh --boot systemd`
  adds a polkit rule instead.
- New groups (gpio, i2c, spi) count only from the next login; the udev rules also give the
  logged-in desk user ACLs (uaccess) for `/dev/gpiochip*`, `/dev/i2c-*`, `/dev/spidev*` at once.
  sysfs PWM/LED files need the group: start a process with `sg gpio -c ...` until the next login.
- `sudo` on the board: only when needed, password fed on stdin (`sudo -S`), never stored in
  files, scripts, memory or commits. Don't reboot a shared board or disable services you did
  not create without asking. Don't upgrade system packages the desktop runs on (X server).
- Never `pkill -f`/`pgrep -f` with a pattern that also appears in your own command line.
- Unix socket paths max ~100 bytes (`ARSTRO_SOCKET` in deep scratch dirs).
- HDMI RX: avoid stress tests; automated tests use the test pattern and switch back.
- GStreamer: two capsfilters in a row do not parse; swscale cannot read NV16; `mp4mux` takes
  no PCM; fractional fps confuse MPP rate control.
- Terminals: `term.attach` replies **before** the replayed bytes; output frames carry absolute
  offsets. Serial consoles are terminals too (`kind: "serial"`).
- Web: ETag + `no-cache` statics, strict CSP (no inline scripts), WebCodecs only in secure
  contexts; the login cookie is per port (`arstro_token_<port>`).
- Everything machine-specific comes from arguments / env / `local.env` / the slot's config dir
  (ARC-05). `test_repo_policy.py` enforces it.
