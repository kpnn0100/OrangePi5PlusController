---
name: arstro.ntwb.implement
description: Implement or change NTWB, the native-to-web bridge (protocol 1.0.0) between Arstro Remote (the host - Apps page, launcher, relay) and native apps that show their UI in a browser (the first is Cosmo). Covers changing the protocol itself, the host (Apps module), the SDKs (web ntwb.js, Python app SDK, C++ core/Ntwb in the arstro repo), and adapting a new app (manifest, API description, adapter, web UI, install). Its whole point is CONSISTENCY - one definition (spec.py) from which the docs are generated and against which every other representation is tested. Invoke for "add a message/field to NTWB", "the app's web UI needs X from the app", "adapt <app> to Arstro Remote", "why does the host refuse this message", "/arstro.ntwb.implement".
---

# arstro.ntwb.implement

**NTWB lets a native app keep its core on the machine and show its UI in a browser.** Arstro Remote
(the *host*) lists installed apps, launches them, serves their web UI at `/apps/<id>/` and relays
JSON messages and binary blobs between the app (Unix socket) and every browser (WebSocket). Read
`docs/ntwb/NTWB.md` once per session - it is short and it is the why.

Also load `arstro.embedded_server.implement` (this repo's workflow: requirements first, tests,
docs, deploy to the idle slot, commit + push). In the arstro repo, `arstro.rule` and the app's own
skills apply as well.

## The one rule: one definition, everything else checked against it

The protocol exists **once**, as code: `server/arstro_remote/ntwb/spec.py`. Every other form is
either generated from it or tested against it - never edited on its own:

| representation | how it stays equal to spec.py | checked by |
|---|---|---|
| `docs/ntwb/api.json`, `docs/ntwb/API.md` | **generated**: `cd server && python3 -m arstro_remote.ntwb.spec --write ../docs/ntwb` | `test_ntwb.py::generated_reference_is_committed_and_current` |
| `docs/ntwb/NTWB.md` (narrative) | hand-written; must name every message and the version | same test |
| host validation (`ntwb/host.py`) | calls `spec.validate()` / `validate_blob_header()` on everything it receives | `test_ntwb.py` (rejections), `a_raw_app_is_checked` |
| Python app SDK (`ntwb/app.py`) | validates everything it sends with `spec` | `test_ntwb.py` via `server/examples/ntwb/hello` |
| web SDK (`web/static/ntwb/ntwb.js`) | `VERSION` + `MESSAGES.c2h/h2c` lists | `test_ntwb.py::web_sdk_speaks_the_same_protocol` |
| C++ client (arstro `core/Ntwb`) | `spec/api.json` = a **vendored copy** of the generated file; `Protocol.cpp`'s table | arstro ctest `ntwb_tests::table_matches_the_vendored_spec` |
| an app's own API (`api.json` of its manifest) | the app generates it from its code (Cosmo: `cosmo-cc ntwb api`) | host `spec.validate_app_api`; Cosmo ctest `cosmo_ntwb_api_current` |

**So a protocol change is always, in this order:**

1. **Requirement** - `docs/requirements.md` NTWB-/APP- section (new id, or amend the text).
2. **`spec.py`** - the message/field/type/direction/manifest key/timing. Nothing else first.
3. **Regenerate** `docs/ntwb/` (command above); update `NTWB.md` where the story changed (a new
   message must be named there - the test enforces it).
4. **Host** (`ntwb/host.py`) - behaviour of the new thing; **SDKs**: `app.py`, `ntwb.js` (lists +
   API), then in the arstro repo copy `docs/ntwb/api.json` over `core/Ntwb/spec/api.json`, run
   `ntwb_tests` and **see it fail**, then change `Protocol.cpp` / `Client` until it passes.
5. **Tests** - `python3 server/tests/test_ntwb.py` (host + SDKs + docs), arstro `ctest -R ntwb`,
   and the example app if the change is visible to apps (`server/examples/ntwb/hello`).
6. **Version** - `spec.VERSION`: PATCH for wording/docs, MINOR for a new optional field or message
   (peers send only what the lower version defines), MAJOR for anything a 1.x peer would reject.
   `ntwb.js` and `core/Ntwb` carry the version too; the tests compare them. Add a row to the
   *Versions* table in `NTWB.md`.
7. Deploy (host into the idle slot: `arstro-remote slots --idle`, see `arstro.embedded_server.deploy`),
   reinstall affected apps (`cosmo-cc ntwb install`), commit + push **both** repos (Arstro Remote first -
   it owns the spec; arstro second, with the re-vendored copy).

A change that updates the code but not the generated docs, or the host but not the vendored copy,
fails a test - that is the design, not an obstacle. Never "fix" such a test by editing the
generated file or the vendored copy by hand.

## Adapting a new app

1. **Requirements in the app's repo** (for arstro apps: an `R-NTWB` section + as-built `DR-NTWB`,
   like `apps/cosmo/REQUIREMENTS.md`), and a row in this repo's `docs/parity.md` if it changes
   what the host offers.
2. **The adapter is a front end, not a second app.** It must hold no behaviour: map NTWB `call`
   onto the app's own command path (for an Arstro app: its `Command` grammar - Cosmo exposes
   exactly one method for all changes, `command {line}`), the app's observable model onto a
   `state` key, its events onto NTWB `event`s, pixels onto blobs (`coalesce` only for live
   streams). Anything a browser would otherwise have to re-derive (catalogues, unit conversions,
   ranges) is published as data by the adapter - see Cosmo's `controls` (EditControls.h, sampled
   conversions).
3. **API description**: generated from the app's code (`<app> ntwb api`), committed, drift-tested,
   installed as the manifest's `api` - the host then refuses unknown methods (NTWB-07) and serves
   it to agents (`arstro-remote apps api <id>`).
4. **Install**: the app writes `$XDG_DATA_HOME/ntwb/apps/<id>/{ntwb.json, api.json, web/}` itself
   (`cosmo-cc ntwb install`); `exec` is its adapter's absolute path. No host restart needed.
5. **Web UI**: static files + `<script src="/ntwb/ntwb.js">`; `NTWB.connect()`, `onState`,
   `onEvent`, `onBlob`, `call`, `notify`. Presentation only. It must work at 360 px and follow the
   app's design language (for Cosmo: `arstro.cosmo.design.implement`, R-G-1 - every visible change
   eases).
6. **Verify without a browser first**: `arstro-remote apps call <id> <method> '<json>'`,
   `arstro-remote apps state <id> <key>`, `arstro-remote apps log <id>`; then the page:
   `ARSTRO_TOKEN=... DISPLAY=:0 python3 server/tests/web/page_smoke.py http://127.0.0.1:<port> <dir> [390x844] "/apps/<id>/"`.

## Gotchas (each cost time)

- **Deploy into the idle slot** - `arstro-remote slots` first; a shell in a web terminal is a child
  of that slot's daemon (`arstro.embedded_server.deploy`, rule zero).
- `apps.*` ops name the app `app`, never `id` - `id` is the protocol's request id and a clash
  silently turns "cosmo" into a number.
- `ready` may reach a client **before** the app published its first `state` (the app connects,
  then publishes): read state through `onState`, which fires on the later `state` too. The host
  clears retained state when an app disconnects, so a restarted app never shows a stale value.
- Blobs are delivered in order and all of them, unless the header says `coalesce: true` (live
  previews). Thumbnails, files: never coalesce.
- Errors travelling through Arstro Remote lose leading/trailing quotes (`OpError` text is
  stripped) - write "no method named x", not "no method 'x'".
- Unix socket paths are limited to ~100 bytes: put test runtime dirs under a short `/tmp/...`.
- Front-end verbs of an app's grammar (Cosmo: `state print`, `wait`, `ui dump`, `quit`) must be
  answered or refused explicitly by the adapter - never accepted silently.
- A second front end finds core defects the first one hides (Cosmo D-62, JPEG orientation): file
  them in the app's defect list; do not paper over them in the web view.
