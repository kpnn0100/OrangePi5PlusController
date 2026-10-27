#!/usr/bin/env python3
"""Recorder + gallery tests against the running server, with a test-pattern source.

Run on the Pi (the server must be running):
    python3 tests/test_recorder.py [--source 1280x720@30]

Recordings go to a temporary folder; the source and settings are restored at the end,
so the user's own recordings are never touched. Light load on purpose (720p30): the
board is known to reset under heavy HDMI capture load.
"""

import argparse
import http.client as http_client
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from harness import check, nal_types, run, test, wait_until

from arstro_remote.client import Client
from arstro_remote.paths import control_socket_path, load_config
from arstro_remote.web import auth, ws

HERE = os.path.dirname(os.path.abspath(__file__))


class Ctx:
    pass



@test("CAM-01", "CAM-02", "REC-08")
def camera_source_is_chosen_and_kept(c):
    r = c.a.call("camera.sources")
    ids = [x["id"] for x in r["sources"]]
    check("hdmi" in ids and "test" in ids, ids)
    try:
        st = c.a.call("camera.select", source="test", spec="1280x720@30", timeout=60)
        check(st["source"] == "test:1280x720@30" and st["simulate"] == "1280x720@30", st.get("source"))
        check(load_config().get("recorder_simulate") == "1280x720@30", "the source is not saved")
        check(c.a.call("camera.sources")["current"] == "test:1280x720@30", "sources do not show the choice")
        r = c.a.request("camera.select", source="v4l2:/dev/video99")
        check(r["ok"] is False and "not connected" in r["error"], r)
    finally:
        c.a.call("recorder.source", simulate=c.source, timeout=60)

def setup():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="1280x720@30")
    args, _ = ap.parse_known_args()
    c = Ctx()
    c.a = Client.unix(control_socket_path())
    c.a.call("hello", app="test-recorder")
    c.b = Client.unix(control_socket_path())        # a second controller: must see everything live
    c.b.call("hello", app="test-observer")
    st = c.a.call("recorder.status")
    c.orig_source = st.get("simulate")
    c.orig_settings = c.a.call("recorder.settings.get")
    # The user's settings survive a run that never reached teardown (board froze / repowered):
    # they are saved first, and a slot still on a test folder gets them back from that file.
    backup = os.path.join(os.path.expanduser("~/.cache"),          # one per slot (by its socket)
                          "arstro-test-recorder-%s.json" % os.path.basename(control_socket_path()))
    if c.orig_settings.get("storage", "").startswith(os.path.join(tempfile.gettempdir(), "arstro-rec-test-")):
        try:
            with open(backup) as f:
                c.orig_settings, c.orig_source = json.load(f)
            print("      (an interrupted run left test settings; restoring the user's from %s)" % backup)
        except (OSError, ValueError):
            pass
    else:
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        with open(backup, "w") as f:
            json.dump([c.orig_settings, c.orig_source], f)
            f.flush()
            os.fsync(f.fileno())
    c.backup = backup
    c.tmp = tempfile.mkdtemp(prefix="arstro-rec-test-")
    c.a.call("recorder.settings.set", settings={"storage": c.tmp, "audio": {"record": False}})
    c.a.call("recorder.source", simulate=args.source, timeout=40)
    c.source = args.source
    c.port = int(load_config().get("web_port", 8080))
    c.base = "http://127.0.0.1:%d" % c.port
    c.token = auth.load()
    return c


def teardown(c):
    try:
        st = c.a.call("recorder.status")
        if st["recording"].get("active"):
            c.a.call("recorder.stop")
    finally:
        o = c.orig_settings
        c.a.call("recorder.settings.set", settings={k: o[k] for k in ("mode", "h265", "raw", "audio",
                                                                       "storage", "edid", "preview")})
        c.a.call("recorder.source", simulate=c.orig_source, timeout=40)
        try:
            os.remove(c.backup)                       # restored: nothing left to recover
        except OSError:
            pass
        c.a.close()
        c.b.close()
        shutil.rmtree(c.tmp, ignore_errors=True)


def http(c, path, headers=None):
    req = urllib.request.Request(c.base + path, headers=dict({"Authorization": "Bearer " + c.token},
                                                             **(headers or {})))
    return urllib.request.urlopen(req, timeout=20)


def take_with(c, pred, timeout=20):
    def find():
        for t in c.a.call("gallery.list")["takes"]:
            if pred(t):
                return t
        return None
    return wait_until(find, timeout, 0.5)


# ------------------------------------------------------------------- tests
@test("REC-08", "REC-01", "REC-07")
def source_is_test_pattern(c):
    st = wait_until(lambda: (lambda s: s if s["signal"].get("present") else None)(c.a.call("recorder.status")), 10)
    check(st, "no signal from the test source")
    check(st["simulate"] == c.source, st.get("simulate"))
    w, h = map(int, c.source.split("@")[0].split("x"))
    check((st["signal"]["width"], st["signal"]["height"]) == (w, h), st["signal"])
    check(st["caps"].get("vpu_h264") and st["caps"].get("vpu_h265"), st["caps"])
    check(not st["capture"]["running"], "capture should be idle with no viewer/recording (REC-06)")


@test("REC-02", "REC-06", "ARC-03")
def preview_stream_h264_with_keyframe(c):
    conn = ws.connect("ws://127.0.0.1:%d/ws/preview" % c.port, {"Authorization": "Bearer " + c.token})
    config, frames, first = None, [], None
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < 12 and len(frames) < 30:
            op, data = conn.recv_message()
            if op == ws.OP_TEXT:
                m = json.loads(data)
                if m["type"] == "config":
                    config = m
                continue
            ver, flags, pts = struct.unpack_from(">BBQ", data, 0)
            au = data[10:]
            if first is None:
                first = (flags, au, time.monotonic() - t0)
            frames.append(len(au))
        check(first is not None, "no video within 12 s")
        check(first[0] & 1, "first frame is not a keyframe")
        types = nal_types(first[1])
        check(7 in types and 8 in types and 5 in types, "keyframe lacks SPS/PPS/IDR: %s" % types)
        check(first[2] < 8, "first picture took %.1fs" % first[2])
        check(config and config["codec"] == "h264" and config["width"] <= 1280, config)
        check(len(frames) >= 30, "only %d frames" % len(frames))
        ok = c.b.wait_state("recorder", lambda d: d["preview"]["viewers"] == 1 and d["capture"]["running"], 5)
        check(ok, "observer did not see the viewer/capture state (ARC-03)")
    finally:
        conn.close()
    ok = c.b.wait_state("recorder", lambda d: d["preview"]["viewers"] == 0 and not d["preview"]["on"], 12)
    check(ok, "preview/encoder did not stop after the last viewer left (REC-06)")


@test("REC-02", "UX-02")
def preview_keeps_up_with_a_4k_source(c):
    """Smooth and live: a 4K 4:2:2 source (like a real camera) still gives >= 25 fps with
    no big gaps, at the low and medium presets."""
    c.a.call("recorder.source", simulate="3840x2160@30:NV16", timeout=60)
    try:
        check(wait_until(lambda: c.a.call("recorder.status")["signal"].get("present"), 20), "no test signal")
        for quality in ("low", "medium"):
            c.a.call("recorder.settings.set", settings={"preview": {"quality": quality}})
            conn = ws.connect("ws://127.0.0.1:%d/ws/preview" % c.port, {"Authorization": "Bearer " + c.token})
            try:
                times, t_end = [], None
                while True:
                    op, _data = conn.recv_message()
                    if op != ws.OP_BINARY:
                        continue
                    now = time.monotonic()
                    if t_end is None:
                        t_end = now + 6
                    if now > t_end:
                        break
                    times.append(now)
            finally:
                conn.close()
            gaps = sorted(b - a for a, b in zip(times, times[1:]))
            fps = len(times) / 6
            p95 = gaps[int(len(gaps) * 0.95)] if gaps else 1
            print("      %s: %.1f fps, 95%% gap %.0f ms" % (quality, fps, p95 * 1000))
            check(fps >= 25, "%s preview only %.1f fps from a 4K source" % (quality, fps))
            check(p95 < 0.1, "%s preview stutters: 95%% gap %.0f ms" % (quality, p95 * 1000))
    finally:
        c.a.call("recorder.settings.set", settings={"preview": c.orig_settings["preview"]})
        c.a.call("recorder.source", simulate=c.source, timeout=60)
        wait_until(lambda: c.a.call("recorder.status")["signal"].get("present"), 20)


@test("REC-02")
def second_viewer_gets_picture_quickly(c):
    a = ws.connect("ws://127.0.0.1:%d/ws/preview" % c.port, {"Authorization": "Bearer " + c.token})
    try:
        def first_key(conn, limit):
            t0 = time.monotonic()
            while time.monotonic() - t0 < limit:
                op, data = conn.recv_message()
                if op == ws.OP_BINARY and data[1] & 1:
                    return time.monotonic() - t0
            return None
        check(first_key(a, 12) is not None, "first viewer got no keyframe")
        b = ws.connect("ws://127.0.0.1:%d/ws/preview" % c.port, {"Authorization": "Bearer " + c.token})
        try:
            dt = first_key(b, 5)
            check(dt is not None and dt < 2.0, "joining viewer waited %s s for a picture" % dt)
        finally:
            b.close()
    finally:
        a.close()


@test("REC-04")
def settings_are_validated_and_shared(c):
    r = c.a.request("recorder.settings.set", settings={"h265": {"bitrate": 9999}})
    check(r["ok"] is False and "between" in r["error"], r)
    r = c.a.request("recorder.settings.set", settings={"mode": "jpeg"})
    check(r["ok"] is False, r)
    r = c.a.request("recorder.settings.set", settings={"nope": 1})
    check(r["ok"] is False, r)
    c.a.call("recorder.settings.set", settings={"mode": "h265", "h265": {"bitrate": 8, "container": "mp4"}})
    ok = c.b.wait_state("recorder.settings", lambda s: s["h265"]["bitrate"] == 8 and s["mode"] == "h265", 3)
    check(ok, "observer did not get the new settings (ARC-03)")


@test("REC-03", "REC-04", "ARC-03", "GAL-01", "GAL-07")
def record_h265_synced_everywhere(c):
    c.b.events.clear()
    r = c.a.call("recorder.start", timeout=30)
    check(r["file"].endswith(".mp4"), r)
    ok = c.b.wait_state("recorder", lambda d: d["recording"].get("active"), 3)
    check(ok, "observer did not see the recording start within 3 s (ARC-03)")
    ok = c.b.wait_state("recorder", lambda d: d["recording"].get("elapsed", 0) >= 2 and d["recording"]["size"] > 0, 8)
    check(ok, "no live progress (elapsed/size)")
    time.sleep(2)
    s = c.a.call("recorder.stop", timeout=50)
    check(s["file"] == r["file"] and s["size"] > 50_000, s)
    ok = c.b.wait_state("recorder", lambda d: not d["recording"].get("active"), 5)
    check(ok, "observer did not see the recording stop")
    path = os.path.join(c.tmp, r["file"])
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                       "-show_format", path], capture_output=True, text=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    check(v["codec_name"] == "hevc", v["codec_name"])
    dur = float(probe["format"]["duration"])
    check(2.5 <= dur <= 8, "duration %.1f" % dur)
    t = take_with(c, lambda t: any(i["id"] == r["file"] for i in t["items"]))
    check(t and t["items"][0]["kind"] == "H.265", t)
    c.h265_file = r["file"]
    c.h265_take = t["id"]
    ok = c.b.wait_state("gallery", lambda g: g["takes"] >= 1, 10)
    check(ok, "observer did not get a gallery update (GAL-07)")


@test("GAL-04", "GAL-05", "GAL-01", "ARC-03")
def convert_to_h264_share_with_live_progress(c):
    # a bitrate the VPU refuses is refused when asked for, not after three failed attempts
    for bitrate, why in ((300, "at most 200"), ("fast", "number"), (0.01, "between")):
        r = c.a.request("gallery.convert", file=c.h265_file, target="h264-vpu", bitrate=bitrate)
        check(r["ok"] is False and why in r["error"], (bitrate, r))
    t = c.a.call("gallery.targets")["targets"]
    check(next(x for x in t if x["id"] == "h264-vpu")["options"]["bitrate_max"] == 200, t)
    job = c.a.call("gallery.convert", file=c.h265_file, target="h264-vpu", scale="720")
    check(job["output"].endswith("_H264_720p.mp4"), job)
    saw_running = c.b.wait_state("jobs", lambda js: any(j["id"] == job["id"] and j["state"] in ("running", "done")
                                                        for j in js), 10)
    check(saw_running, "observer never saw the job")
    done = c.b.wait_state("jobs", lambda js: any(j["id"] == job["id"] and j["state"] in ("done", "failed")
                                                 for j in js), 90)
    check(done, "job did not finish")
    j = next(j for j in c.b.state["jobs"] if j["id"] == job["id"])
    check(j["state"] == "done", j)
    t = take_with(c, lambda t: t["id"] == c.h265_take and "H.264" in t["kinds"], 20)
    check(t, "take has no H.264 variant")
    item = next(i for i in t["items"] if i["kind"] == "H.264")
    check(item["height"] == 720 and item["codec"] == "h264", item)
    c.h264_file = item["id"]
    r = c.a.request("gallery.convert", file=c.h265_file, target="nope")
    check(r["ok"] is False, r)


@test("GAL-05", "ARC-03")
def a_running_conversion_is_cancelled(c):
    # every controller named the job `id` - the field that carries the request's own number - so
    # the server read the request counter instead: "no job 13", and cancel never worked
    job = c.a.call("gallery.convert", file=c.h265_file, target="h265-x265", quality="max", preset="slow")
    running = c.b.wait_state("jobs", lambda js: any(j["id"] == job["id"] and j["state"] == "running" for j in js), 15)
    check(running, "the job never ran: %s" % c.b.state.get("jobs"))
    r = c.a.request("jobs.cancel", job=job["id"])
    check(r["ok"] and r["data"]["id"] == job["id"], r)
    gone = c.b.wait_state("jobs", lambda js: any(j["id"] == job["id"] and j["state"] == "cancelled" for j in js), 15)
    check(gone, "the observer never saw the job cancelled: %s" % c.b.state.get("jobs"))
    r = c.a.request("jobs.cancel", job=987654)
    check(r["ok"] is False and "no job 987654" in r["error"], r)


@test("GAL-02", "GAL-03", "SEC-03")
def media_range_and_thumbnail(c):
    with http(c, "/api/media/" + c.h264_file, {"Range": "bytes=0-99"}) as r:
        check(r.status == 206 and len(r.read()) == 100, r.status)
        check(r.headers["Content-Range"].startswith("bytes 0-99/"), r.headers["Content-Range"])
    with http(c, "/api/thumb/" + c.h265_take) as r:
        data = r.read()
        check(r.headers["Content-Type"] == "image/jpeg" and data[:2] == b"\xff\xd8", r.headers["Content-Type"])
    try:
        urllib.request.urlopen(c.base + "/api/media/" + c.h264_file, timeout=5)
        check(False, "media without token was served")
    except urllib.error.HTTPError as e:
        check(e.code == 401, e.code)
    try:
        http(c, "/api/media/../../etc/passwd")
        check(False, "path traversal served")
    except urllib.error.HTTPError as e:
        check(e.code == 404, e.code)


@test("GAL-11", "GAL-03", "SEC-03")
def h265_streams_to_any_browser(c):
    t = c.a.call("gallery.get", take=c.h265_take)
    item = next(i for i in t["items"] if i["id"] == c.h265_file)
    check((item.get("mse") or "").startswith("hvc1."), "no MSE codec string: %s" % item.get("mse"))
    for mode, start, codec in (("copy", 0, "hevc"), ("h264", 1, "h264")):
        with http(c, "/api/play/%s?mode=%s&t=%s" % (urllib.parse.quote(c.h265_file), mode, start)) as r:
            check(r.status == 200 and r.headers["Content-Type"] == "video/mp4", r.status)
            data = r.read()
        check(data[4:8] == b"ftyp" and b"moov" in data[:4096] and b"moof" in data, "%s: not a fragmented MP4" % mode)
        path = tempfile.mktemp(suffix=".mp4")
        try:
            open(path, "wb").write(data)
            probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
                                    "-show_entries", "stream=codec_name,nb_read_frames", "-of", "csv=p=0", path],
                                   capture_output=True, text=True, timeout=30).stdout.strip()
        finally:
            os.remove(path)
        print("      %s from %ss: %s, %d bytes" % (mode, start, probe, len(data)))
        check(probe.startswith(codec) and int(probe.split(",")[-1]) > 0, probe)
    # one VPU conversion at a time: the same viewer's next request (a seek) replaces its stream,
    # another viewer is told the converter is busy
    path = "/api/play/%s?mode=h264" % urllib.parse.quote(c.h265_file)
    first = http(c, path)
    try:
        first.read(1000)
        second = http(c, path + "&t=2")
        try:
            got = second.read(1000)
            check(got[4:8] == b"ftyp", "the seek did not get a new stream: %r" % got[:80])
            other = http_client.HTTPConnection("127.0.0.1", c.port, timeout=20, source_address=("127.0.0.2", 0))
            other.request("GET", path, headers={"Authorization": "Bearer " + c.token})
            resp = other.getresponse()
            check(resp.status == 409 and b"busy" in resp.read(), resp.status)
            other.close()
        finally:
            second.close()
    finally:
        first.close()
    for bad in ("?mode=x265", ""):
        try:
            http(c, "/api/play/%s%s" % (urllib.parse.quote(c.h264_file if bad else "nope.mov"), bad))
            check(False, "served %s" % bad)
        except urllib.error.HTTPError as e:
            check(e.code in (404, 409), e.code)
    try:
        urllib.request.urlopen(c.base + "/api/play/" + urllib.parse.quote(c.h265_file), timeout=5)
        check(False, "stream without token was served")
    except urllib.error.HTTPError as e:
        check(e.code == 401, e.code)
    out = tempfile.mktemp(suffix=".mp4")
    try:                                              # the CLI saves the same stream
        r = subprocess.run([sys.executable, "-m", "arstro_remote", "gallery", "stream", c.h265_file, out,
                            "--start", "1"], cwd=os.path.join(HERE, ".."), capture_output=True, text=True, timeout=60)
        check(r.returncode == 0 and open(out, "rb").read(8)[4:8] == b"ftyp", r.stdout + r.stderr)
    finally:
        if os.path.exists(out):
            os.remove(out)
    check(wait_until(lambda: not subprocess.run(["pgrep", "-f", "arstro_remote.recorder.play"],
                                                capture_output=True).stdout, 10), "a play helper outlived its viewer")


@test("GAL-01", "GAL-12", "SEC-03")
def a_day_downloads_as_one_zip(c):
    import io
    import zipfile
    t = take_with(c, lambda t: t["id"] == c.h265_take)
    want_day = time.strftime("%Y-%m-%d", time.localtime(t["created"]))
    check(t["day"] == want_day and t["month"] == want_day[:7], "day/month %s %s" % (t.get("day"), t.get("month")))
    # one take, every format: the bundle names its files, the ZIP carries exactly them
    b = c.a.call("gallery.bundle", takes=[c.h265_take])
    check(sorted(b["files"]) == sorted(i["id"] for i in t["items"]) and b["name"] == "arstro-%s.zip" % want_day, b)
    with http(c, b["url"]) as r:
        blob = r.read()
        check(r.headers["Content-Type"] == "application/zip", r.headers["Content-Type"])
        check(int(r.headers["Content-Length"]) == len(blob), "the announced length is the real one")
        check(want_day in r.headers["Content-Disposition"], r.headers["Content-Disposition"])
    zf = zipfile.ZipFile(io.BytesIO(blob))
    check(zf.testzip() is None and sorted(zf.namelist()) == sorted(b["files"]), zf.namelist())
    for name in b["files"]:
        with http(c, "/api/media/" + name) as r:
            check(zf.read(name) == r.read(), "%s differs from /api/media" % name)
        check(zf.getinfo(name).compress_type == zipfile.ZIP_STORED, "stored, never recompressed")
    # a whole day, one format only
    b = c.a.call("gallery.bundle", day=want_day, kind="H.264")
    check(b["files"] and all("H264" in f for f in b["files"]) and c.h264_file in b["files"], b)
    # what is refused
    for params, why in (({}, "choose what to download"), ({"takes": ["no_such_take"]}, "no take"),
                        ({"files": ["no_such.mp4"]}, "no file"), ({"day": "1999-01-01"}, "nothing to download")):
        r = c.a.request("gallery.bundle", **params)
        check(r["ok"] is False and why in r["error"], (params, r))
    try:
        urllib.request.urlopen(c.base + b["url"], timeout=5)
        check(False, "a bundle without the password was served")
    except urllib.error.HTTPError as e:
        check(e.code == 401, e.code)
    try:
        http(c, "/api/bundle/not-a-bundle.zip")
        check(False, "an unknown bundle was served")
    except urllib.error.HTTPError as e:
        check(e.code == 404, e.code)
    # the CLI: a day into a folder, and the same as one ZIP
    out = tempfile.mkdtemp(prefix="arstro-dl-test-")
    try:
        env = dict(os.environ)
        cli = [sys.executable, "-m", "arstro_remote", "gallery", "download", "--day", want_day]
        r = subprocess.run(cli + ["--out", out], capture_output=True, text=True, env=env, timeout=120,
                           cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        check(r.returncode == 0 and set(b2 for b2 in os.listdir(out)) >= set(t_i["id"] for t_i in t["items"]),
              (r.returncode, r.stdout[-300:], r.stderr[-300:], os.listdir(out)))
        r = subprocess.run(cli + ["--kind", "H.264", "--zip", os.path.join(out, "day.zip")], capture_output=True,
                           text=True, env=env, timeout=120, cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        check(r.returncode == 0 and zipfile.ZipFile(os.path.join(out, "day.zip")).testzip() is None, r.stderr[-300:])
        check(c.h264_file in zipfile.ZipFile(os.path.join(out, "day.zip")).namelist(), "the CLI ZIP")
    finally:
        shutil.rmtree(out, ignore_errors=True)


@test("REC-04", "REC-03", "GAL-05")
def raw_recording_with_ffv1_copy_after(c):
    # keep the RAW here (replacing it is GAL-08's test): a job is "done" only after its check
    c.a.call("recorder.settings.set", settings={"mode": "raw", "raw": {"hq": False, "ffv1": True,
                                                                        "ffv1_engine": "cpu", "when": "after",
                                                                        "ffv1_replace_raw": False}})
    r = c.a.call("recorder.start", timeout=30)
    check(r["file"].endswith(".arh"), r)
    time.sleep(2)
    c.a.call("recorder.stop", timeout=60)
    ok = c.b.wait_state("jobs", lambda js: any(j["source"] == r["file"] and j["codec"] == "ffv1" and j["state"] == "done"
                                               for j in js), 120)
    check(ok, "FFV1 copy was not made after the recording: %s" % c.b.state.get("jobs"))
    t = take_with(c, lambda t: any(i["id"] == r["file"] for i in t["items"]) and "FFV1" in t["kinds"])
    check(t and t["kinds"][:1] == ["RAW"], t and t["kinds"])
    c.raw_take = t["id"]
    c.a.call("recorder.settings.set", settings={"mode": "h265", "raw": {"ffv1": False}})


@test("REC-04", "REC-03", "GAL-05")
def raw_recording_with_an_h264_copy_during(c):
    # the share copy is made by the VPU while the RAW is still being written (it follows the file)
    for bad in ({"h264_scale": "4k"}, {"h264_quality": "best"}):
        r = c.a.request("recorder.settings.set", settings={"raw": bad})
        check(r["ok"] is False, (bad, r))
    s = c.a.call("recorder.settings.set", settings={"mode": "raw", "raw": {"h264": True, "h264_scale": "720", "h264_quality": "high",
                                                                            "hq": False, "ffv1": False, "when": "during"}})
    check(s["raw"]["h264"] is True and s["raw"]["h264_scale"] == "720", s["raw"])
    check("+H.264" in c.a.call("recorder.status")["mode_text"], "mode text does not say the H.264 copy")
    r = c.a.call("recorder.start", timeout=30)
    following = c.b.wait_state("jobs", lambda js: any(j["source"] == r["file"] and j["codec"] == "h264-vpu" and j["live"]
                                                      for j in js), 10)
    time.sleep(2)
    c.a.call("recorder.stop", timeout=60)
    check(following, "no H.264 copy followed the recording: %s" % c.b.state.get("jobs"))
    ok = c.b.wait_state("jobs", lambda js: any(j["source"] == r["file"] and j["codec"] == "h264-vpu" and j["state"] == "done"
                                               for j in js), 120)
    check(ok, "the H.264 copy did not finish: %s" % c.b.state.get("jobs"))
    take = r["file"][:-4]
    t = take_with(c, lambda t: t["id"] == take and "H.264" in t["kinds"])
    check(t, "the take has no H.264 copy")
    item = t and next(i for i in t["items"] if i["kind"] == "H.264")
    check(item and item["height"] == 720 and item["codec"] == "h264", item)
    c.a.call("recorder.settings.set", settings={"mode": "h265", "raw": {"h264": False}})


@test("REC-05", "GAL-06")
def guards(c):
    r = c.a.call("recorder.start", timeout=30)
    try:
        x = c.a.request("recorder.start")
        check(x["ok"] is False and "already" in x["error"], x)
        x = c.a.request("gallery.delete", file=r["file"])
        check(x["ok"] is False and "recorded" in x["error"], x)
    finally:
        c.a.call("recorder.stop", timeout=50)
    c.a.call("recorder.test_signal", present=False)
    try:
        x = c.a.request("recorder.start")
        check(x["ok"] is False and "signal" in x["error"].lower(), x)
    finally:
        c.a.call("recorder.test_signal", present=True)
    wait_until(lambda: c.a.call("recorder.status")["signal"]["present"], 5)


@test("GAL-06", "GAL-07", "ARC-03")
def delete_one_format_then_the_take(c):
    c.a.call("gallery.delete", file=c.h264_file)
    t = take_with(c, lambda t: t["id"] == c.h265_take and "H.264" not in t["kinds"])
    check(t and "H.265" in t["kinds"], "H.265 variant should remain: %s" % (t and t["kinds"]))
    before = c.b.state["gallery"]["version"]
    c.a.call("gallery.delete_take", take=c.h265_take)
    check(not take_with(c, lambda t: t["id"] == c.h265_take, 2), "take still listed")
    ok = c.b.wait_state("gallery", lambda g: g["version"] > before, 10)
    check(ok, "observer did not see the deletion (GAL-07)")
    x = c.a.request("gallery.delete", file="nothing_here.mp4")
    check(x["ok"] is False, x)


def _jobs_for(c, take):
    return [j for j in c.a.call("jobs.list")["jobs"] if j["source"].startswith(take)]


def _record(c, seconds):
    r = c.a.call("recorder.start", timeout=30)
    time.sleep(seconds)
    c.a.call("recorder.stop", timeout=60)
    return r["file"].rsplit(".", 1)[0]


@test("GAL-08", "REC-04", "ARC-03")
def raw_is_deleted_only_after_a_verified_ffv1_copy(c):
    """Record RAW from a 4:2:2 source (like the real camera) with FFV1 after + delete RAW:
    the copy is checked byte for byte, then the RAW goes and the FFV1 is marked verified."""
    caps = c.a.call("recorder.status")["caps"]
    engines = ["cpu"] + (["gpu"] if caps.get("gpu_ffv1") else [])
    c.a.call("recorder.source", simulate="1280x720@30:NV16", timeout=60)
    try:
        check(wait_until(lambda: c.a.call("recorder.status")["signal"].get("present"), 20), "no test signal")
        for engine in engines:
            c.a.call("recorder.settings.set", settings={
                "mode": "raw", "audio": {"record": True},
                "raw": {"hq": False, "ffv1": True, "ffv1_engine": engine, "when": "after", "ffv1_replace_raw": True}})
            take = _record(c, 3)
            done = wait_until(lambda: [j for j in _jobs_for(c, take) if j["state"] in ("done", "failed", "cancelled")
                                       and j["codec"].startswith("ffv1")], 240, 1)
            check(done, "%s FFV1 job did not finish" % engine)
            j = done[0]
            check(j["state"] == "done" and j["verify"] and j["verify"]["ok"], "%s: %s" % (engine, j))
            check(wait_until(lambda: j["verify"].get("raw_deleted") or any(
                x["verify"] and x["verify"].get("raw_deleted") for x in _jobs_for(c, take)), 10), "RAW not deleted")
            t = c.a.call("gallery.get", take=take)
            kinds = [i["kind"] for i in t["items"]]
            check("RAW" not in kinds and "FFV1" in kinds, kinds)
            ffv1 = next(i for i in t["items"] if i["kind"] == "FFV1")
            check(ffv1["verified"] and ffv1["verified"]["raw"] == take + ".arh", ffv1)
            check(not os.path.exists(os.path.join(c.tmp, take + ".arh")), "RAW file still on disk")
            print("      %s: %s" % (engine, j["verify"]["detail"]))
            # the observer controller saw the same job result (ARC-03)
            check(wait_until(lambda: any((x.get("verify") or {}).get("raw_deleted")
                                         for x in (c.b.state.get("jobs") or []) if x["id"] == j["id"]), 5),
                  "second controller did not see the verified job")
            c.a.call("gallery.delete_take", take=take)
    finally:
        c.a.call("recorder.settings.set", settings={"raw": c.orig_settings["raw"], "audio": {"record": False}})
        c.a.call("recorder.source", simulate=c.source, timeout=60)
        wait_until(lambda: c.a.call("recorder.status")["signal"].get("present"), 20)


@test("GAL-08", "GAL-06")
def a_changed_pixel_is_caught_and_the_raw_is_kept(c):
    """One flipped byte in the RAW: the check names the frame/plane/pixel and deletes nothing.
    The untouched pair passes, and the on-demand check can then delete the RAW."""
    from arstro_remote.recorder import arh, verify
    c.a.call("recorder.settings.set", settings={"mode": "raw", "raw": {"hq": False, "ffv1": False}})
    try:
        take = _record(c, 2)
        raw = os.path.join(c.tmp, take + ".arh")
        job = c.a.call("gallery.convert", file=take + ".arh", target="ffv1", replace_raw=False)
        check(job["options"].get("replace_raw") is False, job["options"])
        check(wait_until(lambda: [j for j in _jobs_for(c, take) if j["id"] == job["id"] and j["state"] == "done"],
                         120, 1), "FFV1 conversion did not finish")
        check(os.path.exists(raw), "RAW deleted although replace_raw was off")
        ffv1 = os.path.join(c.tmp, take + "_FFV1.mkv")
        good = verify.verify(raw, ffv1)
        check(good["ok"], good)
        # flip one byte of the 3rd frame, in the chroma plane
        r = arh.ArhReader(raw)
        offs = []
        with open(raw, "rb") as f:
            pos = arh.HEADER_SIZE
            while len(offs) < 3:
                f.seek(pos)
                ctype, psize, _pts, _seq, _a, _b = arh.CHUNK.unpack(f.read(arh.CHUNK.size))
                if ctype == arh.VFRM:
                    offs.append(pos + arh.CHUNK.size)
                pos += arh.CHUNK.size + psize
        target = offs[2] + r.video["plane_offsets"][1] + 1000
        with open(raw, "r+b") as f:
            f.seek(target)
            b = f.read(1)
            f.seek(target)
            f.write(bytes([b[0] ^ 0x01]))
        bad = verify.verify(raw, ffv1)
        check(not bad["ok"] and "frame 2" in bad["detail"] and "plane U" in bad["detail"], bad)
        job = c.a.call("gallery.verify", file=take + "_FFV1.mkv", delete_raw=True)
        fin = wait_until(lambda: [j for j in _jobs_for(c, take) if j["id"] == job["id"]
                                  and j["state"] in ("done", "failed")], 120, 1)
        check(fin and fin[0]["verify"] and not fin[0]["verify"]["ok"], fin)
        check(os.path.exists(raw), "RAW deleted although the copy differs!")
        print("      caught: %s" % fin[0]["verify"]["detail"])
        # put the byte back: now the on-demand check passes and may delete the RAW
        with open(raw, "r+b") as f:
            f.seek(target)
            f.write(b)
        job = c.a.call("gallery.verify", file=take + "_FFV1.mkv", delete_raw=True)
        fin = wait_until(lambda: [j for j in _jobs_for(c, take) if j["id"] == job["id"] and j["state"] == "done"], 120, 1)
        check(fin and fin[0]["verify"]["ok"], fin)
        check(wait_until(lambda: not os.path.exists(raw), 10), "verified RAW not deleted")
        c.a.call("gallery.delete_take", take=take)
    finally:
        c.a.call("recorder.settings.set", settings={"raw": c.orig_settings["raw"], "mode": c.orig_settings["mode"]})


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, __doc__))
