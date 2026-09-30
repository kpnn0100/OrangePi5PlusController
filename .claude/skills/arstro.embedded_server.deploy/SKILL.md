---
name: arstro.embedded_server.deploy
description: Deploy the Arstro embedded server safely with A/B slots - install a new version into the idle slot (like an A/B boot partition) while the other keeps serving the running session, verify it, then switch or roll back. Also covers first installs on a new machine (install.sh), headless machines, migrating an old unslotted install, and uninstalling. Use whenever server code must go onto a machine, or the user asks to install, update, switch slots or roll back.
---

# Deploying with A/B slots

Two instances of the server can run side by side on one machine (ADM-06):

| | slot A | slot B |
|---|---|---|
| web / remote CLI | `http://<host>:8080/` | `http://<host>:8081/` |
| code | `~/.local/share/arstro-remote-a/` | `~/.local/share/arstro-remote-b/` |
| config (+ `web_token`) | `~/.config/arstro-remote-a/` | `~/.config/arstro-remote-b/` |
| logs | `~/.local/state/arstro-remote-a/` | `~/.local/state/arstro-remote-b/` |
| socket / lock | `$XDG_RUNTIME_DIR/arstro-remote-a.sock\|.lock` | `...-b.sock\|.lock` |
| CLI | `arstro-remote-a` | `arstro-remote-b` |
| start at boot | `~/.config/autostart/arstro-remote-a.desktop` | `...-b.desktop` |

Both start at boot. The **active** slot (`~/.config/arstro-remote-active`) owns the app's Bluetooth
link and the plain `arstro-remote` command. An old, unslotted install (`arstro-remote.desktop`,
`~/.config/arstro-remote/`, port 8080, `~/.local/bin/arstro-remote-launcher`) counts as slot A
until it is migrated.

## Rule zero - ask which slot hosts THIS session, every time, before any deploy

**Run this first, from the shell you will deploy from:**

```bash
arstro-remote slots            # or, from the repo:  cd server && python3 -m arstro_remote slots
#   b       port 8081  running  pid 71117  <- THIS SESSION (do not reinstall/restart)
#   legacy  port 8080  running  pid 2123
#   deploy into: a
arstro-remote slots --idle     # just the answer: the slot to deploy into
```

It walks this process's parents up to a slot's daemon (its pid is in
`$XDG_RUNTIME_DIR/arstro-remote-<slot>.lock`): a shell in a web terminal - and the agent or
`install.sh` started from it - is a child of the slot that serves that terminal, so
reinstalling or restarting that slot kills the shell mid-deploy. **Deploy only into the slot
`--idle` names.** The session moves between slots over time (after a switch the user works on
the other port), so never assume "B is the test slot" - ask every time.

`install.sh` enforces it: started from inside slot X it refuses `--slot X` (and a `legacy`
host counts as slot A, whose port it owns) unless `--force-own-session` - use that only when
the user asked for it and knows the terminal will die. `--no-start` into the own slot is
allowed (files only; the running daemon keeps its code until it restarts).

When the session is not inside any slot (`slots --current` prints `-`: SSH, the desktop's
own terminal), both slots are safe to touch as far as *you* are concerned - but the **user**
may be working in one: check which port their browser uses before restarting it.

## Update (the normal case)

1. **Commit first** (the deploy installs the working tree; the commit is the version).
2. **Install into the idle slot** - the one `arstro-remote slots --idle` names - on the machine,
   from the repository:
   ```bash
   ./install.sh --slot "$(cd server && python3 -m arstro_remote slots --idle)" --yes
   ```
   From a dev PC: `scripts/setup_pi.sh --slot b user@<host>`. The installer swaps the code
   atomically, restarts that slot's daemon through its launcher and waits until it answers.
   New groups (first install of IO permissions) apply from the next login; to give a freshly
   started slot the `gpio` group now: `sg gpio -c "./install.sh --slot b --yes"`.
3. **Verify** (see `arstro.embedded_server.test`):
   ```bash
   curl -s http://127.0.0.1:8081/api/ping                 # version, slot "b"
   arstro-remote-b system info                            # modules, board, paths
   arstro-remote-b log -n 200 --grep -i error             # nothing unexpected
   ARSTRO_SLOT=b python3 server/tests/test_daemon.py      # + test_sync_web, test_screen, test_recorder
   ARSTRO_TOKEN=$(cat ~/.config/arstro-remote-b/web_token) DISPLAY=:0 python3 server/tests/web/page_smoke.py http://127.0.0.1:8081 <dir>
   ```
   Also check that the other slot is still fine: `curl -s http://127.0.0.1:8080/api/ping`.
4. **Tell the user** the new version runs on 8081 and what was verified. They switch their
   browser / session to 8081 when it suits them.
5. **Switch** (only when the user works on the new slot, or agrees):
   ```bash
   ./install.sh --slot b --activate --yes   # B gets the Bluetooth link + the plain `arstro-remote` command
   arstro-remote-a system restart           # A restarts without the Bluetooth link (run from B's session!)
   ```
6. **Bring the other slot up to date later** - when nobody works in A any more, install the same
   commit into A (`./install.sh --slot a --yes`). Now A is the idle slot for the next update.

**Roll back**: the other slot still runs the previous version - use its port, and
`./install.sh --slot a --activate` if the Bluetooth link must move back. A broken slot can be
reinstalled from any older commit (`git checkout <commit> -- server && ./install.sh --slot b`).

## First install on a new machine

```bash
./install.sh                 # slot A on 8080: asks for the web password (Enter = admin - tell the user to change it)
./install.sh --slot b        # optional second slot on 8081 (copies A's settings and password)
```
Options: `--modules monitor,system,terminal,io,files` (only what the machine needs),
`--password PW`, `--bluetooth on|off`, `--boot desktop|systemd|none` (systemd user service +
polkit rule + linger for machines without a desktop), `--no-deps`, `--no-perms`, `--no-start`.
It must run as the desktop user (not root); it asks for sudo only for packages and device
permissions (`server/scripts/install_deps.sh`, `server/scripts/setup_io_perms.sh`).

## Migrating an old unslotted install to slot A

From a session that is **not** hosted by the old instance (e.g. slot B's web terminal on 8081):
`./install.sh --slot a --yes`. It seeds A's config and password from an installed slot or the old
`~/.config/arstro-remote/`, disables `arstro-remote.desktop` (renamed `...replaced-by-arstro-remote-a`)
and stops the old launcher and daemon, then starts A on 8080. The old `~/.local/bin/arstro-remote`
script stays until `--activate` replaces it with the link to the active slot. Remove the old
code with `server/uninstall.sh --legacy`.

## Remove a slot

`./install.sh --slot b --uninstall` (config and logs stay) or `server/uninstall.sh --slot b --purge`.

## Where to look when something is wrong

`~/.local/state/arstro-remote-<slot>/`: `launcher.log` (starts, exits, restarts),
`arstro-remote.log` (daemon: every failed op is a WARNING with op and controller),
`recorder.log`, `screen.log`, `crash.log` (Python stacks of hard crashes). Or System › Logs in
the web UI / `arstro-remote-<slot> log -f`. `arstro-remote-<slot> log --level debug` logs every op
with its duration (reset with `--level info`).
