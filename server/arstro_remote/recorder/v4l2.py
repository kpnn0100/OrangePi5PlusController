"""HDMI RX device helpers: EDID, signal detection, display plane and audio lookup."""
import glob
import os
import re
import subprocess

EDID_TYPES = {
    "4k60": "hdmi-4k-600mhz",   # up to 3840x2160@60 (600 MHz TMDS, HDMI 2.0)
    "4k30": "hdmi-4k-300mhz",   # up to 3840x2160@30
    "1080p": "hdmi",            # up to 1920x1080@60
}


def v4l2(dev, *args):
    r = subprocess.run(["v4l2-ctl", "-d", dev, *args], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def find_hdmirx_device():
    for path in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            with open(os.path.join(path, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if "hdmirx" in name.lower():
            return "/dev/" + os.path.basename(path)
    return None


def set_edid(dev, edid, edid_file=None):
    if edid_file:
        args = [f"--set-edid=pad=0,file={edid_file}"]
        label = edid_file
    else:
        args = [f"--set-edid=pad=0,type={EDID_TYPES[edid]}"]
        label = EDID_TYPES[edid]
    rc, out = v4l2(dev, *args)
    if rc != 0 or "failed" in out.lower():
        print(f"[warn] could not set EDID ({label}): {out.strip()}")
        return False
    print(f"[info] EDID set: {label} (source will re-read it via hot-plug)")
    return True


def link_state(dev):
    """Why there is (no) picture: (state, explanation) with state one of
    'none' (no 5 V from a source), 'idle' (source connected but not transmitting),
    'unlocked' (signal present, receiver can't lock), 'unsupported', 'ok'."""
    _, pw = v4l2(dev, "--get-ctrl", "power_present")
    powered = pw.strip().endswith("1")
    rc, out = v4l2(dev, "--query-dv-timings")
    low = out.lower()
    if rc == 0 and re.search(r"Active width:\s*[1-9]", out):
        return "ok", "signal locked"
    if not powered:
        return "none", "Nothing connected (no 5 V from a source on the HDMI IN port)."
    if "severed" in low or "no link" in low:
        return "idle", ("A source is plugged in (5 V present) but it is not sending video. "
                        "It did not accept the board as a display: try another EDID below, "
                        "copy the EDID of a screen that works with the camera, or turn the "
                        "camera off and on.")
    if "no locks" in low or "lock" in low:
        return "unlocked", ("The source is sending but the receiver can't lock on. Try a "
                            "shorter / better HDMI cable or a lower resolution EDID.")
    if "out of range" in low:
        return "unsupported", "The source sends a video format the receiver can't take."
    return "idle", out.strip().splitlines()[0] if out.strip() else "no signal"


def monitor_edids():
    """EDIDs of screens connected to this board's outputs: [(connector, name, bytes)]."""
    found = []
    for path in sorted(glob.glob("/sys/class/drm/card*-*/edid")):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if len(data) >= 128:
            conn = os.path.basename(os.path.dirname(path)).split("-", 1)[1]
            found.append((conn, edid_name(data) or conn, data))
    return found


def edid_name(data):
    """Monitor name from the EDID's display-name descriptor (0xFC)."""
    for off in (54, 72, 90, 108):
        d = data[off:off + 18]
        if len(d) == 18 and d[:3] == b"\0\0\0" and d[3] == 0xFC:
            return d[5:].split(b"\n")[0].decode("ascii", "replace").strip()
    return None


def save_edid_hex(data, path):
    """Write an EDID in the hex format v4l2-ctl --set-edid file= expects."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i in range(0, len(data), 16):
            f.write(" ".join(f"{b:02x}" for b in data[i:i + 16]) + "\n")


def query_signal(dev):
    """Return (width, height, fps) of the incoming signal, or None."""
    rc, out = v4l2(dev, "--query-dv-timings")
    w = re.search(r"Active width:\s*(\d+)", out)
    h = re.search(r"Active height:\s*(\d+)", out)
    if rc != 0 or not w or not h or int(w.group(1)) == 0:
        return None
    fps = re.search(r"\(([\d.]+) frames per second\)", out)
    return int(w.group(1)), int(h.group(1)), float(fps.group(1)) if fps else 0.0


# V4L2 fourcc -> GStreamer format name. The rk_hdmirx driver lists BGR3/NV24/NV16/NV12
# but only accepts the one that matches the incoming colour format (RGB -> BGR3,
# YCbCr 4:4:4 -> NV24, 4:2:2 -> NV16, 4:2:0 -> NV12), so the capture caps must be
# pinned to the current format or GStreamer may pick one the driver rejects.
V4L2_TO_GST = {
    "NV12": "NV12", "NV21": "NV21", "NV16": "NV16", "NV61": "NV61",
    "NV24": "NV24", "NV42": "NV42", "BGR3": "BGR", "RGB3": "RGB",
    "YUYV": "YUY2", "UYVY": "UYVY", "XR24": "BGRx", "AR24": "BGRA",
}
# formats rkximagesink can put on a display plane directly (it ignores NV24 and 24-bit RGB)
DISPLAY_FORMATS = {"NV12", "NV21", "NV16", "YUY2", "UYVY", "YVYU", "BGRx", "BGRA", "RGBx", "RGBA"}


def lock_timings(dev):
    """Apply the detected timings; return (GStreamer format, V4L2 fourcc) the driver will deliver."""
    v4l2(dev, "--set-dv-bt-timings=query")
    _, out = v4l2(dev, "--get-fmt-video")
    m = re.search(r"Pixel Format\s*:\s*'(\w+)'", out)
    fourcc = m.group(1) if m else "?"
    return V4L2_TO_GST.get(fourcc, fourcc), fourcc


def find_overlay_plane():
    """Pick a DRM overlay plane that can scan out NV12 on an active CRTC.

    rkximagesink's own plane search fails when X already holds the planes it
    looks at, so choose one ourselves: a YUV-capable plane allowed on a CRTC
    that is showing a mode, excluding that CRTC's primary (X desktop) plane.
    """
    try:
        out = subprocess.run(["modetest", "-M", "rockchip", "-p"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.splitlines()
    crtcs, planes, section = [], [], None
    for i, line in enumerate(lines):
        if line.startswith("CRTCs:"):
            section = "crtc"
        elif line.startswith("Planes:"):
            section = "plane"
        elif section == "crtc":
            m = re.match(r"(\d+)\s+(\d+)\s+\(\d+,\d+\)\s+\((\d+)x(\d+)\)", line)
            if m:
                crtcs.append((int(m.group(2)), int(m.group(3)) > 0))  # (fb, active)
        elif section == "plane":
            m = re.match(r"(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+\S+\s+\d+\s+0x([0-9a-f]+)", line)
            if m and i + 1 < len(lines) and "NV12" in lines[i + 1]:
                planes.append((int(m.group(1)), int(m.group(3)), int(m.group(4), 16)))
    for idx, (crtc_fb, active) in enumerate(crtcs):
        if not active:
            continue
        for plane_id, fb, mask in planes:
            if mask & (1 << idx) and (fb == 0 or fb != crtc_fb):
                return plane_id
    return None


def audio_present(dev):
    """True when the HDMI source is sending audio. The hdmiin ALSA device can't even be
    opened otherwise ("No such device"), which would fail the whole pipeline."""
    _, out = v4l2(dev, "--get-ctrl", "audio_present")
    return out.strip().endswith("1")


def find_pulse_hdmiin():
    """PulseAudio source for HDMI-in, if PulseAudio runs and owns the card. PulseAudio opens
    the device as soon as the source sends audio, so a direct ALSA open is then "busy"."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and "hdmiin" in parts[1] and not parts[1].endswith(".monitor"):
            return parts[1]
    return None


def find_hdmiin_audio():
    try:
        with open("/proc/asound/cards") as f:
            for line in f:
                m = re.match(r"\s*\d+\s+\[(\S+)\s*\]", line)
                if m and "hdmiin" in m.group(1).lower():
                    return m.group(1)
    except OSError:
        pass
    return None


class SimulatedSignal:
    """Stand-in for the HDMI RX port (--simulate): a test pattern with a fixed timing.

    Toggle the signal on/off with toggle() to exercise hot-plug handling.
    """

    def __init__(self, spec):
        m = re.fullmatch(r"(\d+)x(\d+)@([\d.]+)(?::(\w+))?(?::([\w-]+))?", spec)
        if not m:
            raise ValueError("simulate spec must look like 3840x2160@60, 1920x1080@60:NV16 "
                             "or 3840x2160@60:NV12:black (videotestsrc pattern)")
        self.timing = (int(m.group(1)), int(m.group(2)), float(m.group(3)))
        self.format = (m.group(4) or "NV12").upper()
        self.pattern = m.group(5) or "smpte"
        self.present = True

    def query(self):
        return self.timing if self.present else None

    def toggle(self):
        self.present = not self.present
