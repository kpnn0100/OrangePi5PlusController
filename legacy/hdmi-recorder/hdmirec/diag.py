"""Diagnostic report: everything needed to debug the HDMI RX path, in one text file.

    python3 hdmi_recorder.py diag            # writes ~/.local/state/hdmi-recorder/diag-*.txt

Collects system / driver / GStreamer facts, then runs short live tests with the
real device (capture speed per io-mode, display, VPU encode, audio). The live
tests need the device, so the app pauses its own capture while they run.
"""
import glob
import os
import platform
import re
import subprocess
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from . import log, settings, v4l2  # noqa: E402
from .gstutil import is_dmabuf  # noqa: E402

ELEMENTS = ["v4l2src", "rkximagesink", "kmssink", "mpph265enc", "x265enc", "avenc_ffv1",
            "mp4mux", "qtmux", "matroskamux", "voaacenc", "alsasrc", "videoconvert"]


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).rstrip() or f"(exit {r.returncode}, no output)"
    except (OSError, subprocess.SubprocessError) as e:
        return f"(failed: {e})"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        return f"(unreadable: {e.strerror})"


def _pipeline_test(desc, seconds=3.0, count_element="probe"):
    """Run a pipeline for a few seconds. Returns (frames/s, first buffer info, error or None)."""
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error as e:
        return 0.0, None, f"parse error: {e.message}"
    counter = {"n": 0, "t0": None, "first": None}

    def probe(pad, info):
        buf = info.get_buffer()
        if counter["t0"] is None:
            counter["t0"] = time.monotonic()
            caps = pad.get_current_caps()
            counter["first"] = (f"{buf.get_size()} bytes, "
                                f"{'dmabuf' if is_dmabuf(buf) else 'system memory'}, "
                                f"{caps.to_string() if caps else '?'}")
        else:
            counter["n"] += 1
        return Gst.PadProbeReturn.OK

    el = pipe.get_by_name(count_element)
    if el:
        el.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe)
    bus = pipe.get_bus()
    error = None
    pipe.set_state(Gst.State.PLAYING)
    deadline = time.monotonic() + seconds + 2
    while time.monotonic() < deadline:
        msg = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg and msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            error = f"{err.message}\n      {dbg}"
            break
        if counter["t0"] and time.monotonic() - counter["t0"] >= seconds:
            break
    pipe.set_state(Gst.State.NULL)
    fps = 0.0
    if counter["t0"] and counter["n"]:
        fps = counter["n"] / max(0.001, time.monotonic() - counter["t0"])
    return fps, counter["first"], error


def report(dev=None, live=True):
    Gst.init(None)
    dev = dev or v4l2.find_hdmirx_device()
    out = []

    def section(title):
        out.append(f"\n===== {title} " + "=" * max(0, 60 - len(title)))

    def add(label, value):
        value = str(value)
        if "\n" in value:
            out.append(f"{label}:\n" + "\n".join("    " + l for l in value.splitlines()))
        else:
            out.append(f"{label}: {value}")

    out.append(f"hdmi-recorder diagnostic report — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    section("System")
    add("kernel", platform.release())
    add("board", _read("/proc/device-tree/model").replace("\x00", ""))
    add("os", _run(["sh", "-c", ". /etc/os-release; echo $PRETTY_NAME"]))
    add("uptime", _read("/proc/uptime").split()[0] + " s")
    add("memory", _run(["free", "-m"]))
    add("cmdline", _read("/proc/cmdline"))
    add("boot env", _run(["grep", "-E", "^(overlays|extraargs)=", "/boot/orangepiEnv.txt"]))
    add("recent boots (unexpected reboots show as short boots)",
        "\n".join(_run(["journalctl", "--list-boots", "--no-pager"]).splitlines()[-4:]))
    add("temperatures (m°C)", " ".join(
        _read(p) for p in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))))

    section("GStreamer")
    add("version", Gst.version_string())
    add("elements", ", ".join(f"{e}={'yes' if Gst.ElementFactory.find(e) else 'NO'}"
                              for e in ELEMENTS))
    add("blacklisted", _run(["gst-inspect-1.0", "-b"]))
    add("GST_* environment", "\n".join(f"{k}={v}" for k, v in sorted(os.environ.items())
                                       if k.startswith("GST_")) or "(none)")
    add("DISPLAY", os.environ.get("DISPLAY", "(unset)"))

    section("HDMI RX device")
    add("device", dev or "NOT FOUND (is overlays=hdmirx set and the board rebooted?)")
    for p in sorted(glob.glob("/sys/class/video4linux/video*")):
        add(p, _read(os.path.join(p, "name")))
    if dev:
        add("v4l2-ctl --all", _run(["v4l2-ctl", "-d", dev, "--all"]))
        add("link state", " — ".join(v4l2.link_state(dev)))
        add("formats", _run(["v4l2-ctl", "-d", dev, "--list-formats-ext"]))
        add("query-dv-timings", _run(["v4l2-ctl", "-d", dev, "--query-dv-timings"]))
        add("EDID (first 256 bytes)", _run(["v4l2-ctl", "-d", dev, "--get-edid=format=hex"]))
        add("kernel log (hdmirx)", "\n".join(
            [l for l in _run(["journalctl", "-k", "-b", "--no-pager", "-n", "400"]).splitlines()
             if "hdmirx" in l][-25:]) or "(no hdmirx lines / no access)")

    section("Display")
    add("xrandr", _run(["xrandr", "--prop"]) if os.environ.get("DISPLAY") else "(no DISPLAY)")
    plane = v4l2.find_overlay_plane()
    add("chosen overlay plane", plane)
    add("screens with readable EDID (can be copied to HDMI IN)",
        "\n".join(f"{c}: {n} ({len(d)} bytes)" for c, n, d in v4l2.monitor_edids()) or "(none)")
    planes = _run(["modetest", "-M", "rockchip", "-p"])
    if plane is not None:
        m = re.search(rf"^{plane}\s.*?(?=^\d+\s)", planes, re.S | re.M)
        add(f"plane {plane}", m.group(0).strip()[:1500] if m else "(not listed)")
    add("rkximagesink formats", "BGRx BGRA RGBx RGBA NV12 NV21 NV16 YUY2 UYVY YVYU "
        "(NV24 and 24-bit RGB are ignored: the app converts the preview for those)")

    section("Audio")
    add("cards", _read("/proc/asound/cards"))
    card = v4l2.find_hdmiin_audio()
    add("hdmiin card", card or "not found")

    section("App settings")
    add(settings.PATH, _read(settings.PATH))

    if live and dev:
        section("Live tests (device must not be used by another program)")
        timing = v4l2.query_signal(dev)
        add("signal", timing or "NO SIGNAL — connect the source, then run the report again")
        if timing:
            fmt, fourcc = v4l2.lock_timings(dev)
            w, h, fps = timing
            add("driver format", f"{fourcc} -> GStreamer {fmt}")
            caps = f"video/x-raw,format={fmt},width={w},height={h}"
            for io in ("dmabuf", "mmap"):
                r = _pipeline_test(f"v4l2src device={dev} io-mode={io} ! {caps} ! "
                                   f"identity name=probe ! fakesink sync=false")
                add(f"capture io-mode={io}", _fmt_result(r, fps))
            unpinned = _pipeline_test(f"v4l2src device={dev} io-mode=dmabuf ! "
                                      "video/x-raw,format=NV16 ! identity name=probe ! fakesink")
            add("capture forcing NV16 (expected to fail unless the source sends 4:2:2)",
                _fmt_result(unpinned, fps))
            if os.environ.get("DISPLAY") and Gst.ElementFactory.find("rkximagesink"):
                conv = "" if fmt in v4l2.DISPLAY_FORMATS else \
                    "videoconvert n-threads=4 ! video/x-raw,format=NV16 ! "
                ptxt = f" plane-id={plane}" if plane is not None else ""
                r = _pipeline_test(f"v4l2src device={dev} io-mode=dmabuf ! {caps} ! "
                                   f"identity name=probe ! queue max-size-buffers=2 leaky=downstream ! "
                                   f"{conv}rkximagesink sync=false{ptxt}")
                add("capture + preview (rkximagesink)", _fmt_result(r, fps))
            if Gst.ElementFactory.find("mpph265enc"):
                conv = "" if fmt == "NV12" else "videoconvert n-threads=4 ! video/x-raw,format=NV12 ! "
                r = _pipeline_test(f"v4l2src device={dev} io-mode=dmabuf ! {caps} ! queue ! {conv}"
                                   "mpph265enc bps=80000000 ! identity name=probe ! fakesink sync=false")
                add("VPU H.265 encode (encoded frames/s)", _fmt_result(r, fps))
        if card:
            r = _pipeline_test(f"alsasrc device=plughw:CARD={card},DEV=0 ! "
                               "audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
                               "identity name=probe ! fakesink", seconds=2)
            add("audio capture (buffers/s)", _fmt_result(r, None))

    section("Recent app log")
    out.append(log.tail())
    return "\n".join(out) + "\n"


def _fmt_result(r, nominal):
    fps, first, err = r
    if err:
        return "FAILED: " + err
    if first is None:
        return "FAILED: no data within the test time"
    s = f"{fps:.1f}/s"
    if nominal:
        s += f" (signal {nominal:.2f} fps){'  <-- TOO SLOW' if fps < nominal * 0.9 else ''}"
    return f"{s}; first buffer: {first}"


def write_report(dev=None, live=True):
    os.makedirs(log.LOG_DIR, exist_ok=True)
    text = report(dev, live)
    path = os.path.join(log.LOG_DIR, time.strftime("diag-%Y%m%d-%H%M%S.txt"))
    with open(path, "w") as f:
        f.write(text)
    latest = os.path.join(log.LOG_DIR, "diag-latest.txt")
    try:
        if os.path.lexists(latest):
            os.remove(latest)
        os.symlink(os.path.basename(path), latest)
    except OSError:
        pass
    return path
