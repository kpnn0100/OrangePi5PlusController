# NTWB 1.0.0 - API reference

**Generated** from `server/arstro_remote/ntwb/spec.py` - do not edit; run `python3 -m arstro_remote.ntwb.spec --write docs/ntwb` (a test fails when this file drifts).
The narrative (flow, rationale, examples) is in [NTWB.md](NTWB.md).

## Framing (app <-> host, Unix socket)

`[type u8][length u32 big-endian][payload]`, payload at most 67108864 bytes; blob header at most 16384 bytes.

| frame | type | payload |
|---|---|---|
| json | 1 | one UTF-8 JSON object: a message from MESSAGES |
| blob | 2 | [header length u16 BE][header: UTF-8 JSON object, see BLOB_HEADER][data bytes] |

Browser <-> host: WebSocket `/ws/app/<id>` - JSON messages as text frames, blobs as binary frames carrying the blob payload.

## Roles

- **host** - Arstro Remote: registers apps, launches them, serves their web UI and relays between app and clients
- **app** - a native program with an NTWB adapter; it keeps its core and state, the host never interprets its methods
- **client** - a browser page (the app's web UI, with ntwb.js) or any other front end that speaks the WebSocket side

## Types

- `str` - a JSON string
- `int` - a JSON integer
- `num` - a JSON number
- `bool` - true or false
- `obj` - a JSON object
- `any` - any JSON value, null included
- `list[str]` - a JSON array of strings
- `id` - string of 1-64 characters from A-Z a-z 0-9 . _ : -
- `name` - string: a lower-case letter, then up to 63 of a-z 0-9 . _ -
- `version` - string: semantic version MAJOR.MINOR.PATCH
- `enum` - one of the listed strings

## Messages

Every message is a JSON object whose `t` is its type. A field not listed is an error, so is a missing required one; the receiver drops the message and answers `error`.

| message | directions |
|---|---|
| [`hello`](#msg-hello) | app -> host (socket) |
| [`welcome`](#msg-welcome) | host -> app (socket) |
| [`ready`](#msg-ready) | host -> client (WebSocket) |
| [`status`](#msg-status) | host -> client (WebSocket) |
| [`client.open`](#msg-client-open) | host -> app (socket) |
| [`client.close`](#msg-client-close) | host -> app (socket) |
| [`call`](#msg-call) | client -> host (WebSocket), host -> app (socket) |
| [`result`](#msg-result) | app -> host (socket), host -> client (WebSocket) |
| [`notify`](#msg-notify) | client -> host (WebSocket), host -> app (socket) |
| [`event`](#msg-event) | app -> host (socket), host -> client (WebSocket) |
| [`state`](#msg-state) | app -> host (socket), host -> client (WebSocket) |
| [`log`](#msg-log) | app -> host (socket) |
| [`ping`](#msg-ping) | host -> app (socket) |
| [`pong`](#msg-pong) | app -> host (socket) |
| [`error`](#msg-error) | host -> app (socket), host -> client (WebSocket) |
| [`bye`](#msg-bye) | app -> host (socket), host -> app (socket) |

<a id="msg-hello"></a>
### `hello`

First message on an app connection. The host answers `welcome`, or `error` and closes.  
Directions: app -> host (socket)

| field | type | required | meaning |
|---|---|---|---|
| `ntwb` | `version` | yes | protocol version the app speaks; the MAJOR must equal the host's |
| `app` | `name` | yes | the app id, as in its manifest |
| `version` | `str` | yes | the app's own version (shown to users) |
| `token` | `str` | no | NTWB_TOKEN from the environment when the host launched the app; an app started by hand may attach without one if its manifest allows `attach` |
| `pid` | `int` | no | process id, for the host's process view |
| `capabilities` | `list[str]` | no | optional features the app uses: see CAPABILITIES |

<a id="msg-welcome"></a>
### `welcome`

The host accepted `hello`. From here on both sides may send any message of their direction.  
Directions: host -> app (socket)

| field | type | required | meaning |
|---|---|---|---|
| `ntwb` | `version` | yes | protocol version of the host |
| `session` | `id` | yes | this app connection |
| `host` | `obj` | yes | {name, version} of the host |
| `clients` | `list[str]` | yes | clients already waiting for the app; each also gets `client.open` |

<a id="msg-ready"></a>
### `ready`

The app is connected. Carries the app's retained state, so a client is complete at once.  
Directions: host -> client (WebSocket)

| field | type | required | meaning |
|---|---|---|---|
| `ntwb` | `version` | yes | protocol version of the host |
| `client` | `id` | yes | this client's id (what the app sees in `call.client`) |
| `app` | `obj` | yes | {id, name, version} of the connected app |
| `state` | `obj` | yes | every retained state key -> its latest data (see `state`) |

<a id="msg-status"></a>
### `status`

Where the app's process is: sent on connect and on every change.  
Directions: host -> client (WebSocket)

| field | type | required | meaning |
|---|---|---|---|
| `state` | `enum:starting\|running\|stopped\|failed` | yes | starting = launched, waiting for `hello` |
| `detail` | `str` | no | why (exit code, launch error, ...) |

<a id="msg-client-open"></a>
### `client.open`

A client attached to the app.  
Directions: host -> app (socket)

| field | type | required | meaning |
|---|---|---|---|
| `client` | `id` | yes | the client |
| `info` | `obj` | no | {peer, controller}: where it connects from |

<a id="msg-client-close"></a>
### `client.close`

A client went away; per-client resources (subscriptions, streams) can be freed.  
Directions: host -> app (socket)

| field | type | required | meaning |
|---|---|---|---|
| `client` | `id` | yes | the client |

<a id="msg-call"></a>
### `call`

A request. The host adds `client` and forwards it to the app; the app must answer with `result` carrying the same `id`. Methods are the app's own API (its manifest `api` lists them).  
Directions: client -> host (WebSocket), host -> app (socket)

| field | type | required | meaning |
|---|---|---|---|
| `id` | `id` | yes | chosen by the caller, unique among its open calls |
| `method` | `name` | yes | the app method |
| `params` | `obj` | no | the method's arguments (default {}) |
| `client` | `id` | no | set by the host towards the app; a client never sends it |

<a id="msg-result"></a>
### `result`

The answer to one `call`. The host routes it to the client that made the call.  
Directions: app -> host (socket), host -> client (WebSocket)

| field | type | required | meaning |
|---|---|---|---|
| `id` | `id` | yes | the call's id |
| `ok` | `bool` | yes | false = the call failed; `error` says why |
| `data` | `any` | no | the method's result when ok |
| `error` | `str` | no | a sentence a user understands, when not ok |

<a id="msg-notify"></a>
### `notify`

A request without a reply (pointer moves, slider drags). Same routing as `call`.  
Directions: client -> host (WebSocket), host -> app (socket)

| field | type | required | meaning |
|---|---|---|---|
| `method` | `name` | yes | the app method |
| `params` | `obj` | no | the method's arguments (default {}) |
| `client` | `id` | no | set by the host towards the app |

<a id="msg-event"></a>
### `event`

Something happened. To every client of the app, or to one when `client` is set.  
Directions: app -> host (socket), host -> client (WebSocket)

| field | type | required | meaning |
|---|---|---|---|
| `name` | `name` | yes | the event's name in the app's API |
| `data` | `any` | no | payload |
| `client` | `id` | no | towards the host only: deliver to this client alone |

<a id="msg-state"></a>
### `state`

Observable state. The host retains the newest `data` per `key` and replays all keys in `ready`, so a client that (re)connects is complete without asking. `data: null` deletes the key.  
Directions: app -> host (socket), host -> client (WebSocket)

| field | type | required | meaning |
|---|---|---|---|
| `key` | `name` | yes | state key in the app's API |
| `data` | `any` | yes | the whole new value (not a delta) |

<a id="msg-log"></a>
### `log`

A line for the host's log of this app (System > Logs, `arstro-remote apps log`).  
Directions: app -> host (socket)

| field | type | required | meaning |
|---|---|---|---|
| `level` | `enum:debug\|info\|warning\|error` | yes | severity |
| `msg` | `str` | yes | the line; never secrets |

<a id="msg-ping"></a>
### `ping`

Liveness check; the app answers `pong` with the same `n`.  
Directions: host -> app (socket)

| field | type | required | meaning |
|---|---|---|---|
| `n` | `int` | yes | sequence number |

<a id="msg-pong"></a>
### `pong`

Answer to `ping`.  
Directions: app -> host (socket)

| field | type | required | meaning |
|---|---|---|---|
| `n` | `int` | yes | the ping's number |

<a id="msg-error"></a>
### `error`

The peer sent something the protocol does not allow (unknown message, bad field, unknown method...). The offending message was dropped.  
Directions: host -> app (socket), host -> client (WebSocket)

| field | type | required | meaning |
|---|---|---|---|
| `error` | `str` | yes | what was wrong |
| `about` | `str` | no | the offending message type or call id |

<a id="msg-bye"></a>
### `bye`

Orderly end. From the host: the app should save and exit (it is killed after STOP_GRACE_S). From the app: it is about to exit.  
Directions: app -> host (socket), host -> app (socket)

| field | type | required | meaning |
|---|---|---|---|
| `reason` | `str` | no | why |

## Blob header

Binary data (images, audio, files) without base64. App -> host -> clients only.

Directions: app -> host (socket), host -> client (WebSocket)

| field | type | required | meaning |
|---|---|---|---|
| `stream` | `name` | yes | which stream in the app's API, e.g. `preview`, `thumb` |
| `mime` | `str` | yes | media type of the data, e.g. image/jpeg |
| `meta` | `obj` | no | stream-specific metadata (size, sequence, the call it answers ...) |
| `client` | `id` | no | towards the host only: deliver to this client alone |
| `coalesce` | `bool` | no | true for live streams (previews): while this blob waits in a slow client's queue, a newer blob of the same stream replaces it. Default false: every blob is delivered (thumbnails, files) |

## Capabilities

- `blobs` - the app sends binary streams
- `attach` - the app may be running before the host (started by hand) and attach to it

## Environment of a launched app

| variable | meaning |
|---|---|
| `NTWB_SOCKET` | path of the host's Unix socket to connect to |
| `NTWB_TOKEN` | one-time token to send in `hello` |
| `NTWB_APP_ID` | the manifest id the host launched |
| `NTWB_VERSION` | the host's protocol version |
| `NTWB_HOST` | name of the host program (arstro-remote) |
| `NTWB_DATA_DIR` | a writable directory for the app's own data under the host's state dir |

## Manifest (`ntwb.json`)

<data dir>/ntwb/apps/<id>/ntwb.json - searched in $XDG_DATA_HOME (default ~/.local/share), then each of $XDG_DATA_DIRS (default /usr/local/share:/usr/share); the first wins

| field | type | required | meaning |
|---|---|---|---|
| `ntwb` | `version` | yes | protocol version the app speaks |
| `id` | `name` | yes | unique app id; also the directory name and the URL path /apps/<id>/ |
| `name` | `str` | yes | display name |
| `version` | `str` | yes | the app's version |
| `description` | `str` | no | one sentence for the app list |
| `icon` | `str` | no | path of an SVG/PNG icon, relative to the manifest directory |
| `exec` | `list[str]` | yes | argv to launch the adapter; first element absolute or relative to the manifest dir |
| `cwd` | `str` | no | working directory (default: the manifest directory) |
| `env` | `obj` | no | extra environment variables (string values) |
| `web` | `str` | yes | directory with the web UI (index.html), relative to the manifest directory |
| `api` | `str` | no | path of the app's API description (see APP_API), relative to the manifest directory |
| `capabilities` | `list[str]` | no | see CAPABILITIES |
| `single` | `bool` | no | one process for all clients (default true; false is reserved) |

## App API description (manifest `api`)

When an app ships one, the host refuses calls and notifies to methods it does not list.

| field | type | required | meaning |
|---|---|---|---|
| `ntwb` | `version` | yes | protocol version |
| `app` | `name` | yes | app id |
| `methods` | `obj` | yes | method name -> {doc, params: {name: {type, required, doc}}, result: doc} |
| `events` | `obj` | no | event name -> {doc, data: doc} |
| `state` | `obj` | no | state key -> {doc, data: doc} |
| `streams` | `obj` | no | blob stream -> {doc, mime, meta: doc} |

## Timings

| name | value |
|---|---|
| `HELLO_TIMEOUT_S` | 20 |
| `STOP_GRACE_S` | 5 |
| `PING_INTERVAL_S` | 15 |
| `PONG_TIMEOUT_S` | 30 |
