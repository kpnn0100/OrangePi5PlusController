"""NTWB - native-to-web bridge (NTWB-01..10, APP-01..08).

A native program ("app") keeps its core where it runs and exposes it to web clients through
Arstro Remote ("host"): the host launches it, serves its web UI and relays messages and
binary streams between the app and every browser that opens it.

    spec.py      the protocol, defined once (validation + generated docs/ntwb/API.md, api.json)
    wire.py      framing (shared by host and SDK)
    registry.py  app manifests (installed ntwb.json files)
    host.py      AppsService: the apps.* ops, launching, the app socket, the client relay
    app.py       the Python SDK for writing an app adapter (reference implementation)
"""
