# NTWB 1.1.0 - native-to-web bridge

**NTWB lets a native program keep its core where it runs and show its UI in a browser.**
The program ("app") gets a small adapter; Arstro Remote ("host") lists it on its Apps page,
launches it, serves its web UI and relays messages and binary streams between the app and every
browser that opens it. The app's state and computation never leave the machine - the browser
is one more front end, next to the app's own window or CLI.

**It is MVVM across a network, not a remote screen.** The app on the machine is the *model*;
every browser builds its own *view* (and the *view-model* that feeds it) from what the model
publishes. A phone and a desktop can open the same session with completely different layouts,
each suited to its screen, and still see each other's edits the moment they land - because
what travels is the model, never the pixels of somebody's window (section 2).

This file is the narrative: the idea, the flow, the rules, examples. The exact catalogue -
every message, field, type, environment variable and manifest key - is
[API.md](API.md) / [api.json](api.json), **generated** from `server/arstro_remote/ntwb/spec.py`.
Where they differ, the generated reference is right and this file is the bug.

| document | authority |
|---|---|
| `server/arstro_remote/ntwb/spec.py` | the protocol, as code - the host validates with it, the docs are rendered from it |
| `docs/ntwb/api.json`, `docs/ntwb/API.md` | the generated reference; `test_ntwb.py` fails when they drift |
| `docs/ntwb/NTWB.md` (this) | the why and the how, with examples |
| an app's `api.json` (manifest `api`) | that app's own methods, events, state keys and streams |

## 1. Why a bridge, and what was learned from Artboard

Arstro's apps are built "core first": a service with one way in (commands) and one way out
(a model plus events), so a GUI, a CLI, a control socket and tests are peers. Artboard, the
suite's UI library, applies the same idea to drawing: a platform-free core behind the smallest
possible seam (`IRenderTarget`), with one adapter per platform (Cairo, Canvas2D) and a
recording adapter for tests.

NTWB is that seam for **the whole application boundary**, not for drawing:

* **The app stays native** - its core, files, GPU and CPU budget stay on the device; only
  messages and encoded pixels travel.
* **One small seam** - JSON messages plus binary blobs over a byte stream. The app's own
  command/event vocabulary rides on top unchanged: NTWB never interprets an app's methods.
* **Portable by construction** - the protocol is plain framing + JSON, so an adapter is a few
  hundred lines in any language (Python: `arstro_remote.ntwb.app`; C++: arstro `core/Ntwb`),
  and the host side is independent of the app's toolkit. (A stream of the app's own draw calls
  would fit the seam too - and was rejected: it is a remote screen, not a view; section 2.)
* **Testable without a browser** - `apps.call` / `apps.state` reach an app from the CLI or a
  script by exactly the path a browser uses.

## 2. MVVM: the app is the model, every client is its own view (NTWB-11)

```
            one session = one app process                         each client (browser tab)
 ┌──────────────────────────────────────────┐      host      ┌──────────────────────────────────┐
 │ MODEL  the app's core: files, GPU, state │  ── state ──▶  │ VIEW-MODEL  derives from state,   │
 │        changes only through call/notify  │  ── event ──▶  │             holds the view's own  │
 │        publishes whole values + facts    │  ── blobs ──▶  │             state, sends intents  │
 │                                          │  ◀── call ───  │ VIEW        laid out for ITS      │
 └──────────────────────────────────────────┘  ◀── notify ─  │             screen (phone, desk)  │
                                                              └──────────────────────────────────┘
```

| lives in | what | travels? |
|---|---|---|
| the app (model) | the document, the edit target, parameters, history, jobs - everything a second view must agree on | yes: `state` (whole values), `event` (facts), blobs (pixels) |
| the client (view-model) | layout, which tab or panel is open, scroll, zoom and pan, hover, a drag in flight, a draft value | **never** - it is this view's, not the session's |
| the client (view) | the DOM, drawn from the view-model, suited to the screen | never |

Four rules follow, and the spec states them (`MVVM` in [API.md](API.md)):

1. **The model changes only through `call` / `notify`.** A view never edits `state`; it asks the
   app, and the new value comes back to *every* client of the session, the asker included.
2. **State is whole values.** A late client is complete from `ready`; two clients never need to
   agree about a delta.
3. **Anything a second view would re-derive is data.** Catalogues, ranges, unit conversions,
   labels: the app publishes them (a state key or a method), so two views cannot disagree.
4. **View state stays in the view.** Two clients of one session see the same model and nothing of
   each other's view state: one can be zoomed into a corner while the other shows the whole picture.

**Why not stream the app's window?** Because that is a remote screen: every client gets the same
pixels, laid out for one window size and one input device, and a menu opened on the desktop opens
on the phone. It was tried for Cosmo (the native window's draw calls replayed in a canvas) and
rejected for exactly that reason - see the Cosmo decisions log.

### Sessions (APP-04)

A **session** is one running process of an app - one model - and the clients attached to it.

* Every app has the session **`main`**. A client that names none joins it.
* An app whose manifest says **`"single": false`** may run more, side by side (at most
  `SESSIONS_PER_APP`, see *Limits* in API.md), each its own process with its own model:
  `/apps/<id>/?session=new` starts one; the host names it (`s2`, `s3` ... - never reused) and
  `ntwb.js` rewrites the page's URL to `?session=s2`, so a reload or a shared link joins that
  session. Two clients of one session see each other's edits; two sessions share nothing
  but the app's data dir.
* The app learns its session from `NTWB_SESSION` and `welcome.session`; a client from
  `ready.session`, and how many clients share it from `status.clients` - sent again whenever
  somebody joins or leaves (**presence**, NTWB-12; `app.onPresence(...)` in ntwb.js).
* A session runs until it is stopped or exits, clients or not. A stopped session other than
  `main` that nobody watches is forgotten.

## 3. The flow

```
 install          app writes  ~/.local/share/ntwb/apps/<id>/ntwb.json   (or: arstro-remote apps register DIR)
 list             host: Apps page / apps.list  - rescans on every list, no restart needed
 open             browser loads /apps/<id>/ (the app's web UI) + /ntwb/ntwb.js, opens WS /ws/app/<id>[?session=]
 launch           host starts manifest.exec with NTWB_SOCKET, NTWB_TOKEN, NTWB_SESSION ... (auto on first open)
 hello            app connects to NTWB_SOCKET, sends `hello` {ntwb, app, version, token}
 welcome          host answers `welcome` {session, clients}; `client.open` per waiting client
 ready            each browser gets `ready` {client, app, state, session}  (all retained state)
 presence         every client of the session gets `status` {state, session, clients} on each join / leave
 use              browser `call` -> host (rewrites the id, adds client) -> app -> `result` -> browser
                  browser `notify` -> app (no reply)
                  app `event` / `state` / blobs -> every client (or one)
 leave            browser closes -> app gets `client.close`; the app keeps running for the others
 stop             host sends `bye` -> app saves and exits (SIGTERM, then SIGKILL after 5 s each)
```

### Transport

* **App <-> host:** a Unix-domain stream socket, path in `NTWB_SOCKET`, mode 0600 in the user's
  runtime dir (one per host slot: `$XDG_RUNTIME_DIR/arstro-remote-<slot>-ntwb.sock`). Frames are
  `[type u8][length u32 BE][payload]`: type 1 = one JSON message, type 2 = a blob
  (`[header length u16 BE][JSON header][bytes]`).
* **Browser <-> host:** a WebSocket at `/ws/app/<id>` (`?session=<id>` joins a session,
  `?session=new` starts one), behind the host's normal password/cookie.
  A JSON message is a text frame, a blob a binary frame carrying the blob payload.

### Messages at a glance

| | |
|---|---|
| `hello`, `welcome` | the app's handshake (version check, token) |
| `ready`, `status` | what a client learns on connect: the app, its retained state, its session, whether the process runs and how many clients share the session |
| `client.open`, `client.close` | the app learns who is attached |
| `call`, `result` | request / reply; the host routes each result to the client that asked |
| `notify` | request without reply (drags, pointer moves) |
| `event` | something happened - to all clients or one |
| `state` | observable state; the host retains the newest value per key and replays it in `ready` |
| blobs | binary streams (JPEG previews, thumbnails, files); live streams set `coalesce` so slow clients skip frames |
| `log` | a line for the host's log of this app |
| `ping`, `pong` | liveness |
| `error` | a peer broke the protocol; the offending message was dropped |
| `bye` | orderly stop, either way |

## 4. Rules that make it trustworthy

1. **One definition.** A message, field or manifest key that is not in `spec.py` does not exist.
   Change `spec.py`, regenerate (`python3 -m arstro_remote.ntwb.spec --write docs/ntwb`), then
   implement - host, SDKs, apps. `test_ntwb.py` fails when the generated files, `ntwb.js` or this
   narrative fall behind; the C++ client checks its table against the vendored `api.json`.
2. **Unknown input is rejected, never accepted quietly.** An unknown message type, an unknown or
   missing field, a wrong type, a message in the wrong direction: the receiver drops it and
   answers `error` (a bad `call` from a browser is answered with `result` ok=false). A call to a
   method the app's API description does not list is refused by the host.
3. **Versions.** `MAJOR.MINOR.PATCH`. A peer refuses a different MAJOR in `hello`. A MINOR adds
   optional fields or messages; each side sends only what the lower version defines.
4. **The host owns ids.** A client's call id is only unique for that client; the host rewrites it
   towards the app and back. A client never sets `client`.
5. **State is whole values, events are facts.** `state` carries the complete new value of a key
   (a model dump, a counter), so a late client is complete from `ready` alone; `event` says what
   happened (and may be missed by a client that was not there).
6. **Slow clients never slow the app.** Each client has a send queue. A blob marked `coalesce`
   (a live preview) is replaced by a newer blob of the same stream while it waits; every other
   blob (thumbnails, files) and every JSON message is delivered (a client too far behind is closed).
7. **Security.** App pages and `/ws/app/<id>` need the host's password; the app socket is 0600;
   a launched app must present its one-time `NTWB_TOKEN`; an app may attach without one only if
   its manifest declares the `attach` capability. An installed app is trusted like any program
   the user installs - its web UI runs same-origin with the host.

## 5. Writing an app adapter

### Manifest (`~/.local/share/ntwb/apps/<id>/ntwb.json`)

```json
{
  "ntwb": "1.1.0",
  "id": "hello",
  "name": "Hello NTWB",
  "version": "1.0.0",
  "description": "The example app",
  "icon": "icon.svg",
  "exec": ["hello_app.py"],
  "web": "web",
  "api": "api.json",
  "capabilities": ["blobs"],
  "single": false
}
```

Relative paths are relative to the manifest's directory. The installer of an app writes this
file (or `arstro-remote apps register <dir>` records a manifest kept elsewhere). `single: false`
lets clients start sessions of their own; leave it out for an app with one shared model.

### Python

```python
from arstro_remote.ntwb.app import App, MethodError
app = App("hello", "1.0.0", capabilities=["blobs"])

@app.method("add")
def add(params, client):
    counter["n"] += params["n"]
    app.state("counter", counter["n"])            # every client sees it, late ones in `ready`
    app.event("added", {"n": params["n"], "by": client})
    return counter["n"]                           # -> result ok=true data=...

app.run()                                         # $NTWB_SOCKET, $NTWB_TOKEN from the host
```

The complete example (counter, echo, failing method, PNG blob stream, quit, web UI) is
`server/examples/ntwb/hello/`.

### C++ (arstro `core/Ntwb`)

`ntwb::Codec` (platform-free framing + JSON), `ntwb::ITransport` (the seam; `UnixSocketTransport`
on POSIX) and `ntwb::Client` (handshake, dispatch of `call`/`notify`, `result`, `event`, `state`,
`blob`, `log`). Cosmo's adapter (`cosmo-cc ntwb`) is the worked example.

### The web UI - a view and a view-model, not a picture of the window

Any static files; load the SDK from the host. Build it the MVVM way (section 2): keep a
view-model in the page that derives everything shown from `onState`, holds the page's own view
state, and turns the user's intent into `call`s; lay the view out for the screen it runs on.

```html
<script src="/ntwb/ntwb.js"></script>
<script>
  const app = NTWB.connect();                    // app id from the URL /apps/<id>/
  app.onState("counter", (n) => show(n));
  app.onEvent("added", (d) => note(d));
  app.onBlob("picture", (blob, h) => img.src = URL.createObjectURL(blob));
  app.call("add", {n: 1});
  app.onPresence(({session, clients}) => badge(`${clients} viewing ${session}`));
</script>
```

The page may be opened on its own (`/apps/<id>/`) or inside the host's Apps page (an iframe of
the same origin); it should work at 360 px width.

### An app's API description (`api.json`)

Lists the app's methods (with params and result), events, state keys and streams. The host
refuses calls to unlisted methods, serves it at `/apps/<id>/api.json` and via `apps.api`, so an
agent or a script discovers an app without reading its source.

## 6. Reaching an app without a browser

```bash
arstro-remote apps list
arstro-remote apps call hello add '{"n": 2}'        # the same path a browser call takes
arstro-remote apps state hello counter
arstro-remote apps sessions hello                    # main, s2 ...: state and viewers of each
arstro-remote apps launch hello --session new        # start another session
arstro-remote apps call hello add '{"n": 1}' --session s2
arstro-remote apps log hello
arstro-remote apps spec                              # this protocol's reference (API.md)
```

## 7. Versions

| version | date | what |
|---|---|---|
| 1.0.0 | 2026-09-30 | first version: manifest + launch, handshake, call/result/notify, event/state, blobs, log, ping, error, bye |
| 1.1.0 | 2026-09-30 | MVVM stated as the contract (NTWB-11); sessions: `single: false`, `?session=<id>\|new`, `NTWB_SESSION`, `welcome.session` names the session, `ready.session`; presence: `status.session` + `status.clients` (NTWB-12); `LIMITS` |
