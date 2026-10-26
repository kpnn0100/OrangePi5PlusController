#!/usr/bin/env python3
"""The VPU encoder behind the H.264 / H.265 conversions (GAL-04) - needs no server.

    python3 tests/test_mppenc.py [-k NAME]

Runs only where librockchip_mpp is installed (the Rockchip boards); elsewhere every test
passes as "skipped". It checks that frames go through the VPU unchanged in number and
order (also with a width whose rows need re-striding), that keyframes come at the GOP,
that a whole conversion keeps every frame and the audio, runs faster than real time, and
that a failing encoder fails the job instead of hanging it.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from harness import check, run, test  # noqa: E402

from arstro_remote.recorder import mppenc  # noqa: E402

Gst.init(None)
SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def skip():
    if not mppenc.available():
        print("    skipped: no librockchip_mpp on this machine")
        return True
    return False


def nv12_frames(w, h, n):
    """n NV12 frames (GStreamer layout) of a moving test pattern, with their strides/offsets."""
    p = Gst.parse_launch(f"videotestsrc pattern=ball num-buffers={n} ! "
                         f"video/x-raw,format=NV12,width={w},height={h},framerate=30/1 ! appsink name=s sync=false")
    s = p.get_by_name("s")
    p.set_state(Gst.State.PLAYING)
    frames, layout = [], None
    while True:
        sample = s.emit("try-pull-sample", 5 * Gst.SECOND)
        if sample is None:
            break
        if layout is None:
            from gi.repository import GstVideo
            vi = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
            layout = (list(vi.stride)[:2], list(vi.offset)[:2])
        b = sample.get_buffer()
        frames.append(b.extract_dup(0, b.get_size()))
    p.set_state(Gst.State.NULL)
    return frames, layout


def decoded_frames(path, parse="h264parse"):
    """Frames a decoder gets out of an Annex-B file (0 if it is not decodable)."""
    p = Gst.parse_launch(f"filesrc location={path} ! {parse} ! decodebin ! fakesink name=s sync=false")
    n = [0]
    p.get_by_name("s").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,
                                                        lambda *_: (n.__setitem__(0, n[0] + 1), Gst.PadProbeReturn.OK)[1])
    p.set_state(Gst.State.PLAYING)
    msg = p.get_bus().timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    return n[0] if msg and msg.type == Gst.MessageType.EOS else 0


@test("GAL-04")
def frames_go_through_the_vpu_unchanged(_t):
    if skip():
        return
    with tempfile.TemporaryDirectory() as d:
        # 1366 is not a multiple of 16: the rows are re-strided for the VPU
        for codec, (w, h) in (("h264", (1280, 720)), ("h264", (1366, 768)), ("h265", (1920, 1080))):
            frames, (strides, offsets) = nv12_frames(w, h, 75)
            enc = mppenc.Encoder(codec, w, h, 30, gop=30, qp=26)
            aus = []
            try:
                for f in frames:
                    aus += enc.encode(f, strides, offsets)
            finally:
                enc.close()
            check(len(aus) == 75, "%s %dx%d: %d access units for 75 frames" % (codec, w, h, len(aus)))
            keys = [i for i, (_au, k) in enumerate(aus) if k]
            check(keys == [0, 30, 60], "%s %dx%d: keyframes at %s, not every 30" % (codec, w, h, keys))
            path = os.path.join(d, "t." + codec)
            with open(path, "wb") as f:
                f.write(b"".join(au for au, _k in aus))
            n = decoded_frames(path, "h264parse" if codec == "h264" else "h265parse")
            check(n == 75, "%s %dx%d: the stream decodes to %d frames" % (codec, w, h, n))


def make_clip(path, seconds=6):
    """A 1080p30 H.264 + AAC clip to convert (x264, so the test does not depend on the VPU)."""
    n = seconds * 30
    aac = "voaacenc" if Gst.ElementFactory.find("voaacenc") else "avenc_aac"
    p = Gst.parse_launch(
        f"videotestsrc pattern=ball num-buffers={n} ! video/x-raw,width=1920,height=1080,framerate=30/1 ! "
        f"x264enc speed-preset=ultrafast key-int-max=30 ! h264parse ! mp4mux name=m ! filesink location={path} "
        f"audiotestsrc num-buffers={seconds * 44100 // 1024} samplesperbuffer=1024 ! audio/x-raw,rate=44100 ! "
        f"audioconvert ! {aac} ! aacparse ! m.")
    p.set_state(Gst.State.PLAYING)
    p.get_bus().timed_pop_filtered(120 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    return n


def streams(path):
    """[(type, frames or None, duration s)] of a file, from ffprobe."""
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                        "stream=codec_type,nb_read_frames,duration", "-of", "json", path],
                       capture_output=True, text=True, timeout=120)
    return [(s["codec_type"], int(s["nb_read_frames"]) if s.get("nb_read_frames", "N/A").isdigit() else None,
             float(s.get("duration", 0))) for s in json.loads(r.stdout or "{}").get("streams", [])]


def convert(src, out, *opts, timeout=300):
    """Run the conversion worker; returns (last JSON line, seconds)."""
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "arstro_remote.recorder.transcode", src, "-o", out, *opts],
                       capture_output=True, text=True, timeout=timeout, cwd=SERVER)
    lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    return (lines[-1] if lines else {"state": "?", "error": r.stderr[-400:]}), time.monotonic() - t0


@test("GAL-04")
def a_conversion_keeps_every_frame_and_beats_real_time(_t):
    if skip():
        return
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "REC_20260101_000000_H264.mp4")
        n = make_clip(src)
        for codec, opts, ext in (("h264-vpu", ["--scale", "720"], ".mp4"),
                                 ("h264-vpu", ["--bitrate", "8", "--rc", "vbr"], ".mp4"),
                                 ("h265-vpu", [], ".mov")):
            out = os.path.join(d, "out_%s_%d%s" % (codec, len(opts), ext))
            last, secs = convert(src, out, "--codec", codec, *opts)
            check(last.get("state") == "done", "%s %s: %s" % (codec, opts, last))
            check(last.get("frames") == n, "%s %s: %s frames of %d" % (codec, opts, last.get("frames"), n))
            got = streams(out)
            video = [s for s in got if s[0] == "video"]
            audio = [s for s in got if s[0] == "audio"]
            check(video and video[0][1] == n, "%s %s: the file holds %s video frames" % (codec, opts, video))
            check(audio and abs(audio[0][2] - video[0][2]) < 0.1, "%s %s: audio %s vs video %s" % (codec, opts, audio, video))
            # 6 s of 1080p30 - the old one-frame-at-a-time element needed ~20 s for this
            check(secs < 6, "%s %s: took %.1f s, slower than real time" % (codec, opts, secs))


@test("GAL-04")
def a_bitrate_above_the_vpu_limit_is_capped_and_said(_t):
    # 300 Mbit/s made the whole configuration fail (MPP_ERR_VALUE) - a job saved with it must
    # still finish, at the VPU's 200, and say so
    if skip():
        return
    frames, (strides, offsets) = nv12_frames(3840, 2160, 10)
    for codec in ("h264", "h265"):
        for rc in ("vbr", "cbr"):
            enc = mppenc.Encoder(codec, 3840, 2160, 30, bps=300_000_000, rc=rc)
            try:
                check(enc.capped == 300_000_000, "%s %s: not marked as capped" % (codec, rc))
                n = sum(len(enc.encode(f, strides, offsets)) for f in frames)
            finally:
                enc.close()
            check(n == 10, "%s %s: %d access units for 10 frames" % (codec, rc, n))
    notes = []
    p = Gst.parse_launch("videotestsrc num-buffers=5 ! video/x-raw,format=NV12,width=640,height=360,"
                         "framerate=30/1 ! " + mppenc.fragment("h264") + " ! fakesink")
    bridge = mppenc.Bridge(p, "h264", 30, bps=300_000_000, on_note=notes.append)
    p.set_state(Gst.State.PLAYING)
    bridge.start()
    msg = p.get_bus().timed_pop_filtered(15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    bridge.stop()
    p.set_state(Gst.State.NULL)
    check(msg is not None and msg.type == Gst.MessageType.EOS, "the capped stream did not finish")
    check(notes and "encoded at 200 Mbit/s" in notes[0], "no note for the job: %s" % notes)


@test("GAL-04")
def a_failing_encoder_fails_the_job(_t):
    if skip():
        return

    class Broken(mppenc.Encoder):
        def encode(self, *_a):
            raise mppenc.MppError("VPU encoder: put frame failed (-1)")

    real = mppenc.Encoder
    mppenc.Encoder = Broken
    try:
        p = Gst.parse_launch("videotestsrc num-buffers=30 ! video/x-raw,format=NV12,width=640,height=360,"
                             "framerate=30/1 ! " + mppenc.fragment("h264") + " ! fakesink")
        bridge = mppenc.Bridge(p, "h264", 30, qp=26)
        p.set_state(Gst.State.PLAYING)
        bridge.start()
        msg = p.get_bus().timed_pop_filtered(15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        bridge.stop()
        p.set_state(Gst.State.NULL)
    finally:
        mppenc.Encoder = real
    check(msg is not None, "the pipeline hung instead of failing")
    check(msg.type == Gst.MessageType.ERROR and "put frame failed" in msg.parse_error()[0].message,
          "expected the encoder's error, got %s" % (msg.type if msg else None))


if __name__ == "__main__":
    GLib.set_prgname("test_mppenc")
    raise SystemExit(run(description=__doc__))
