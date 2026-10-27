#!/usr/bin/env python3
"""Film work, self-contained (REC-11, REC-12, REC-13, REC-15, REC-16, GAL-13): timecode and the
tmcd track, take names, sidecars and the editor's CSV, storage figures and the disk test,
exposure analysis and LUTs, the record button and tally light (with a stand-in for GPIO).
No server, no HDMI; the tmcd test muxes small files with GStreamer like the recorder does.

    python3 server/tests/test_film.py [-k NAME]
"""

import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime

from harness import check, run, test, wait_until

from arstro_remote.recorder import monitor, settings as settings_mod, storage, takes, timecode
from arstro_remote.recorder.gpio_ctl import RecorderGpio, parse_pin


class Ctx:
    pass


def setup():
    c = Ctx()
    c.dir = tempfile.mkdtemp(prefix="film-test-")
    # never the user's own settings or state: everything this test saves goes to its temp folder
    os.environ["XDG_STATE_HOME"] = os.path.join(c.dir, "state")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(c.dir, "config")
    return c


def teardown(c):
    shutil.rmtree(c.dir, ignore_errors=True)


# ------------------------------------------------------------------------- timecode
@test("REC-11")
def drop_frame_labels_follow_the_wall_clock():
    for base, dropn in ((30, 2), (60, 4)):
        day = base * 86400 - dropn * 1296
        for count in list(range(0, 40000, 7)) + list(range(day - 2000, day)):
            label = timecode.label_from_frames(count, base, True, dropn)
            h, m, s, f = int(label[:2]), int(label[3:5]), int(label[6:8]), int(label[9:])
            check(label[8] == ";", label)
            check(not (m % 10 and s == 0 and f < dropn), "%s is a dropped label" % label)
            check(timecode.frames_from_label(h, m, s, f, base, True, dropn) == count, (count, label))
    check(timecode.label_from_frames(1800, 30, True, 2) == "00:01:00;02", "29.97 skips ;00 ;01")
    check(timecode.label_from_frames(17982, 30, True, 2) == "00:10:00;00", "every tenth minute keeps ;00")
    wall = time.mktime((2026, 10, 27, 10, 1, 0, 0, 0, -1)) + 0.5
    for rate, want in (((25, 1), "10:01:00:12"), ((24000, 1001), "10:01:00:12"), ((30000, 1001), "10:01:00;15"),
                       ((50, 1), "10:01:00:25")):
        tc = timecode.start_timecode(wall, *rate)
        check(tc["tc"] == want, (rate, tc["tc"], want))
    tc = timecode.start_timecode(time.mktime((2026, 10, 27, 10, 1, 0, 0, 0, -1)), 30000, 1001)
    check(tc["tc"] == "10:01:00;02", "a dropped label at the start moves to the next one: %s" % tc["tc"])
    check(timecode.offset_label(timecode.start_timecode(wall, 25, 1), 25 * 61) == "10:02:01:12", "offset")


def _mux(path, rate, mux, audio=True):
    n, d = rate
    desc = (f"videotestsrc num-buffers=45 ! video/x-raw,width=320,height=180,framerate={n}/{d} ! x264enc ! mux. ")
    if audio:
        desc += "audiotestsrc num-buffers=70 ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! mux. "
    desc += f"{mux} name=mux ! filesink location={path}"
    r = subprocess.run(["gst-launch-1.0", "-q"] + desc.split(), capture_output=True, text=True, timeout=60)
    check(r.returncode == 0 and os.path.getsize(path) > 0, "gst-launch: %s" % r.stderr[-300:])


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout or "{}")


@test("REC-11", "REC-09")
def a_tmcd_track_editors_read(c):
    """MOV (PCM) with the recorder's reserved index, MP4, and a MOV with its index at the end:
    each gets a timecode track ffmpeg reads, the media untouched and still decodable."""
    wall = time.mktime((2026, 10, 27, 14, 23, 45, 0, 0, -1)) + 0.5
    reserved = "reserved-max-duration=14400000000000 reserved-moov-update-period=1000000000"
    cases = (("a.mov", (30000, 1001), "qtmux " + reserved, True),
             ("b.mp4", (25, 1), "mp4mux " + reserved, False),
             ("c.mov", (24000, 1001), "qtmux", True))
    for name, rate, mux, audio in cases:
        path = os.path.join(c.dir, name)
        _mux(path, rate, mux, audio)
        before = _probe(path)
        size = os.path.getsize(path)
        tc = timecode.start_timecode(wall, *rate)
        check(timecode.add_tmcd(path, tc) is True, "no track added to %s" % name)
        check(timecode.add_tmcd(path, tc) is False, "a second track added to %s" % name)
        p = _probe(path)
        tcs = {s.get("codec_tag_string"): s.get("tags", {}).get("timecode") for s in p["streams"]}
        check(tcs.get("tmcd") == tc["tc"], "%s: tmcd says %s, want %s" % (name, tcs.get("tmcd"), tc["tc"]))
        check(abs(float(p["format"]["duration"]) - float(before["format"]["duration"])) < 0.01, "duration changed")
        if audio:
            check(any(s.get("codec_name") == "pcm_s16le" for s in p["streams"]), "the audio is not PCM")
        if "reserved" in mux:
            check(os.path.getsize(path) - size == 12, "%s: the moov did not stay in its reserved room" % name)
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                             capture_output=True, text=True, timeout=60)
        check(dec.returncode == 0 and not dec.stderr.strip(), "%s no longer decodes: %s" % (name, dec.stderr[:200]))
        frames = timecode.read_tmcd(path)
        check(frames == tc["frames"], "read_tmcd %s != %s" % (frames, tc["frames"]))


# ---------------------------------------------------------------- takes and the sheet
@test("REC-15")
def the_slate_names_the_take():
    when = datetime(2026, 10, 27, 15, 30, 12)
    check(takes.take_name({"scene": "", "shot": "", "take": 3}, when) == "REC_20261027_153012", "no scene")
    check(takes.take_name({"scene": "12A", "shot": "3", "take": 4}, when) == "S12A_SH3_T04_20261027_153012", "slate")
    check(takes.take_name({"scene": "7", "shot": "", "take": 12}, when) == "S7_T12_20261027_153012", "no shot")
    from arstro_remote.recorder.library import clip_id
    check(clip_id("S12A_SH3_T04_20261027_153012_H264.mp4") == "S12A_SH3_T04_20261027_153012", "copies group")
    for bad in ({"slate": {"scene": "1 2"}}, {"slate": {"take": 0}}, {"monitor": {"zoom": 5}},
                {"gpio": {"button": "pin7"}}, {"audio": {"source": "usb"}}, {"monitor": {"lut": "../x"}}):
        try:
            settings_mod.apply_patch(bad)
            raise AssertionError("accepted %s" % bad)
        except ValueError:
            pass
    s = settings_mod.apply_patch({"slate": {"scene": "12A", "take": "4"}, "monitor": {"zoom": "3"},
                                  "gpio": {"button": "gpiochip1:5"}, "audio": {"source": "alsa:USB"}})
    check(s["slate"]["take"] == 4 and s["monitor"]["zoom"] == 3 and s["gpio"]["button"] == "gpiochip1:5", s)
    check(settings_mod.DEFAULTS["h265"]["container"] == "mov", "MOV (PCM) is the default container")


@test("GAL-13", "REC-15", "REC-11")
def the_editors_sheet(c):
    folder = os.path.join(c.dir, "sheet")
    os.makedirs(folder)
    name = "S12A_SH3_T04_20261027_153012"
    tc = timecode.start_timecode(time.mktime((2026, 10, 27, 15, 30, 12, 0, 0, -1)) + 0.25, 30000, 1001)
    takes.update(folder, name, lambda d: d.update(slate={"scene": "12A", "shot": "3", "take": 4}, timecode=tc,
                                                  good=True, notes="best one, \"clap\" late",
                                                  markers=[{"tc": "15:30:20;11", "label": "clap"}],
                                                  sync={"drift_ms": -12.5}, drops=0, clock_synced=True))
    d = takes.load(folder, name)
    check(takes.summary(d)["good"] and takes.slate_title(d) == "Scene 12A · Shot 3 · Take 4", takes.summary(d))
    take = {"id": name, "created": 1793115012, "meta_full": d,
            "items": [{"id": name + ".mov", "duration": 10.01, "fps": 29.97, "width": 3840, "height": 2160,
                       "kind": "H.265", "audio": True},
                      {"id": name + "_H264.mp4", "duration": 10.01, "fps": 29.97, "width": 1920, "height": 1080,
                       "kind": "H.264", "audio": True}]}
    rows = list(csv.DictReader(io.StringIO(takes.to_csv([take], folder))))
    check(len(rows) == 2 and rows[0]["File Name"] == name + ".mov", rows)
    r = rows[0]
    check(r["Scene"] == "12A" and r["Shot"] == "3" and r["Take"] == "4" and r["Good Take"] == "1", r)
    check(r["Comments"] == 'best one, "clap" late' and r["Markers"] == "15:30:20;11 clap", r)
    check(r["Start TC"] == tc["tc"] and r["Duration TC"] == "00:00:10;00", (r["Start TC"], r["Duration TC"]))
    check(r["End TC"] == timecode.offset_label(tc, 300), r["End TC"])
    takes.delete(folder, name)
    check(takes.load(folder, name) == {}, "sidecar not deleted")


# ------------------------------------------------------------------------- storage
@test("REC-12")
def storage_rates_and_the_disk_test(c):
    s = settings_mod.load()
    sig = {"present": True, "width": 3840, "height": 2160, "fps": 29.97, "format": "NV16"}
    raw = dict(s, mode="raw")
    need = storage.needed_rate(raw, sig)
    check(490e6 < need < 500e6, "4K 4:2:2 29.97 RAW needs ~497 MB/s, got %d" % need)
    h = dict(s, mode="h265", h265=dict(s["h265"], bitrate=80, rc="cbr", container="mov"))
    check(storage.needed_rate(h, sig) == 10_000_000 + storage.AUDIO_PCM, storage.needed_rate(h, sig))
    check(storage.needed_rate(h, {"present": False}) is None, "no signal, no rate")
    ok, why = storage.verdict(need, 400_000_000)
    check(ok is False and "400 MB/s" in why and "497 MB/s" in why, why)
    check(storage.verdict(need, 700_000_000) == (True, None), "a fast disk")
    check(storage.verdict(need, None)[0] is None and "not measured" in storage.verdict(need, None)[1], "unknown")
    check(storage.verdict(10_000_000, None) == (None, None), "no nagging for H.265")
    folder = os.path.join(c.dir, "disk")
    os.makedirs(folder)
    res = storage.measure(folder, max_bytes=64 << 20, seconds=1.0)
    check(res["speed"] > 1_000_000 and res["bytes"] > 0, res)
    check(not os.path.exists(os.path.join(folder, ".arstro-speed-test")), "test file left behind")
    check(storage.known(folder)["speed"] == res["speed"], "the result is not remembered")
    f = storage.figures(folder, raw, sig)
    check(f["need"] == need and f["seconds_left"] == int(f["free"] / need), f)


# ---------------------------------------------------------------------- monitoring
@test("REC-13")
def exposure_data_from_a_picture():
    np = monitor.np
    check(np is not None, "numpy is missing (python3-numpy)")
    # left half black (16), right half white (235), limited range
    y = np.full((144, 256), 16, np.uint8)
    y[:, 128:] = 235
    meta, payload = monitor.analyze(y, False)
    hist, wave, cells = payload[:128], payload[128:128 + 64 * 128], payload[128 + 64 * 128:]
    check(len(cells) == 256 * 144 and meta["gw"] == 256 and meta["gh"] == 144, meta)
    check(cells[0] == 0 and cells[200] == 200, "black = 0 %, white = 100 % (x 2)")
    check(hist[0] == 255 and hist[100] == 255 and sum(hist[1:100]) == 0, "two spikes in the histogram")
    row100 = int(100 * 64 / 110)
    check(wave[0 * 128 + 10] == 255 and wave[row100 * 128 + 100] == 255, "waveform rows: 0 % at row 0")
    check(wave[row100 * 128 + 10] == 0, "no white on the black side")
    check([b[2] for b in monitor.FALSE_COLOUR][-1] == "#ff2a2a", "clipped white is red")


@test("REC-13")
def luts_built_in_and_cube():
    np = monitor.np
    n = 9
    g = np.linspace(0, 1, n)
    ident = "TITLE \"id\"\nLUT_3D_SIZE %d\n" % n + "\n".join("%f %f %f" % (r, gg, b) for b in g for gg in g for r in g)
    t = monitor.LutTable.build("cube:id", cube_text=ident)
    w, h = 64, 36
    rng = np.random.default_rng(1)
    frame = np.concatenate([rng.integers(16, 236, w * h), rng.integers(112, 144, w * h // 2)]).astype(np.uint8)
    out = np.frombuffer(t.apply_i420(frame.tobytes(), w, h), np.uint8).astype(int)
    check(np.abs(out[:w * h] - frame[:w * h].astype(int)).mean() < 1.5, "an identity LUT changes the picture")
    sl = monitor.LutTable.build("slog3")
    grey = int(sl.ty[(105 >> 1) << 10 | 16 << 5 | 16])
    check(abs(grey - 106) <= 4, "S-Log3 grey (41 %%) should stay ~41 %% on Rec.709, got %d" % grey)
    for bad in ("LUT_3D_SIZE 2\n0 0 0\n", "hello", "LUT_3D_SIZE 2\n" + "0 0 0\n" * 7 + "x y z\n"):
        try:
            monitor.parse_cube(bad)
            raise AssertionError("accepted a broken .cube")
        except ValueError:
            pass
    check(monitor.check_lut_name("Sony S-Log3.cube") == "Sony S-Log3", "name")
    try:
        monitor.check_lut_name("../../etc/passwd")
        raise AssertionError("a path as a LUT name")
    except ValueError:
        pass


# ----------------------------------------------------------------------------- GPIO
class FakeLine:
    lines = {}

    def __init__(self, path, offset, consumer, mode="input", **kw):
        if (path, offset) == ("/dev/gpiochip9", 9):
            raise OSError("line busy")
        self.path, self.offset, self.consumer, self.mode, self.kw = path, offset, consumer, mode, kw
        self.r, self.w = os.pipe()
        self.fd = self.r
        self.value = 0
        self.events = []
        FakeLine.lines[(path, offset)] = self

    def press(self):
        self.events.append((time.time_ns(), "rising"))
        os.write(self.w, b"e")

    def read_events(self):
        os.read(self.r, 64)
        ev, self.events = self.events, []
        return ev

    def set(self, v):
        self.value = v

    def close(self):
        self.fd = None


class FakeGpio:
    Line = FakeLine


@test("REC-16")
def record_button_and_tally():
    presses = []
    g = RecorderGpio(lambda: presses.append(time.monotonic()), gpio_module=FakeGpio)
    try:
        st = g.configure({"button": "gpiochip1:5", "button_active_low": True, "tally": "1:6",
                          "tally_active_low": False})
        btn, tally = FakeLine.lines[("/dev/gpiochip1", 5)], FakeLine.lines[("/dev/gpiochip1", 6)]
        check(st["button"]["ok"] and st["tally"]["ok"], st)
        check(btn.consumer == "arstro-recorder" and btn.kw.get("edge") == "rising" and btn.kw.get("active_low"),
              btn.kw)
        check(btn.kw.get("bias") == "pull-up" and tally.mode == "output", (btn.kw, tally.mode))
        btn.press()
        check(wait_until(lambda: len(presses) == 1, 2), "the press did not reach the recorder")
        btn.press()                                   # bounce inside the debounce time: ignored
        time.sleep(0.2)
        check(len(presses) == 1, "a bounce counted as a second press")
        time.sleep(0.35)
        btn.press()
        check(wait_until(lambda: len(presses) == 2, 2), "the second press was lost")
        g.set_tally("on")
        check(tally.value == 1 and g.state["tally"]["on"], "tally not on while recording")
        g.set_tally("busy")
        seen = set()
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            seen.add(tally.value)
            time.sleep(0.05)
        check(seen == {0, 1}, "the tally does not blink while a take is finished: %s" % seen)
        g.set_tally("off")
        check(tally.value == 0, "tally still on")
        st = g.configure({"button": "gpiochip9:9", "tally": "x"})
        check(st["button"]["ok"] is False and "busy" in st["button"]["error"], st["button"])
        check(st["tally"]["ok"] is False and "chip:line" in st["tally"]["error"], st["tally"])
        check(parse_pin("") is None and parse_pin("2:17") == ("/dev/gpiochip2", 17), "parse_pin")
    finally:
        g.close()


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, __doc__))
