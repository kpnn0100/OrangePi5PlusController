"""Play a recording in any browser (GAL-11): a fragmented MP4 stream on stdout.

    python3 -m arstro_remote.recorder.play FILE --codec hevc|h264 [--start SECONDS]
                                           [--mode copy|h264] [--no-audio]

The web server runs one of these per viewer (ARC-06: a GStreamer crash ends only this
process) and pipes stdout to the browser, which plays it with Media Source Extensions.

  copy  the video as it is (H.265 or H.264, no re-encoding: full quality, almost no load)
        for browsers that decode it; only the audio becomes AAC (browsers take no PCM).
  h264  for browsers without H.265: the VPU decodes and re-encodes H.264. Its H.264 encoder
        manages ~15-19 fps at 4K on the RK3588 (measured), so this mode streams 15 fps.

Two pipelines: A reads (demux, the video chain, AAC) into appsinks and can be seeked; B
muxes what A gives into fragmented MP4. A fragmented mp4mux cannot take a flushing seek
once it has written its header (it restarts mid-stream), so B starts only after A's seek,
and its timestamps start at 0 (the keyframe the seek landed on).
"""

import argparse
import os
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

H264_FPS = 15                     # what the VPU's H.264 encoder keeps up with at 4K
H264_BPS = 12_000_000
MIN_FREE_MB = 1024                # stop before the board runs out of RAM (it freezes, no OOM kill):
                                  # MPP elements fed the wrong buffers leak GBs (RGA scaling: ~5 GB/20 s)


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            return next(int(l.split()[1]) // 1024 for l in f if l.startswith("MemAvailable:"))
    except (OSError, StopIteration, ValueError):
        return None
QUEUE = "queue max-size-buffers=0 max-size-bytes=0 max-size-time=2000000000"


def _demux(path):
    ext = os.path.splitext(path)[1].lower()
    return "matroskademux" if ext in (".mkv", ".webm") else "qtdemux"


def _aac():
    return "voaacenc bitrate=160000" if Gst.ElementFactory.find("voaacenc") else "avenc_aac bitrate=160000"


def reader_desc(path, codec, mode, audio):
    """Pipeline A: the file -> parsed H.265/H.264 access units + AAC, into appsinks."""
    parse = {"hevc": "h265parse", "h264": "h264parse"}[codec]
    if mode == "copy":
        caps = "video/x-h265,stream-format=hvc1,alignment=au" if codec == "hevc" else \
            "video/x-h264,stream-format=avc,alignment=au"
        video = f"{parse} ! {caps}"
    else:
        # integer frame rate for MPP's rate control; drop to what the encoder keeps up with
        video = (f"{parse} ! mppvideodec ! videorate drop-only=true max-rate={H264_FPS} ! "
                 f"capssetter caps=\"video/x-raw,framerate={H264_FPS}/1\" ! queue max-size-buffers=4 ! "
                 f"mpph264enc rc-mode=vbr bps={H264_BPS} bps-max={H264_BPS * 3 // 2} gop={H264_FPS} "
                 "max-reenc=0 zero-copy-pkt=true header-mode=each-idr profile=high ! "
                 "h264parse ! video/x-h264,stream-format=avc,alignment=au")
    sink = "appsink sync=false max-buffers=8 drop=false emit-signals=false"
    desc = f"filesrc name=src ! {_demux(path)} name=d d.video_0 ! {QUEUE} ! {video} ! {sink} name=vsink"
    if audio:
        desc += (f" d.audio_0 ! {QUEUE} ! decodebin ! audioconvert ! audioresample ! {_aac()} ! aacparse ! "
                 f"{sink} name=asink")
    return desc


def writer_desc(audio):
    """Pipeline B: appsrcs -> fragmented MP4 on stdout."""
    src = "appsrc format=time block=true max-bytes=33554432 is-live=false"
    # pads by name: the caps are only known later, so "! m." alone may pick the wrong pad
    desc = ("mp4mux name=m fragment-duration=500 streamable=true faststart=false ! fdsink fd=1 sync=false "
            f"{src} name=vsrc ! m.video_0")
    if audio:
        desc += f" {src} name=asrc ! m.audio_0"
    return desc


def play(path, start, mode, codec, audio):
    Gst.init(None)
    a = Gst.parse_launch(reader_desc(path, codec, mode, audio))
    a.get_by_name("src").set_property("location", path)
    b = Gst.parse_launch(writer_desc(audio))
    loop = GLib.MainLoop()
    state = {"code": 0, "base": None}
    base_ready = threading.Event()

    def fail(text):
        print("play: %s" % text, file=sys.stderr)
        state["code"] = 2
        GLib.idle_add(loop.quit)

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            if msg.src == b:
                loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            fail("%s (%s)" % (err.message, dbg))
    for p in (a, b):
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_message)

    a.set_state(Gst.State.PAUSED)
    ok, _st, _pend = a.get_state(15 * Gst.SECOND)
    if ok == Gst.StateChangeReturn.FAILURE:
        print("play: cannot open %s" % path, file=sys.stderr)
        return 2
    if start > 0:
        a.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(start * Gst.SECOND))
        a.get_state(15 * Gst.SECOND)

    def pump(sink_name, src_name, is_video):
        """Move samples A -> B, re-timed to start at the first video frame (0)."""
        sink, src = a.get_by_name(sink_name), b.get_by_name(src_name)
        caps_set = False
        try:
            while True:
                sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
                if sample is None:
                    if sink.get_property("eos"):
                        break
                    continue
                buf = sample.get_buffer()
                if is_video and state["base"] is None:
                    state["base"] = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0
                    base_ready.set()
                if not is_video:
                    base_ready.wait()
                base = state["base"] or 0
                if buf.pts == Gst.CLOCK_TIME_NONE or buf.pts < base:
                    continue                          # audio from before the first video frame
                if not caps_set:
                    src.set_property("caps", sample.get_caps())
                    caps_set = True
                out = buf.copy()
                out.pts = buf.pts - base
                if buf.dts != Gst.CLOCK_TIME_NONE:
                    out.dts = max(0, buf.dts - base) if buf.dts >= base else 0
                if src.emit("push-buffer", out) != Gst.FlowReturn.OK:
                    return
        finally:
            if is_video:
                base_ready.set()                       # no video at all: let the audio finish
            src.emit("end-of-stream")

    b.set_state(Gst.State.PLAYING)
    a.set_state(Gst.State.PLAYING)
    threads = [threading.Thread(target=pump, args=("vsink", "vsrc", True), daemon=True)]
    if audio:
        threads.append(threading.Thread(target=pump, args=("asink", "asrc", False), daemon=True))
    for t in threads:
        t.start()
    for sig in (15, 2):                               # SIGTERM / SIGINT: the viewer went away
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: loop.quit() or GLib.SOURCE_REMOVE)

    def guard():
        free = mem_available_mb()
        if free is not None and free < MIN_FREE_MB:
            fail("stopped: only %d MB of memory left" % free)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
    GLib.timeout_add(500, guard)
    try:
        loop.run()
    finally:
        a.set_state(Gst.State.NULL)
        b.set_state(Gst.State.NULL)
    return state["code"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arstro_remote.recorder.play")
    ap.add_argument("file")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--mode", choices=["copy", "h264"], default="copy")
    ap.add_argument("--codec", choices=["hevc", "h264"], required=True)
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args(argv)
    try:
        return play(a.file, max(0.0, a.start), a.mode, a.codec, not a.no_audio)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
