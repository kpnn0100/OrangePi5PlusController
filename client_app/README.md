# Arstro Remote Client

Manages this Android phone (Android 12+) from a browser: the app runs a web server on the phone -
`http://<phone>:8080/`, and `https://<phone>:8443/` for live video - behind the password shown in the
app, and optionally also through your own Arstro device manager. You get the front and back camera,
find and ring the phone, its screen (view and control), device controls (ringer, Do Not Disturb,
Bluetooth, alarms, sensors, ...), internet calls, messages, files, notifications and more - only the
features you switch on in the app. Controlling a Pi is the other app, Arstro Remote (`../app`).

* Design, API, features, Android limits: [docs/phone/PHONE.md](../docs/phone/PHONE.md)
* Build the signed APK: `scripts/build_apk.sh --client` (same release key as Arstro Remote)
* Tests: `flutter test`, `cd android && ./gradlew :app:testDebugUnitTest`,
  `node phone_web_test/ui_test.mjs`, and `server/tests/test_phone_e2e.py` on an emulator
