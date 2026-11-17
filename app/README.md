# Arstro Remote (Android)

Controls a machine running the Arstro Remote server **over Bluetooth only** (CON-06): no network
permission in the release build, nothing reaches the machine but the paired RFCOMM link.

- **Dashboard** - live stats.
- **Recorder** - the signal, the record button and tally, timecode, slate and markers, storage and
  the disk test, the recording settings; the gallery as a list (notes, good takes, conversions,
  checks, delete, the editor's CSV into Downloads/Arstro).
- **Wi-Fi** - status, networks, connect, forget, radio - and the machine's **hotspot** (WIFI-08):
  start it where there is no router, join it from a laptop or this phone, open the web address.
- **Terminal** - the machine's shared shells, full screen, copy / paste.
- **Remote** - touchpad, mouse buttons and keyboard (Win+L locks the desktop).
- **Settings** - pairing, web access (the address to open in a browser), connected controllers.

The live picture, thumbnails, playback and downloads, the remote screen, the apps' web UIs and the
other A/B slots are the web UI's - open it in the phone's browser (Settings › Web access, or the
hotspot's *Open web UI*).

Build: `scripts/build_apk.sh` (analyze, tests, signed release APK into `release/`). Needs Flutter
3.38.5 or newer (Dart 3.10.4). Debug builds also accept `tcp:HOST:PORT` as the address (through
`adb reverse` and an SSH tunnel to the daemon's socket) for testing without Bluetooth.
