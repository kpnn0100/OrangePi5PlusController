"""BlueZ integration: adapter setup, RFCOMM server profile and the pairing agent.

All methods here must run on the GLib main loop thread (dbus-python is not
thread-safe); other threads go through Daemon.call_in_main().
"""

import glob
import logging
import socket
import struct
import time

import dbus
import dbus.service
from gi.repository import GLib

log = logging.getLogger("arstro.bt")

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"

# Custom 128-bit UUID of the Arstro Remote RFCOMM service. The Android app
# connects with createRfcommSocketToServiceRecord(SERVICE_UUID).
SERVICE_UUID = "a57e0001-7c2b-4d1e-9f3a-5e7a1b2c3d4e"
PROFILE_PATH = "/org/arstro/remote/profile"
AGENT_PATH = "/org/arstro/remote/agent"


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


def rfkill_unblock_bluetooth():
    """Clear a soft rfkill block (the user is in group netdev which owns /dev/rfkill)."""
    blocked = [r for r in glob.glob("/sys/class/rfkill/rfkill*")
               if open(r + "/type").read().strip() == "bluetooth"
               and open(r + "/soft").read().strip() == "1"]
    if not blocked:
        return False
    try:
        # struct rfkill_event { u32 idx; u8 type; u8 op; u8 soft; u8 hard; }
        with open("/dev/rfkill", "wb", buffering=0) as f:
            f.write(struct.pack("=IBBBB", 0, 2, 3, 0, 0))  # TYPE_BLUETOOTH, OP_CHANGE_ALL
        log.info("rfkill: unblocked bluetooth")
        return True
    except OSError as e:
        log.warning("rfkill unblock failed: %s", e)
        return False


def find_free_channel(adapter_addr, preferred):
    """Pick an RFCOMM channel nobody listens on (bluetoothd does not report clashes)."""
    for ch in [preferred] + [c for c in range(1, 31) if c != preferred]:
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            s.bind((adapter_addr, ch))
            return ch
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free RFCOMM channel")


class Profile(dbus.service.Object):
    def __init__(self, bus, bt):
        super().__init__(bus, PROFILE_PATH)
        self.bt = bt

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        log.info("profile released by bluetoothd")

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, properties):
        fd = fd.take()
        log.info("new RFCOMM connection from %s (fd %d)", device, fd)
        self.bt.on_connection(str(device), fd)

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        log.info("bluetoothd requests disconnection of %s", device)
        self.bt.on_disconnect(str(device))


class Agent(dbus.service.Object):
    """Pairing and service authorization policy.

    Security model: only *trusted* devices may open the shell service. A device
    becomes trusted by bonding while the pairing window is open. Three layers:
      * Pairable follows the window, so outside it the kernel refuses bonding.
      * The profile uses RequireAuthorization, so every connection from an
        untrusted device lands in AuthorizeService below. This also covers
        "no-bonding" pairings, which the kernel allows even when not pairable
        and which would otherwise satisfy RequireAuthentication alone.
      * Registered as DisplayYesNo so the kernel asks us (instead of silently
        auto-accepting) whenever the phone has a display.
    """

    def __init__(self, bus, bt):
        super().__init__(bus, AGENT_PATH)
        self.bt = bt

    def _check(self, device, what):
        name = self.bt.device_label(device)
        if not self.bt.pairing_open:
            log.warning("rejected %s from %s: pairing window closed (run 'arstro-remote pair')", what, name)
            raise Rejected("Pairing window closed")
        log.info("accepted %s from %s", what, name)
        self.bt.mark_for_trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        log.info("agent released")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="",
                         async_callbacks=("reply", "error"))
    def AuthorizeService(self, device, uuid, reply, error):
        uuid = str(uuid).lower()

        def decide(final):
            props = self.bt.device_props(device)
            name = self.bt.device_label(device)
            if props.get("Trusted"):
                reply()
                return False
            bonded = bool(props.get("Bonded", props.get("Paired", False)))
            if uuid == SERVICE_UUID and bonded and self.bt.pairing_open:
                log.info("authorized %s (bonded during pairing window), trusting it", name)
                self.bt._trust(device)
                reply()
                return False
            if not final and uuid == SERVICE_UUID and self.bt.pairing_open:
                # The link key (Bonded) can be stored a moment after pairing
                # completes; look again before refusing a phone that just paired.
                GLib.timeout_add(1500, decide, True)
                return False
            log.warning("refused service %s for untrusted %s (bonded=%s, pairing window %s)",
                        uuid, name, bonded, "open" if self.bt.pairing_open else "closed")
            error(Rejected("Device not trusted"))
            return False

        decide(False)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        self._check(device, "legacy PIN pairing")
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self._check(device, "passkey pairing")
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log.info("passkey for %s: %06d", self.bt.device_label(device), passkey)

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log.info("PIN for %s: %s", self.bt.device_label(device), pincode)

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        self._check(device, "pairing (code %06d)" % passkey)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        self._check(device, "just-works pairing")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log.info("pairing cancelled")


class Bluetooth:
    def __init__(self, bus, config, on_connection, on_disconnect):
        self.bus = bus
        self.config = config
        self._on_connection = on_connection
        self._on_disconnect = on_disconnect
        self.adapter_path = None
        self.channel = None
        self.ready = False
        self.pairing_until = 0.0  # monotonic deadline, inf = always
        self._pair_timer = None
        self._reassert_timer = None
        self._to_trust = set()
        self.profile = Profile(bus, self)
        self.agent = Agent(bus, self)
        bus.add_signal_receiver(self._on_props_changed, signal_name="PropertiesChanged",
                                dbus_interface=PROPS_IFACE, bus_name=BLUEZ, path_keyword="path")
        bus.add_signal_receiver(self._on_name_owner, signal_name="NameOwnerChanged",
                                dbus_interface="org.freedesktop.DBus", arg0=BLUEZ)

    # ----------------------------------------------------------------- setup
    def _objects(self):
        om = dbus.Interface(self.bus.get_object(BLUEZ, "/"), OM_IFACE)
        return om.GetManagedObjects()

    def _adapter_props(self):
        return dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), PROPS_IFACE)

    def setup(self):
        """(Re)initialise everything. Safe to call again after bluetoothd restarts."""
        self.ready = False
        rfkill_unblock_bluetooth()
        adapters = sorted(p for p, ifs in self._objects().items() if ADAPTER_IFACE in ifs)
        if not adapters:
            raise RuntimeError("no Bluetooth adapter found")
        wanted = self.config.get("adapter")
        self.adapter_path = next((p for p in adapters if wanted and p.endswith(wanted)), adapters[0])
        props = self._adapter_props()
        if not props.Get(ADAPTER_IFACE, "Powered"):
            props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
            for _ in range(20):
                if props.Get(ADAPTER_IFACE, "Powered"):
                    break
                time.sleep(0.25)
        alias = self.config.get("alias")
        if alias:
            alias = alias.replace("{hostname}", socket.gethostname())
            if props.Get(ADAPTER_IFACE, "Alias") != alias:
                props.Set(ADAPTER_IFACE, "Alias", dbus.String(alias))
        props.Set(ADAPTER_IFACE, "PairableTimeout", dbus.UInt32(0))
        props.Set(ADAPTER_IFACE, "DiscoverableTimeout", dbus.UInt32(0))
        address = str(props.Get(ADAPTER_IFACE, "Address"))

        pm = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.ProfileManager1")
        try:
            pm.UnregisterProfile(PROFILE_PATH)
        except dbus.DBusException:
            pass
        self.channel = find_free_channel(address, int(self.config.get("channel", 22)))
        pm.RegisterProfile(PROFILE_PATH, SERVICE_UUID, {
            "Name": dbus.String("Arstro Remote"),
            "Role": dbus.String("server"),
            "Channel": dbus.UInt16(self.channel),
            "RequireAuthentication": dbus.Boolean(True),
            "RequireAuthorization": dbus.Boolean(True),
            "AutoConnect": dbus.Boolean(False),
        })
        am = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
        try:
            am.UnregisterAgent(AGENT_PATH)
        except dbus.DBusException:
            pass
        am.RegisterAgent(AGENT_PATH, "DisplayYesNo")
        am.RequestDefaultAgent(AGENT_PATH)
        self.ready = True
        log.info("bluetooth ready: adapter %s (%s) alias %r, RFCOMM channel %d, uuid %s",
                 self.adapter_path, address, str(alias or props.Get(ADAPTER_IFACE, "Alias")), self.channel,
                 SERVICE_UUID)
        self._apply_discoverable()

    # --------------------------------------------------------------- pairing
    @property
    def pairing_open(self):
        return time.monotonic() < self.pairing_until

    def pairing_remaining(self):
        if self.pairing_until == float("inf"):
            return -1
        return max(0, int(self.pairing_until - time.monotonic()))

    def open_pairing(self, seconds):
        """Open the pairing window; seconds < 0 means forever, 0 closes it."""
        if seconds < 0:
            self.pairing_until = float("inf")
        else:
            self.pairing_until = time.monotonic() + seconds
        if self._pair_timer:
            GLib.source_remove(self._pair_timer)
            self._pair_timer = None
        if 0 < seconds:
            self._pair_timer = GLib.timeout_add_seconds(int(seconds) + 1, self._pair_timeout)
        log.info("pairing window %s", "open forever" if seconds < 0 else
                 "open for %ds" % seconds if seconds else "closed")
        self._apply_discoverable()

    def _pair_timeout(self):
        self._pair_timer = None
        log.info("pairing window closed")
        self._apply_discoverable()
        return False

    def _apply_discoverable(self):
        """Discoverable *and* Pairable follow the pairing window. Outside it the
        kernel refuses new pairings outright; bonded phones still connect."""
        if not self.adapter_path:
            return
        want = self.pairing_open
        try:
            props = self._adapter_props()
            for prop in ("Pairable", "Discoverable"):
                if bool(props.Get(ADAPTER_IFACE, prop)) != want:
                    props.Set(ADAPTER_IFACE, prop, dbus.Boolean(want))
        except dbus.DBusException as e:
            log.warning("cannot set Discoverable/Pairable=%s: %s", want, e)
        # Other agents (blueman-applet) may grab the default-agent slot when the
        # desktop starts; keep reclaiming it while pairing is allowed.
        if want and self._reassert_timer is None:
            self._reassert_timer = GLib.timeout_add_seconds(5, self._reassert_agent)
        elif not want and self._reassert_timer is not None:
            GLib.source_remove(self._reassert_timer)
            self._reassert_timer = None

    def _reassert_agent(self):
        if not self.pairing_open:
            self._reassert_timer = None
            self._apply_discoverable()
            return False
        try:
            am = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
            am.RequestDefaultAgent(AGENT_PATH)
            props = self._adapter_props()
            for prop in ("Pairable", "Discoverable"):
                if not props.Get(ADAPTER_IFACE, prop):
                    props.Set(ADAPTER_IFACE, prop, dbus.Boolean(True))
        except dbus.DBusException as e:
            log.debug("reassert agent: %s", e)
        return True

    # --------------------------------------------------------------- devices
    def device_props(self, path):
        try:
            p = dbus.Interface(self.bus.get_object(BLUEZ, path), PROPS_IFACE)
            return p.GetAll(DEVICE_IFACE)
        except dbus.DBusException:
            return {}

    def device_label(self, path):
        props = self.device_props(path)
        name = props.get("Alias") or props.get("Name") or "?"
        return "%s [%s]" % (name, props.get("Address", str(path).rsplit("/", 1)[-1][4:].replace("_", ":")))

    def is_paired(self, path):
        return bool(self.device_props(path).get("Paired", False))

    def mark_for_trust(self, path):
        self._to_trust.add(str(path))

    def _trust(self, path):
        try:
            p = dbus.Interface(self.bus.get_object(BLUEZ, path), PROPS_IFACE)
            if not p.Get(DEVICE_IFACE, "Trusted"):
                p.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
                log.info("trusted %s", self.device_label(path))
        except dbus.DBusException as e:
            log.warning("cannot trust %s: %s", path, e)

    def paired_devices(self):
        devs = []
        for path, ifs in self._objects().items():
            d = ifs.get(DEVICE_IFACE)
            if not d or not str(path).startswith(str(self.adapter_path) + "/"):
                continue
            if d.get("Paired"):
                devs.append({"address": str(d.get("Address")), "name": str(d.get("Alias", "")),
                             "connected": bool(d.get("Connected")), "trusted": bool(d.get("Trusted"))})
        return devs

    def remove_device(self, address):
        for path, ifs in self._objects().items():
            d = ifs.get(DEVICE_IFACE)
            if d and str(d.get("Address", "")).upper() == address.upper():
                adapter = dbus.Interface(self.bus.get_object(BLUEZ, self.adapter_path), ADAPTER_IFACE)
                adapter.RemoveDevice(path)
                log.info("removed device %s", address)
                return {"removed": address}
        raise KeyError("device %s not found" % address)

    def adapter_status(self):
        if not self.adapter_path:
            return {"ready": False}
        try:
            p = self._adapter_props().GetAll(ADAPTER_IFACE)
        except dbus.DBusException as e:
            return {"ready": False, "error": str(e)}
        return {
            "ready": self.ready,
            "adapter": str(self.adapter_path),
            "address": str(p.get("Address")),
            "alias": str(p.get("Alias")),
            "powered": bool(p.get("Powered")),
            "discoverable": bool(p.get("Discoverable")),
            "pairable": bool(p.get("Pairable")),
            "channel": self.channel,
            "uuid": SERVICE_UUID,
            "pairing_open": self.pairing_open,
            "pairing_remaining": self.pairing_remaining(),
            "paired_devices": self.paired_devices(),
        }

    # --------------------------------------------------------------- signals
    def on_connection(self, device_path, fd):
        self._on_connection(device_path, fd)

    def on_disconnect(self, device_path):
        self._on_disconnect(device_path)

    def _on_props_changed(self, iface, changed, _invalidated, path=None):
        if iface == DEVICE_IFACE:
            if changed.get("Bonded") or changed.get("Paired"):
                bonded = bool(self.device_props(path).get("Bonded", changed.get("Paired")))
                log.info("paired with %s (bonded=%s)", self.device_label(path), bonded)
                # Bonding can only happen while Pairable, i.e. inside the window
                # (the kernel may auto-accept without asking the agent).
                if bonded and (self.pairing_open or path in self._to_trust):
                    self._trust(path)
                self._to_trust.discard(path)
            if "Connected" in changed and not changed["Connected"]:
                self._on_disconnect(str(path))
        elif iface == ADAPTER_IFACE and path == self.adapter_path and "Powered" in changed:
            log.info("adapter powered=%s", bool(changed["Powered"]))
            if changed["Powered"]:
                GLib.timeout_add(500, self._resetup_once)

    def _on_name_owner(self, name, old, new):
        if new:
            log.info("bluetoothd (re)started, re-registering")
            GLib.timeout_add(1000, self._resetup_once)
        else:
            log.warning("bluetoothd went away")
            self.ready = False

    def _resetup_once(self):
        try:
            self.setup()
        except Exception as e:
            log.error("bluetooth setup failed: %s (retrying in 5s)", e)
            GLib.timeout_add_seconds(5, self._resetup_once)
        return False
