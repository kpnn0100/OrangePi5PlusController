#!/usr/bin/env python3
"""Unit tests of the pairing / authorization policy (no radio needed).

Drives the real Agent and Bluetooth classes from arstro_remote.bluez with a fake
adapter, on a private D-Bus-less object, through the GLib main loop.

    python3 tests/test_agent_policy.py
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import dbus.mainloop.glib  # noqa: E402
from gi.repository import GLib  # noqa: E402

from arstro_remote import bluez  # noqa: E402

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
DEV = "/org/bluez/hci0/dev_11_22_33_44_55_66"


class FakeProps:
    def __init__(self, store):
        self.store = store
        self.sets = []

    def Get(self, _iface, name):
        return self.store.get(name, False)

    def Set(self, _iface, name, value):
        self.sets.append((name, bool(value)))
        self.store[name] = bool(value)


class FakeBT(bluez.Bluetooth):
    """Bluetooth with D-Bus replaced by dictionaries."""

    def __init__(self):  # no super().__init__: no bus, no signal receivers
        self.config = {}
        self.adapter_path = "/org/bluez/hci0"
        self.pairing_until = 0.0
        self._pair_timer = None
        self._reassert_timer = None
        self._to_trust = set()
        self.devices = {}
        self.adapter = FakeProps({"Discoverable": False, "Pairable": False})
        self.trusted_calls = []

    def device_props(self, path):
        return dict(self.devices.get(path, {}))

    def device_label(self, path):
        return path.rsplit("/", 1)[-1]

    def _adapter_props(self):
        return self.adapter

    def _trust(self, path):
        self.trusted_calls.append(path)
        self.devices.setdefault(path, {})["Trusted"] = True

    def _reassert_agent(self):
        return False


class AgentUnderTest(bluez.Agent):
    def __init__(self, bt):  # no bus registration
        self.bt = bt


def authorize(agent, uuid, wait=3.0):
    """Call AuthorizeService like bluetoothd would; returns 'ok' or the error name."""
    result = {}
    loop = GLib.MainLoop()

    def reply():
        result["r"] = "ok"
        loop.quit()

    def error(e):
        result["r"] = e.get_dbus_name()
        loop.quit()

    # dbus-python's async_callbacks: call the undecorated function directly
    bluez.Agent.AuthorizeService.__wrapped__(agent, DEV, uuid, reply, error) \
        if hasattr(bluez.Agent.AuthorizeService, "__wrapped__") else \
        agent.AuthorizeService(DEV, uuid, reply=reply, error=error)
    if "r" not in result:
        GLib.timeout_add(int(wait * 1000), loop.quit)
        loop.run()
    return result.get("r", "timeout")


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def fresh(window_open, dev=None):
    bt = FakeBT()
    if window_open:
        bt.pairing_until = time.monotonic() + 60
    if dev is not None:
        bt.devices[DEV] = dev
    return bt, AgentUnderTest(bt)


@test
def trusted_device_is_allowed_even_with_window_closed():
    bt, ag = fresh(False, {"Trusted": True, "Bonded": True})
    assert authorize(ag, bluez.SERVICE_UUID) == "ok"


@test
def bonded_device_in_window_is_allowed_and_trusted():
    bt, ag = fresh(True, {"Trusted": False, "Bonded": True})
    assert authorize(ag, bluez.SERVICE_UUID) == "ok"
    assert bt.trusted_calls == [DEV]


@test
def bonded_but_untrusted_device_refused_when_window_closed():
    bt, ag = fresh(False, {"Trusted": False, "Bonded": True})
    assert authorize(ag, bluez.SERVICE_UUID) == "org.bluez.Error.Rejected"
    assert bt.trusted_calls == []


@test
def no_bonding_pairing_refused_even_inside_window():
    bt, ag = fresh(True, {"Trusted": False, "Bonded": False, "Paired": True})
    t0 = time.monotonic()
    assert authorize(ag, bluez.SERVICE_UUID) == "org.bluez.Error.Rejected"
    assert time.monotonic() - t0 >= 1.4, "should re-check before refusing"
    assert bt.trusted_calls == []


@test
def late_bonded_flag_is_accepted_on_recheck():
    bt, ag = fresh(True, {"Trusted": False, "Bonded": False})
    GLib.timeout_add(500, lambda: bt.devices[DEV].update(Bonded=True) or False)
    assert authorize(ag, bluez.SERVICE_UUID) == "ok"
    assert bt.trusted_calls == [DEV]


@test
def other_services_refused_for_untrusted():
    bt, ag = fresh(True, {"Trusted": False, "Bonded": True})
    assert authorize(ag, "0000110a-0000-1000-8000-00805f9b34fb") == "org.bluez.Error.Rejected"


@test
def pairing_requests_follow_window():
    for opened, expect_ok in ((True, True), (False, False)):
        bt, ag = fresh(opened, {})
        try:
            ag.RequestConfirmation(DEV, 123456)
            ok = True
        except bluez.Rejected:
            ok = False
        assert ok == expect_ok, (opened, ok)
        try:
            ag.RequestAuthorization(DEV)
            ok = True
        except bluez.Rejected:
            ok = False
        assert ok == expect_ok


@test
def window_drives_pairable_and_discoverable():
    bt, _ = fresh(False)
    bt.open_pairing(30)
    assert bt.adapter.store["Pairable"] and bt.adapter.store["Discoverable"]
    bt.open_pairing(0)
    assert not bt.adapter.store["Pairable"] and not bt.adapter.store["Discoverable"]
    bt.open_pairing(-1)
    assert bt.pairing_open and bt.pairing_remaining() == -1 and bt.adapter.store["Pairable"]
    bt.open_pairing(0)


@test
def bonding_inside_window_trusts_device():
    bt, _ = fresh(True, {"Bonded": True})
    bt._on_props_changed(bluez.DEVICE_IFACE, {"Paired": True, "Bonded": True}, [], path=DEV)
    assert bt.trusted_calls == [DEV]
    bt2, _ = fresh(False, {"Bonded": True})
    bt2._on_props_changed(bluez.DEVICE_IFACE, {"Paired": True, "Bonded": True}, [], path=DEV)
    assert bt2.trusted_calls == []


def main():
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            passed += 1
            print("PASS  %s" % t.__name__)
        except Exception as e:
            failed += 1
            print("FAIL  %s: %r" % (t.__name__, e))
            traceback.print_exc(limit=2)
    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
