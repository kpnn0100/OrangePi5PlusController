# Arstro Remote Client

Shares this Android phone through your own Arstro device manager: from any browser you pick the
phone in the device list, give its access key and get its screen (view and control), camera,
internet calls, find my phone, messages, files, notifications and more - only the features you
switch on in the app. Controlling a Pi is the other app, Arstro Remote (`../app`).

* Design, API, features, Android limits: [docs/phone/PHONE.md](../docs/phone/PHONE.md)
* Build the signed APK: `scripts/build_apk.sh --client` (same release key as Arstro Remote)
* Tests: `flutter test`, `cd android && ./gradlew :app:testDebugUnitTest`,
  `node phone_web_test/ui_test.mjs`, and `server/tests/test_phone_e2e.py` on an emulator
