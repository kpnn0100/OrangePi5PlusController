# Arstro Devices

Every device of your Arstro device managers in one Android app (requirements MGR-01..06 in
`docs/requirements.md`). The device list and the device pages are native; a device's own pages -
its web UI with screen, terminal and files, a backup terminal, a phone's page, any other port -
open inside the app, end-to-end encrypted with the device's access key.

* **Add a device manager:** type its address; the domain is enough
  (`arstro.example.com` -> `https://arstro.example.com/arstro/internal/devices`). A viewer password
  is asked only when it has one. Switch or edit device managers from the title.
* **Open a device:** *Open* (its web port) or the device page (any port, the ports it listens on).
  The first time the app asks for the device's access key: `arstro-remote relay key` on a machine
  (`arstro-remote-a` / `-b` for a slot), `arstro-backup key` for a backup terminal, the access key
  shown in Arstro Remote Client on a phone. Keys stay on the phone, encrypted.
* **Recent:** every port that answered, newest first, one tap away.

How it works: `docs/architecture.md` (Arstro Devices) and `docs/relay/PROTOCOL.md` section 5b.

## Build and test

```
scripts/build_apk.sh --devices          # analyze, widget tests, JVM tests, release APK -> release/
cd devices_app && flutter test          # the native pages against a fake Kotlin side
cd devices_app/android && ./gradlew :app:testDebugUnitTest
                                        # the viewer and the gateway against the real device manager
                                        # (python3 -m arstro_relay) and tool/fake_device.py
```

`android/.../core/E2e.kt` and `Transport.kt` are client_app's files with another package line:
change both (a JVM test compares them). On a phone or emulator, a throwaway device manager and
`tool/fake_device.py --relay <url> --token <token> --key-file <file>` give a device to open.
