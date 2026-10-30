# Arstro on Android: the phone as a device (PHN-01..)

The **Arstro Remote Client** app (`client_app/`, package `com.arstro.arstro_remote_client`)
**shares this phone**: a foreground service makes the phone a device of the device manager, exactly
like an Arstro Remote machine (`docs/relay/`). It is its own app, apart from Arstro Remote (`app/`,
the remote control of a Pi): a phone can have either or both, and only the client asks for the
phone's sensitive permissions. From any browser you
open the device list, pick the phone, give its access key, and get the phone's own web UI:
screen view and control, camera, internet calls, find my phone, messages, files and more.

```
 browser (device manager viewer) ══ end-to-end encrypted channel ══ phone: PhoneService
                                                                       ├─ RelayAgent  (relay/PROTOCOL.md, device side)
   B/d/<phone id>/8080/            http / ws streams ───────────────► ├─ Router      (in-process: no local port)
                                                                       │    ├─ assets/phone/   the web UI
                                                                       │    ├─ POST /api/op/<op>, GET /api/..., /ws/...
                                                                       │    └─ Features    screen, camera, call, ...
 the app's page (Flutter)  ──────────── MethodChannel arstro/phone ──┘    (each: permissions, ops, routes, sockets)
```

**No local server.** The relay agent hands every http / ws stream of a channel straight to the
in-process `Router`. Nothing listens on a port, so other apps on the phone cannot use the
features (a local port would let any app drive the accessibility-based remote control). The
device advertises `web: 8080` and its `ready` says `ports: [8080]`: only port 8080 reaches the
router (another port: "port N is not allowed on this device"), and `tcp` streams are refused
("tcp is not available on a phone") - so the relay client's `forward` does not work with a phone;
agents use `http` (docs/relay/AGENT_API.md).

## Code map

| Path | Owner | What |
|---|---|---|
| `client_app/android/app/src/main/kotlin/com/arstro/arstro_remote_client/core/Api.kt` | contract | `Feature`, `Request`, `Response`, `WsSession`, `WsHandler`, `PhoneContext`, `EventBus`, `SpecialAccess`, `OpError` |
| `.../arstro_remote_client/core/` | core | `PhoneService` (foreground service), `RelayAgent` + `E2e` + transports (WebSocket, HTTP pipes), `Router`, `Store` (identity, settings, feature switches), `BootReceiver`, `TrampolineActivity`, `PhoneBridge` (MethodChannel for Flutter), `Features.kt` (the registry) |
| `.../arstro_remote_client/features/` | features | one file per feature + `ControlService` (accessibility), `NotifListener`, `AdminReceiver`, `SmsReceiver`, `LostModeActivity`, `CallActivity` |
| `client_app/android/app/src/main/assets/phone/` | web | the phone's web UI (served by the Router at `/`) |
| `client_app/lib/` | core | Flutter: the app's page, first-run wizard |
| `client_app/android/app/src/test/` | core / features | JVM unit tests (crypto against `docs/relay/vectors.json`, framing, router) |

## Features (ids are stable)

| id | Title | Runtime permissions | Special access | FGS type while used |
|---|---|---|---|---|
| `status` | Phone status | - | - | - |
| `screen` | Screen view & control | - | `SCREEN_CAPTURE` (consent), `ACCESSIBILITY` | mediaProjection |
| `camera` | Camera | `CAMERA`, `RECORD_AUDIO` (video sound) | - | camera, microphone |
| `call` | Internet calls | `RECORD_AUDIO`, `POST_NOTIFICATIONS` | `OVERLAY`, full-screen intent | microphone |
| `mic` | Listen | `RECORD_AUDIO` | - | microphone |
| `messages` | Messages (SMS) | `READ_SMS`, `SEND_SMS`, `RECEIVE_SMS` | - | - |
| `contacts` | Contacts & call log | `READ_CONTACTS`, `READ_CALL_LOG` | - | - |
| `find` | Find my phone | `ACCESS_FINE_LOCATION`, `ACCESS_COARSE_LOCATION`, `ACCESS_BACKGROUND_LOCATION` | `DND`, `DEVICE_ADMIN`, `OVERLAY` | location |
| `files` | Files | - | `ALL_FILES` | - |
| `notifications` | Notifications | - | `NOTIFICATION_LISTENER` | - |
| `clipboard` | Clipboard | - | - | - |
| `apps` | Apps | - | `OVERLAY` (open from background) | - |
| `tools` | Audio & tools (volume, media keys, speak, torch, vibrate, brightness, open URL) | - | `OVERLAY`, `WRITE_SETTINGS` (brightness) | - |
| `shell` | Shell (the app's sandbox) | - | - | - |

Plus, for the service itself: `POST_NOTIFICATIONS`, `BATTERY_UNRESTRICTED` (ignore battery
optimizations). A feature is **on** when its switch is on in the app; it is **ready** when also
every permission / access it lists is granted. Ops of a feature that is off answer 403
`"<title> is turned off on the phone"`; not ready: 409 `"<title> needs <permission> - allow it in
the app"`. Switches are changed only on the phone (the web shows them read-only).

## Background rules (Android 14-16) the core handles

* The service runs with type `specialUse` (+ `location` when `find` is ready) from boot; it adds
  `camera` / `microphone` / `mediaProjection` only while those are in use. A while-in-use type
  cannot be added while the app is in the background, so `PhoneContext.foreground(types)` first
  brings the app to the foreground for a moment through the transparent `TrampolineActivity`
  (allowed because the app holds "display over other apps"), then calls `startForeground` with the
  types, then finishes the activity.
* Screen capture: the consent ("Start now") is asked through `TrampolineActivity`. The
  `MediaProjection` and its single `VirtualDisplay` are kept for as long as the service runs; with
  nobody watching the display's surface is set to `null` (nothing is captured or encoded). With
  `adb shell appops set com.arstro.arstro_remote_client PROJECT_MEDIA allow` the consent is automatic.

## Conventions (all of the API below)

* Times (`date`, `time`, `since`, `mtime`) are **milliseconds** since the epoch.
* `battery.level` is a percent 0..100; `signal.level` 0..4; volume `level` 0..1.
* Video `rotation`: degrees the viewer turns the picture clockwise to show it upright - always 0
  for the screen (frames are upright; the display's own rotation is in `screen.status`), the
  sensor orientation for the camera. Tap coordinates are 0..1 of the picture as sent.
* File paths are relative to the storage root (`/`, `/DCIM/Camera`); absolute ones are accepted.
* A refused WebSocket opens and closes at once with code 4000 + the HTTP status (4403 off, 4409 not
  ready, 4404 unknown path, 4500 crash) and the reason as the close reason; a feature's own
  problems on an open socket come as a text `{"t": "error", "error"}`.
* Ops answer JSON; a few answer bytes (`apps.icon` PNG). A `SecurityException` inside an op is a
  409 with a short reason, anything else a 500 "`<op>` failed: ...".
* Typing keys `enter` and `backspace` are accepted on `screen.key` and `/ws/screen` too (they edit
  the focused field like `text` "\n" / "\b").

## The phone's API (what the web UI and agents use)

All paths are relative to the device root (`B/d/<id>/8080/`). JSON bodies; errors
`{"ok": false, "error": "..."}` with the status above.

| Route | What |
|---|---|
| `GET /` , `GET /static/<file>` | the web UI (assets `phone/`) |
| `GET /api/info` | `{name, model, manufacturer, android, sdk, app_version, id, features: [{id, title, on, ready, missing: [names], note}]}` |
| `POST /api/op/<op>` | `{"ok": true, "data": ...}` - the ops below |
| `GET /ws` | events (text JSON): `{"ev": "state", "topic", "data"}` (retained topics, all sent on connect) and `{"ev": "<name>", ...}` |
| `GET /ws/screen` | screen video (see *Video*) + control messages |
| `GET /ws/camera?facing=back\|front&size=1280x720` | camera video (see *Video*) |
| `GET /ws/mic` | microphone audio (see *Audio*) |
| `GET /ws/call` | an internet call (see *Call*) |
| `GET /api/files/list?path=P` | `{path, parent, entries: [{name, path, dir, size, mtime}]}` |
| `GET /api/files/download?path=P` | the file (`Content-Disposition: attachment`, `Content-Length`, `Range` → 206) |
| `PUT /api/files/upload?dir=D&name=N[&overwrite=1]` | raw body (`Content-Length` required) → `{ok, path}` |
| `GET /api/camera/photo?facing=back\|front` | a JPEG taken now |
| `GET /api/screen/shot` | a PNG of the screen now (for agents) |

**Ops** (`POST /api/op/<op>`):

| Op | Args | Result |
|---|---|---|
| `status.get` | | `{battery: {level, charging, plugged, temperature_c, health}, storage: {total, free}, memory: {total, available}, network: {type, connected, wifi_ssid?, ip?}, signal?: {type, level}, uptime_s, screen_on, locked}` |
| `screen.status` | | `{consent: bool, watching: n, width, height, rotation, control: bool}` |
| `screen.start` / `screen.stop` | | asks for consent on the phone if needed → `{consent, waiting}` / stops capture and releases the projection |
| `screen.tap` / `screen.long` | `x, y` (0..1 of the screen) | |
| `screen.swipe` | `x1, y1, x2, y2, ms?` | |
| `screen.scroll` | `x, y, dy` (screen heights, + = down) | |
| `screen.text` | `text` | typed into the focused field |
| `screen.key` | `key`: `back home recents notifications quick_settings lock power_dialog wake` | |
| `camera.list` | | `[{facing, id, sizes: ["1920x1080", ...], flash}]` |
| `camera.torch` | `on` | |
| `camera.record` | `action: start\|stop, facing?` | `{recording, path?}` (MP4 in `Movies/Arstro/`) |
| `call.start` | `name?` (shown on the phone) | `{state: "ringing"}` - the phone rings; media through `/ws/call` |
| `call.end` | | |
| `messages.threads` | `limit?` | `[{thread, address, name?, snippet, date, unread}]` |
| `messages.list` | `thread? \| address?, limit?` | `[{id, address, name?, body, date, type: in\|out, read}]` |
| `messages.send` | `to, text` | `{sent: true}` |
| `contacts.list` | `q?, limit?` | `[{name, numbers: [..], emails?: [..]}]` |
| `contacts.calls` | `limit?` | `[{number, name?, type: in\|out\|missed\|rejected, date, duration_s}]` |
| `find.locate` | `fresh?` | `{lat, lon, accuracy_m, time, provider}` (`fresh`: wait up to 30 s for a new fix) |
| `find.history` | `limit?` | `[{lat, lon, accuracy_m, time}]` (a fix every 15 min while `find` is ready, kept 7 days) |
| `find.ring` / `find.ring_stop` | `seconds?` (default 60) | loud alarm, also on silent / do-not-disturb |
| `find.lost` / `find.lost_off` | `message, contact?` | locks the phone and shows the message (and a number to call) over the lock screen |
| `files.delete` / `files.mkdir` / `files.rename` | `path` / `path` / `path, name` | |
| `notifications.list` | | `[{key, package, app, title, text, time, can_reply, actions: [..]}]` |
| `notifications.dismiss` | `key` | |
| `notifications.reply` | `key, text` | |
| `clipboard.set` / `clipboard.get` | `text` / | `{text}` (get: only while the app is in the foreground - else 409 with the reason) |
| `apps.list` | | `[{package, label, system}]` |
| `apps.open` | `package` | |
| `apps.icon` | `package` | PNG (also `GET /api/apps/icon?package=P`) |
| `tools.volume` | `stream: media\|ring\|alarm\|call, level? (0..1)` | `{stream, level, max}` |
| `tools.media` | `action: play_pause\|next\|previous\|stop` | |
| `tools.speak` | `text, lang?` | |
| `tools.vibrate` | `ms?` | |
| `tools.brightness` | `level (0..1) \| auto` | |
| `tools.open_url` | `url` | |
| `shell.run` | `cmd, timeout_s?` | `{code, out}` (runs as the app, in its sandbox) |
| `features.list` | | the `features` of `/api/info` |

**Event topics** (`/ws`): `status` (every 30 s and on change), `features`, `screen`, `call`
(`{state: idle\|ringing\|active\|ended, reason?, since}`), `find` (`{ringing, lost}`); events
`sms` (`{address, name?, body, date}`), `notification` (`{key, app, title, text}` posted /
`{key, removed: true}`).

### Video (`/ws/screen`, `/ws/camera`)

The phone first sends a text `{"t": "config", "codec": "avc1.PPCCLL", "width", "height", "rotation"}`
(again on every format change), then **binary** messages `flags u8 (bit0 = key frame) ‖ pts_us u64 BE
‖ H.264 Annex-B access unit` (SPS/PPS in front of every key frame). The browser decodes with
WebCodecs (`VideoDecoder`, `avc: {format: "annexb"}`). Viewer → phone (text JSON):
`{"t": "keyframe"}`, `{"t": "quality", "max": 720|1080|..., "fps"?, "kbps"?}`, and on `/ws/screen`
the control messages `{"t": "tap"|"long", x, y}`, `{"t": "swipe", x1, y1, x2, y2, ms}`,
`{"t": "scroll", x, y, dy}`, `{"t": "text", text}`, `{"t": "key", key}` (same as the ops).
Encoding runs only while at least one viewer is connected.

### Audio (`/ws/mic`, and inside `/ws/call`)

Text `{"t": "config", "rate": 16000, "channels": 1, "format": "s16le"}`, then binary 20 ms frames
(640 bytes) of PCM. The browser plays them through Web Audio.

### Call (`/ws/call`)

`call.start` (or opening `/ws/call`) makes the phone ring (a full-screen "Arstro call" with
Answer / Decline, over the lock screen). Messages on `/ws/call`: phone → viewer text
`{"t": "state", "state": "ringing"|"active"|"ended", "reason"?}`, then while active binary PCM
(as *Audio*, the phone's microphone with echo cancellation); viewer → phone binary PCM (the
browser's microphone, same format) played on the phone's earpiece / speaker; text
`{"t": "end"}`. Video in a call: the viewer opens `/ws/camera?facing=front` alongside.

## The app's MethodChannel `arstro/phone` (Flutter ↔ Kotlin)

| Method | Args | Result |
|---|---|---|
| `state` | | `{running, relay: {state, error, url, name, id, transport}, key_id, first_run, features: [{id, title, on, ready, missing: [{name, kind: permission\|special}]}]}` |
| `setFeature` | `id, on` | state |
| `request` | `id` (feature) or `name` (one permission / special access) | after the user answered: state |
| `setRelay` | `url, token?, name?` | state |
| `key` / `rotateKey` | | `{key, key_id}` |
| `start` / `stop` | | state (the service) |
| `finishFirstRun` | | state |

The page listens to `arstro/phone/events` (EventChannel) for state changes. Extras: `openAppInfo`
(for "Allow restricted settings"); `state` also carries `enabled, sdk, default_url,
service.missing, relay.viewers, relay.token_set, relay.relay_version`, and each `missing` item a
`title`; `setRelay` keeps the stored token when given an empty one.

## Android limits worth knowing (seen on Android 16)

* **Restricted settings:** for an app installed from an APK, Android refuses to turn on its
  accessibility service until "Allow restricted settings" is chosen in App info (⋮). The wizard
  says so and has an App info button.
* **Force stop** (Settings › Apps › Force stop) switches the app's accessibility service off -
  remote control then needs Accessibility again. Swiping the app away from Recents does not.
* Screen sharing shows Android's red status-bar chip; camera and microphone their dots.
* **Uninstalling** (and clearing the app's data) is refused while the app is an active device admin
  (Find my phone's "lock now").
  Turning Find my phone off in the app gives the device admin back; or Settings › Security ›
  Device admin apps › Arstro Remote Client › Deactivate.
* **Play Protect** may refuse to install the APK from a browser, chat or file manager because it asks
  for SMS, accessibility and notification access ("enhanced fraud protection"). Installing over USB
  (`adb -s <serial> install -r <apk>`) is not refused. Arstro Remote (`app/`) asks for none of these.

## Building

`scripts/build_apk.sh --client` (analyze, Flutter + JVM tests, signed release APK in
`release/arstro-remote-client-v<version>.apk`). It is signed with the same key as Arstro Remote
(`app/android/key.properties`); its version is in `client_app/pubspec.yaml`.

Arstro Remote 2.4.0 had this built in; from 2.5.0 it is only in Arstro Remote Client. The client
makes its own identity (device id and access key): a phone that shared through 2.4.0 shows up as a
new device, and the old entry can be removed in the device manager.

## Testing

* JVM: `cd client_app/android && ./gradlew :app:testDebugUnitTest` (crypto against `docs/relay/vectors.json`,
  router, the relay agent against the real Python relay + client over WebSocket and HTTP pipes, the
  features' pure parts).
* Web UI: `node client_app/phone_web_test/ui_test.mjs` (mock phone) and `node client_app/phone_web_test/viewer_e2e.mjs`
  (through a real device manager).
* End to end with the real app: `server/tests/test_phone_e2e.py` (opt-in) on an emulator. Set-up
  without tapping through the wizard (debug build):
  ```
  A="adb -s emulator-5554"; P=com.arstro.arstro_remote_client
  $A install -r -g app-debug.apk && $A shell pm grant $P android.permission.ACCESS_BACKGROUND_LOCATION
  for op in SYSTEM_ALERT_WINDOW MANAGE_EXTERNAL_STORAGE WRITE_SETTINGS PROJECT_MEDIA USE_FULL_SCREEN_INTENT ACCESS_RESTRICTED_SETTINGS; do $A shell appops set $P $op allow; done
  $A shell cmd notification allow_listener $P/$P.features.NotifListener; $A shell cmd notification allow_dnd $P
  $A shell dpm set-active-admin $P/.features.AdminReceiver; $A shell dumpsys deviceidle whitelist +$P
  $A shell settings put secure enabled_accessibility_services $P/$P.features.ControlService; $A shell settings put secure accessibility_enabled 1
  # shared_prefs/arstro_phone.xml (run-as): url, token, name, enabled=true, first_run=false, feature.<id>=true; then reboot (starts at boot)
  ```
  The emulator reaches a device manager on the host at `http://10.0.2.2:<port>/...`; SMS, calls and
  GPS come from `adb emu sms send`, `adb emu gsm call`, `adb emu geo fix`. `PROJECT_MEDIA allow` also
  makes the screen-capture consent automatic on a real phone (once, over USB debugging).
