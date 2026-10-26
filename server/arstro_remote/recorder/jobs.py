"""Background conversions (GAL-04/05): HQ H.265, H.264 "share" MP4, FFV1.

Each job runs in its own low-priority process (`python3 -m arstro_remote.recorder.transcode`),
so a slow CPU encode never competes with the capture for the GIL, and a crash
in an encoder cannot take the recorder down. With --follow a job starts while
the .arh is still being recorded and catches up after the recording ends.

The worker prints one JSON object per line on stdout for progress.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from . import arh, mppenc  # noqa: E402
from .gstutil import gst_buffer  # noqa: E402

CODECS = {
    "h264-vpu": ("H.264 MP4 (VPU, share)", "_H264.mp4"),
    "h265-vpu": ("H.265 HQ (VPU)", "_H265.mov"),
    "h265-x265": ("H.265 HQ (x265)", "_H265.mov"),
    "ffv1": ("FFV1 lossless", "_FFV1.mkv"),
    "ffv1-gpu": ("FFV1 lossless (GPU)", "_FFV1.mkv"),
}
X265_CRF = {"high": 18, "higher": 14, "max": 10}
VPU_QP = {"high": 22, "higher": 18, "max": 14}
X265_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]

# raw HDMI RX pixel format -> (4:2:0 format, format keeping the source chroma, FFV1 format)
FORMATS = {
    "NV12": ("I420", "I420", "I420"),
    "NV21": ("I420", "I420", "I420"),
    "NV16": ("I420", "Y42B", "Y42B"),
    "NV61": ("I420", "Y42B", "Y42B"),
    "YUY2": ("I420", "Y42B", "Y42B"),
    "UYVY": ("I420", "Y42B", "Y42B"),
    "NV24": ("I420", "Y444", "Y444"),
    "BGR": ("I420", "Y444", "BGRx"),
    "RGB": ("I420", "Y444", "BGRx"),
}


SCALES = {"source": None, "1080": (1920, 1080), "720": (1280, 720)}


def _base(path):
    """REC_x_H265.mov -> REC_x (the take's base name, without variant suffixes)."""
    import re
    root = os.path.splitext(path)[0]
    return re.sub(r"(?:_(?:H265|FFV1|H264)(?:_\d+p)?)+$", "", root)


def output_path(src, codec, scale=None):
    suffix = CODECS[codec][1]
    if codec == "h264-vpu" and scale in ("1080", "720"):
        suffix = "_H264_%sp.mp4" % scale
    return _base(src) + suffix


def _scale_desc(scale, fmt="I420"):
    wh = SCALES.get(scale or "source")
    if not wh:
        return ""
    w, h = wh
    return (f"videoscale method=bilinear n-threads=4 ! "
            f"video/x-raw,width={w},height={h},pixel-aspect-ratio=1/1 ! ")


# ---------------------------------------------------------------- worker process

H264_QP = {"high": 23, "higher": 20, "max": 17}


def _vpu_rate(codec, quality, bitrate, rc):
    """Encoder rate settings of a VPU job: {"qp"} (constant quality) or {"bps", "rc"}."""
    if bitrate:
        return {"bps": int(bitrate * 1e6), "rc": "cbr" if rc == "cbr" else "vbr"}
    return {"qp": (H264_QP if codec == "h264-vpu" else VPU_QP)[quality]}


def _vpu_bridge(pipe, args, fps):
    """The VPU encoder between `mppin` and `mppout` when the pipeline has them (made before
    the pipeline starts, started after)."""
    if not pipe.get_by_name("mppin"):
        return None
    return mppenc.Bridge(pipe, "h264" if args.codec == "h264-vpu" else "h265", fps,
                         **_vpu_rate(args.codec, args.quality, args.bitrate, args.rc))


def _encoder_desc(codec, fmt, fps, quality, preset, chroma, bitrate=None, rc="vbr"):
    """Return (input format the encoder needs, encoder pipeline fragment).
    bitrate (Mbit/s): target bitrate instead of constant quality.

    The VPU codecs go through mppenc (librockchip_mpp directly, ~20x the frame rate of
    gst-rockchip's one-frame-at-a-time mpph26xenc here); the elements are the fallback."""
    gop = max(1, round(fps))
    f420, fsrc, fffv1 = FORMATS.get(fmt, ("I420", "I420", "I420"))
    if codec in ("h264-vpu", "h265-vpu") and mppenc.available():
        return "NV12", mppenc.fragment("h264" if codec == "h264-vpu" else "h265")
    if codec == "h264-vpu":
        if bitrate:
            bps = int(bitrate * 1e6)
            rate = (f"rc-mode=cbr bps={bps}" if rc == "cbr" else
                    f"rc-mode=vbr bps={bps} bps-max={int(bps * 1.25)} bps-min={bps // 2}")
        else:
            qp = H264_QP[quality]
            rate = f"rc-mode=fixqp qp-init={qp} qp-min={qp} qp-max={qp}"
        return "I420" if fmt == "NV12" else "NV12", (
            f"capssetter caps=\"video/x-raw,framerate={max(1, round(fps))}/1\" ! "
            f"mpph264enc {rate} gop={gop} header-mode=each-idr profile=high ! h264parse")
    if codec == "h265-vpu":
        if bitrate:
            bps = int(bitrate * 1e6)
            rate = (f"rc-mode=cbr bps={bps}" if rc == "cbr" else
                    f"rc-mode=vbr bps={bps} bps-max={int(bps * 1.25)} bps-min={bps // 2}")
        else:
            qp = VPU_QP[quality]
            rate = f"rc-mode=fixqp qp-init={qp} qp-min={qp} qp-max={qp}"
        # never hand mpph265enc plain system memory directly (it leaks, see gstutil):
        # a real videoconvert step writes into the encoder's own buffers instead
        # (the capssetter works around mpph265enc mis-handling fractional frame rates)
        return "I420" if fmt == "NV12" else "NV12", (
            f"capssetter caps=\"video/x-raw,framerate={max(1, round(fps))}/1\" ! "
            f"mpph265enc {rate} gop={gop} header-mode=each-idr ! h265parse")
    if codec == "h265-x265":
        # x265enc's own bitrate property (kbit/s) selects ABR; without it, CRF (quality)
        rate = (f"bitrate={int(bitrate * 1000)}" if bitrate else
                f"option-string=crf={X265_CRF[quality]}")
        return (fsrc if chroma == "source" else f420,
                f"x265enc speed-preset={preset} key-int-max={gop} {rate} ! h265parse")
    return fffv1, "avenc_ffv1 slices=24 slicecrc=1 threads=0 gop-size=1 coder=range_def context=1"


def _default_layout(v):
    """True when the frames use GStreamer's default plane layout for their format."""
    vi = GstVideo.VideoInfo.new()
    vi.set_format(GstVideo.VideoFormat.from_string(v["format"]), v["width"], v["height"])
    n = vi.finfo.n_planes
    return (vi.size == v["frame_size"] and list(vi.offset)[:n] == v["plane_offsets"]
            and list(vi.stride)[:n] == v["plane_strides"])


def transcode(args):
    """Entry point of the worker process. Returns the exit code.

    The output is locked for the whole run (arh.file_lock): the other A/B slot, or a job
    resumed after a restart, can never write the same file at the same time."""
    try:
        os.nice(args.nice)
    except OSError:
        pass
    if args.cpus:          # threads=0 encoders (FFV1) size themselves to the CPUs they may use
        try:
            cpus = sorted(os.sched_getaffinity(0))
            os.sched_setaffinity(0, cpus[:max(1, args.cpus)])
        except (AttributeError, OSError):
            pass
    out = args.output or output_path(args.input, args.codec, args.scale)
    with arh.file_lock(out) as mine:
        if not mine:
            print(json.dumps({"state": "failed", "error": "%s is being made by another job (the other slot?)"
                              % os.path.basename(out)}), flush=True)
            return 4
        if args.input.lower().endswith(".arh") and not args.follow:
            try:   # an interrupted recording (power loss): finalize it before reading (GAL-09)
                note = arh.repair_if_needed(args.input, lambda m: print(m, file=sys.stderr))
                if note:
                    print(json.dumps({"state": "running", "frames": 0, "total": note["frames"], "fps": 0,
                                      "repaired": note}), flush=True)
            except (OSError, ValueError) as e:
                print("repair of %s failed: %s - reading around the damage" % (args.input, e), file=sys.stderr)
        return _transcode(args)


def _damage(reader):
    """{"ranges", "bytes"} the reader skipped, or None."""
    d = sorted(reader.damaged)
    return {"ranges": [list(x) for x in d[:5]], "bytes": sum(b - a for a, b in d)} if d else None


def _transcode(args):
    Gst.init(None)
    if args.codec == "ffv1-gpu":
        return transcode_ffv1_gpu(args)
    if not args.input.lower().endswith(".arh"):
        return transcode_media(args)

    def emit(**kw):
        print(json.dumps(kw), flush=True)

    try:
        reader = arh.ArhReader(args.input, wait=30 if args.follow else 0)
    except (OSError, ValueError) as e:
        emit(state="failed", error=str(e))
        return 2
    v, a = reader.video, reader.audio if not args.no_audio else None
    fps = reader.fps
    out = args.output or output_path(args.input, args.codec, args.scale)
    part = out + ".part"
    mux = _mux(args.codec)

    need, enc = _encoder_desc(args.codec, v["format"], fps, args.quality, args.preset, args.chroma,
                               args.bitrate, args.rc)
    scale = _scale_desc(args.scale)
    direct = _default_layout(v)
    if direct:
        # frames can go straight in; the feeder stamps them at a constant frame rate
        cm = v.get("colorimetry", "")
        cm = f",colorimetry={cm}" if cm.isalnum() else ""
        src = (f"appsrc name=vsrc format=time block=true max-bytes={v['frame_size'] * 3} "
               f"caps=video/x-raw,format={v['format']},width={v['width']},height={v['height']},"
               f"framerate={v['fps_n']}/{v['fps_d']}{cm} ! ")
    else:
        strides = ",".join(str(x) for x in v["plane_strides"])
        offsets = ",".join(str(x) for x in v["plane_offsets"])
        src = (f"appsrc name=vsrc format=bytes block=true max-bytes={v['frame_size'] * 3} ! "
               f"rawvideoparse use-sink-caps=false width={v['width']} height={v['height']} "
               f"format={v['format'].lower()} framerate={v['fps_n']}/{v['fps_d']} "
               f"plane-strides=\"<{strides}>\" plane-offsets=\"<{offsets}>\" "
               f"frame-size={v['frame_size']} ! ")
    conv = "" if need == v["format"] and direct and not scale else \
        f"{scale}videoconvert n-threads=4 ! video/x-raw,format={need} ! "
    desc = (f"{src}queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! {conv}{enc} ! "
            f"identity name=counter ! {mux} name=mux ! filesink location=\"{part}\"")
    if a:
        desc += (f" appsrc name=asrc format=time block=true max-bytes=2000000 "
                 f"caps=audio/x-raw,format={a['format']},rate={a['rate']},"
                 f"channels={a['channels']},layout=interleaved ! "
                 "queue max-size-time=0 max-size-bytes=0 max-size-buffers=0 ! " +
                 (_audio_tail(args.codec) or "audioconvert ! mux."))
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error as e:
        emit(state="failed", error=f"pipeline: {e.message}")
        return 2

    loop = GLib.MainLoop()
    stop = threading.Event()
    result = {"code": 1, "error": None}
    encoded = [0]
    t0 = time.monotonic()

    def count(_pad, _info):
        encoded[0] += 1
        return Gst.PadProbeReturn.OK
    pipe.get_by_name("counter").get_static_pad("src").add_probe(
        Gst.PadProbeType.BUFFER, count)

    frame_ns = 10**9 * v["fps_d"] / v["fps_n"]
    first_pts = [None]
    first_ready = threading.Event()

    def feed_video():
        src = pipe.get_by_name("vsrc")
        n = [0]

        def push(data):
            buf = gst_buffer(data)
            if direct:
                buf.pts, buf.duration = int(n[0] * frame_ns), int(frame_ns)
            n[0] += 1
            return src.emit("push-buffer", buf) == Gst.FlowReturn.OK
        try:
            # constant frame rate: capture drops repeat the previous frame (the check does the same)
            for pts, _seq, data, _rep in arh.cfr_frames(reader, args.follow, stop=stop):
                if first_pts[0] is None:
                    first_pts[0] = pts
                    first_ready.set()
                if not push(data):
                    return
        except (OSError, ValueError) as e:
            result["error"] = str(e)
        finally:
            first_ready.set()
            src.emit("end-of-stream")

    def feed_audio():
        src = pipe.get_by_name("asrc")
        try:
            first_ready.wait()
            base = first_pts[0] or 0
            for _t, pts, _seq, data in reader.chunks((arh.AUDS,), args.follow, stop=stop):
                if pts < base:
                    continue
                buf = gst_buffer(data)
                buf.pts = pts - base
                buf.duration = int(len(data) / (a["channels"] * 2) * 1e9 / a["rate"])
                if src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                    return
        except (OSError, ValueError) as e:
            result["error"] = str(e)
        finally:
            src.emit("end-of-stream")

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            result["code"] = 0 if not result["error"] else 2
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            result["error"] = f"{err.message} ({dbg})"
            result["code"] = 2
            loop.quit()

    def report():
        dt = time.monotonic() - t0
        try:
            total = reader.estimate_frames() if not reader.finalized else reader.frame_count
            if args.follow and not reader.finalized:
                reader.reload_header()
        except (OSError, ValueError):
            total = 0
        emit(state="running", frames=encoded[0], total=max(total, encoded[0]),
             fps=round(encoded[0] / dt, 2) if dt > 0 else 0,
             live=args.follow and not reader.finalized)
        return True

    def on_term():
        stop.set()
        result["error"] = "cancelled"
        result["code"] = 3
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, on_term)

    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    vpu = _vpu_bridge(pipe, args, fps)
    pipe.set_state(Gst.State.PLAYING)
    if vpu:
        vpu.start()
    threads = [threading.Thread(target=feed_video, daemon=True)]
    if a:
        threads.append(threading.Thread(target=feed_audio, daemon=True))
    for t in threads:
        t.start()
    GLib.timeout_add(500, report)
    emit(state="running", frames=0, total=reader.estimate_frames(), fps=0, output=out)
    loop.run()
    stop.set()
    if vpu:
        vpu.stop()
    pipe.set_state(Gst.State.NULL)

    if result["code"] == 0:
        os.replace(part, out)
        emit(state="done", frames=encoded[0], output=out,
             seconds=round(time.monotonic() - t0, 1), damaged=_damage(reader))
    else:
        try:
            os.remove(part)
        except OSError:
            pass
        emit(state="cancelled" if result["code"] == 3 else "failed",
             error=result["error"] or "unknown error")
    return result["code"]


def transcode_ffv1_gpu(args):
    """FFV1 on the GPU (Vulkan, see gpu.py): FFmpeg 9's ffv1_vulkan, fed through pipes.
    Same output and progress protocol as the other paths; uses ~0.2 CPU cores."""
    from . import gpu

    def emit(**kw):
        print(json.dumps(kw), flush=True)

    g = gpu.ffmpeg()
    if not g:
        emit(state="failed", error="GPU encoding isn't installed: run ./setup_gpu.sh")
        return 2
    exe, env = g
    out = args.output or output_path(args.input, "ffv1")
    part = out + ".part"
    base = [exe, "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk"]
    enc = ["-c:v", "ffv1_vulkan", "-coder", "rice", "-slicecrc", "1",   # rice: 3x faster on this GPU
           "-progress", "pipe:1", "-nostats", "-f", "matroska", part]
    reader, audio_r, audio_w, total = None, None, None, 0

    if args.input.lower().endswith(".arh"):
        try:
            reader = arh.ArhReader(args.input, wait=30 if args.follow else 0)
        except (OSError, ValueError) as e:
            emit(state="failed", error=str(e))
            return 2
        v, a = reader.video, (reader.audio if not args.no_audio else None)
        if v["format"] not in gpu.FFMPEG_PIXFMT or not _default_layout(v):
            emit(state="failed", error=f"GPU FFV1 can't take {v['format']} frames; use CPU FFV1")
            return 2
        cmd = base + ["-f", "rawvideo", "-pix_fmt", gpu.FFMPEG_PIXFMT[v["format"]],
                      "-s", f"{v['width']}x{v['height']}",
                      "-framerate", f"{v['fps_n']}/{v['fps_d']}", "-i", "pipe:0"]
        if a:
            audio_r, audio_w = os.pipe()
            cmd += ["-f", "s16le", "-ar", str(a["rate"]), "-ac", str(a["channels"]),
                    "-i", f"pipe:{audio_r}", "-map", "0:v", "-map", "1:a", "-c:a", "pcm_s16le"]
        cmd += ["-vf", f"format={gpu.PLANAR[v['format']]},hwupload"] + enc
        total = reader.estimate_frames()
    else:
        cmd = base + ["-i", args.input, "-map", "0:v:0"] + \
            ([] if args.no_audio else ["-map", "0:a?", "-c:a", "pcm_s16le"]) + \
            ["-vf", "format=yuv422p|yuv420p|yuv444p,hwupload"] + enc

    try:
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE if reader else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
                                pass_fds=(audio_r,) if audio_r else ())
    except OSError as e:
        emit(state="failed", error=f"can't start FFmpeg: {e}")
        return 2
    if audio_r:
        os.close(audio_r)
    stop = threading.Event()
    result = {"error": None}

    def feed_video():
        try:
            for _pts, _seq, data, _rep in arh.cfr_frames(reader, args.follow, stop=stop):   # CFR over drops
                proc.stdin.write(data)
        except (BrokenPipeError, OSError, ValueError) as e:
            if not stop.is_set():
                result["error"] = result["error"] or str(e)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def feed_audio():
        base_pts = None
        try:
            with os.fdopen(audio_w, "wb") as w:
                for _t, pts, _seq, data in reader.chunks((arh.AUDS,), args.follow, stop=stop):
                    if base_pts is None:
                        # align with the first video frame
                        first = next(reader.chunks((arh.VFRM,), False), None)
                        base_pts = first[1] if first else 0
                    if pts < base_pts:
                        continue
                    w.write(data)
        except (BrokenPipeError, OSError):
            pass

    def on_term(*_):
        stop.set()
        result["error"] = "cancelled"
        proc.terminate()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    threads = []
    if reader:
        threads.append(threading.Thread(target=feed_video, daemon=True))
        if audio_w is not None:
            threads.append(threading.Thread(target=feed_audio, daemon=True))
    for t in threads:
        t.start()
    t0, frames, last = time.monotonic(), 0, 0.0
    emit(state="running", frames=0, total=total, fps=0, output=out)
    for line in proc.stdout:                         # FFmpeg -progress key=value lines
        if line.startswith(b"frame="):
            frames = int(line[6:].strip() or 0)
        if time.monotonic() - last >= 0.5:
            last = time.monotonic()
            if reader and args.follow and not reader.finalized:
                try:
                    reader.reload_header()
                    total = reader.estimate_frames()
                except (OSError, ValueError):
                    pass
            dt = time.monotonic() - t0
            emit(state="running", frames=frames, total=max(total, frames),
                 fps=round(frames / dt, 2) if dt > 0 else 0,
                 live=bool(reader and args.follow and not reader.finalized))
    code = proc.wait()
    stop.set()
    err = proc.stderr.read().decode(errors="replace")
    err = "\n".join(l for l in err.splitlines() if "arm_release_ver" not in l).strip()
    if code == 0 and not result["error"]:
        os.replace(part, out)
        emit(state="done", frames=frames, output=out, seconds=round(time.monotonic() - t0, 1),
             damaged=_damage(reader) if reader else None)
        return 0
    try:
        os.remove(part)
    except OSError:
        pass
    if result["error"] == "cancelled":
        emit(state="cancelled", error="cancelled")
        return 3
    emit(state="failed", error=result["error"] or err[-400:] or f"FFmpeg exit code {code}")
    return 2


def transcode_media(args):
    """Convert an already encoded clip (H.265 .mp4/.mov/.mkv, FFV1 .mkv, ...) to another
    target: decode (hardware for H.265) -> encoder -> mux. Same progress protocol."""
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import GstPbutils

    def emit(**kw):
        print(json.dumps(kw), flush=True)

    try:
        info = GstPbutils.Discoverer.new(5 * Gst.SECOND).discover_uri(
            Gst.filename_to_uri(os.path.abspath(args.input)))
    except GLib.Error as e:
        emit(state="failed", error=f"can't read {args.input}: {e.message}")
        return 2
    vs = info.get_video_streams()
    if not vs:
        emit(state="failed", error="no video stream")
        return 2
    v = vs[0]
    fps = v.get_framerate_num() / v.get_framerate_denom() if v.get_framerate_denom() else 30.0
    total = int(info.get_duration() / Gst.SECOND * fps) if info.get_duration() else 0
    audio = bool(info.get_audio_streams()) and not args.no_audio
    out = args.output or output_path(args.input, args.codec, args.scale)
    part = out + ".part"
    mux = _mux(args.codec)
    # the source format is only known after decoding: always convert (this also keeps
    # the mpph265enc fallback away from plain system memory, see gstutil)
    need, enc = _encoder_desc(args.codec, "decoded", fps, args.quality, args.preset, args.chroma,
                               args.bitrate, args.rc)
    desc = (f"filesrc name=fsrc ! decodebin name=dec "
            f"dec. ! queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! "
            f"{_scale_desc(args.scale)}videoconvert n-threads=4 ! video/x-raw,format={need} ! {enc} ! "
            f"identity name=counter ! {mux} name=mux ! filesink name=fsink")
    if audio:
        desc += (" dec. ! queue max-size-time=0 max-size-bytes=0 max-size-buffers=0 ! " +
                 (_audio_tail(args.codec) or "audioconvert ! audioresample ! audio/x-raw,format=S16LE ! mux."))
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error as e:
        emit(state="failed", error=f"pipeline: {e.message}")
        return 2
    pipe.get_by_name("fsrc").set_property("location", args.input)
    pipe.get_by_name("fsink").set_property("location", part)

    loop = GLib.MainLoop()
    result = {"code": 1, "error": None}
    encoded = [0]
    t0 = time.monotonic()

    def count(_pad, _info):
        encoded[0] += 1
        return Gst.PadProbeReturn.OK
    pipe.get_by_name("counter").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, count)

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            result["code"] = 0
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            result["error"], result["code"] = f"{err.message} ({dbg})", 2
            loop.quit()

    def report():
        dt = time.monotonic() - t0
        emit(state="running", frames=encoded[0], total=max(total, encoded[0]),
             fps=round(encoded[0] / dt, 2) if dt > 0 else 0, live=False)
        return True

    def on_term():
        result["error"], result["code"] = "cancelled", 3
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, on_term)
    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_message)
    emit(state="running", frames=0, total=total, fps=0, output=out)
    vpu = _vpu_bridge(pipe, args, fps)
    pipe.set_state(Gst.State.PLAYING)
    if vpu:
        vpu.start()
    GLib.timeout_add(500, report)
    loop.run()
    if vpu:
        vpu.stop()
    pipe.set_state(Gst.State.NULL)
    if result["code"] == 0:
        os.replace(part, out)
        emit(state="done", frames=encoded[0], output=out, seconds=round(time.monotonic() - t0, 1))
    else:
        try:
            os.remove(part)
        except OSError:
            pass
        emit(state="cancelled" if result["code"] == 3 else "failed",
             error=result["error"] or "unknown error")
    return result["code"]


def _audio_tail(codec):
    """Audio into the muxer: the MP4 share copy needs AAC (mp4mux takes no PCM);
    MOV/MKV copies keep the audio lossless."""
    if codec == "h264-vpu":
        enc = "voaacenc bitrate=192000" if Gst.ElementFactory.find("voaacenc") else "avenc_aac bitrate=192000"
        return f"audioconvert ! audioresample ! {enc} ! aacparse ! mux."
    return None


def _mux(codec):
    if codec == "ffv1":
        return "matroskamux"
    if codec == "h264-vpu":
        return "mp4mux faststart=true"
    return "qtmux"


def add_transcode_args(sp):
    sp.add_argument("input", help=".arh recording, or an encoded clip (.mp4/.mov/.mkv)")
    sp.add_argument("--codec", choices=list(CODECS), required=True)
    sp.add_argument("--quality", choices=list(X265_CRF), default="high",
                    help="H.265: high / higher / max (x265 CRF 18/14/10, VPU QP 22/18/14)")
    sp.add_argument("--preset", choices=X265_PRESETS, default="fast", help="x265 speed preset")
    sp.add_argument("--chroma", choices=["420", "source"], default="420",
                    help="x265: 4:2:0 (plays everywhere) or keep the source 4:2:2/4:4:4")
    sp.add_argument("--bitrate", type=float,
                    help="H.265: target bitrate in Mbit/s instead of constant quality")
    sp.add_argument("--rc", choices=["vbr", "cbr"], default="vbr",
                    help="VPU with --bitrate: variable (default) or constant bitrate")
    sp.add_argument("--scale", choices=list(SCALES), default="source",
                    help="H.264: keep the size or scale down to 1080p / 720p")
    sp.add_argument("--follow", action="store_true",
                    help="the file is still being recorded: keep reading until it ends")
    sp.add_argument("--no-audio", action="store_true")
    sp.add_argument("-o", "--output", help="output file (default: next to the .arh)")
    sp.add_argument("--nice", type=int, default=10, help="CPU priority niceness (default 10)")
    sp.add_argument("--cpus", type=int, default=0,
                    help="run on the first N CPUs only (0 = all): less peak current on a weak supply; "
                         "on big.LITTLE boards the little cores come first")


# ---------------------------------------------------------------- job manager (UI side)

VERIFIED_SUFFIX = ".verified.json"      # next to an FFV1 file proven identical to its RAW (GAL-08)
MAX_ATTEMPTS = 3                        # a failed or interrupted job runs again, up to this (GAL-10)
RETRY_DELAY = 10                        # s, times the attempt number
GENTLE_CPUS = 6                         # FFV1 on CPU after a failure: fewer cores, less peak current
EXIT_LOCKED = 4                         # the worker's code for "another job makes this file"


def verify_wanted(codec, src, opts):
    return codec in ("ffv1", "ffv1-gpu", "verify") and src.lower().endswith(".arh") and bool(opts.get("verify"))


def _mb(n):
    return "%.1f MB" % (n / 1e6)


def clean_stale_parts(folder, busy=()):
    """Remove *.part outputs nobody is writing (left by a power loss), and their empty logs."""
    removed = []
    try:
        names = os.listdir(folder)
    except OSError:
        return removed
    for name in names:
        if not name.endswith(".part"):
            continue
        out = os.path.join(folder, name[:-5])
        if out in busy:
            continue
        with arh.file_lock(out) as mine:
            if not mine:
                continue                       # a worker (this slot or the other) writes it
            for p in (out + ".part", out + ".log"):
                try:
                    if p.endswith(".part") or os.path.getsize(p) == 0:
                        os.remove(p)
                        removed.append(os.path.basename(p))
                except OSError:
                    pass
    return removed


class Job:
    _next_id = 1

    def __init__(self, src, codec, opts, follow, output=None):
        self.id = Job._next_id
        Job._next_id += 1
        self.src, self.codec, self.opts, self.follow = src, codec, opts, follow
        self.output = output or output_path(src, codec, opts.get("scale"))
        self.created = time.time()
        # queued | running | verifying | retrying | done | failed | cancelled
        self.state = "queued"
        self.verify = None            # {"ok", "frames", "audio_bytes", "detail", "raw_deleted"?}
        self.frames = self.total = 0
        self.fps = 0.0
        self.live = follow
        self.error = None
        self.proc = None
        self.started = None
        self.log_path = self.output + (".verify.log" if codec == "verify" else ".log")
        self.attempt = 1
        self.note = None              # what happened on the way: repaired RAW, damage skipped, retries
        self.verify_only = False      # resumed after its copy was made: only the check is left

    @property
    def title(self):
        return "Lossless check" if self.codec == "verify" else CODECS[self.codec][0]

    @property
    def finished(self):
        return self.state in ("done", "failed", "cancelled")

    def add_note(self, text):
        self.note = text if not self.note else self.note + "; " + text

    def eta(self):
        if self.state not in ("running", "verifying") or self.fps <= 0 or self.total <= self.frames:
            return None
        return (self.total - self.frames) / self.fps

    def describe(self):
        return {"id": self.id, "title": self.title, "codec": self.codec, "state": self.state,
                "source": os.path.basename(self.src), "output": os.path.basename(self.output),
                "frames": self.frames, "total": self.total, "fps": self.fps, "live": self.live,
                "progress": round(self.frames / self.total, 4) if self.total else 0.0,
                "eta": round(self.eta()) if self.eta() is not None else None,
                "error": self.error, "options": self.opts, "created": int(self.created),
                "verify": self.verify, "attempt": self.attempt, "max_attempts": MAX_ATTEMPTS,
                "note": self.note}

    def persisted(self):
        return {"src": self.src, "codec": self.codec, "opts": self.opts, "output": self.output,
                "attempt": self.attempt, "note": self.note, "created": self.created,
                "verify_only": self.verify_only or self.state == "verifying"}


class JobManager:
    """Runs transcode workers; at most `parallel` at a time (follow jobs always start).

    GAL-10: unfinished jobs are kept in `store` (JSON, rewritten on every state change), so a
    restart or a power loss does not lose them - start() resumes them. A job that fails (or was
    cut off) runs again up to MAX_ATTEMPTS times; FFV1 on the CPU then uses fewer threads."""

    def __init__(self, on_update, parallel=2, store=None):
        self._notify = on_update
        self.parallel = parallel
        self.jobs = []
        self.store = store
        self._saved = None
        self._closing = False

    def on_update(self, job):
        self._save()
        self._notify(job)

    def _save(self):
        if not self.store or self._closing:
            return
        keep = [j for j in self.jobs if not j.finished]
        fp = [(j.id, j.state, j.attempt) for j in keep]
        if fp == self._saved:
            return
        tmp = self.store + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump([j.persisted() for j in keep], f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.store)
            self._saved = fp
        except OSError:
            pass

    def resume(self):
        """Re-queue the jobs a stop / crash / power loss cut off. Returns how many."""
        try:
            with open(self.store) as f:
                saved = json.load(f)
        except (OSError, ValueError, TypeError):
            return 0
        n = 0
        for d in saved if isinstance(saved, list) else []:
            try:
                if not os.path.exists(d["src"]) or any(j.output == d["output"] and not j.finished for j in self.jobs):
                    continue
                job = Job(d["src"], d["codec"], d.get("opts") or {}, False, output=d["output"])
            except (KeyError, TypeError):
                continue
            job.created = d.get("created", job.created)
            job.attempt = int(d.get("attempt", 1)) + 1     # the cut-off run counts
            job.note = d.get("note")
            job.verify_only = bool(d.get("verify_only")) and os.path.exists(job.output)
            if job.attempt > MAX_ATTEMPTS:
                job.state, job.error = "failed", "interrupted %d times (power?) - start it again by hand" % MAX_ATTEMPTS
            else:
                job.add_note("resumed after a restart")
                n += 1
            self.jobs.append(job)
        self._schedule()
        self.on_update(None)
        return n
    def get(self, job_id):
        return next((j for j in self.jobs if j.id == int(job_id)), None)

    def busy_paths(self):
        """Files an unfinished job reads or writes (they must not be deleted, GAL-06)."""
        out = set()
        for j in self.jobs:
            if not j.finished:
                out.update((j.src, j.output))
        return out

    def add(self, src, codec, opts, follow=False, output=None):
        if codec not in CODECS and codec != "verify":
            raise ValueError("unknown target %s" % codec)
        out = output or output_path(src, codec, opts.get("scale"))
        if any(j.output == out and not j.finished for j in self.jobs):
            raise RuntimeError("%s is already being %s" % (os.path.basename(out),
                                                          "checked" if codec == "verify" else "made"))
        job = Job(src, codec, opts, follow, output=out)
        self.jobs.append(job)
        self._schedule()
        self.on_update(job)
        return job

    def running(self):
        return [j for j in self.jobs if j.state in ("running", "verifying")]

    def active_count(self):
        return sum(1 for j in self.jobs if not j.finished)

    def cancel(self, job):
        if job.state in ("queued", "retrying"):
            job.state = "cancelled"
            self.on_update(job)
        elif job.state in ("running", "verifying") and job.proc:
            job.cancelled = True
            job.proc.send_signal(signal.SIGTERM)

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if not j.finished]
        self.on_update(None)

    def shutdown(self):
        """Stop all workers (on quit); unfinished outputs are removed. The store keeps the
        unfinished jobs, so the next start resumes them."""
        self._closing = True
        for j in self.jobs:
            if j.state in ("running", "verifying") and j.proc:
                j.proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 3
        for j in self.jobs:
            if j.proc and j.proc.poll() is None:
                try:
                    j.proc.wait(max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    j.proc.kill()
                    j.proc.wait()
            if j.proc and j.state in ("running", "verifying"):
                j.state = "cancelled"
                self._cleanup(j)

    @staticmethod
    def _cleanup(job):
        """Remove leftovers: the partial output, and the log unless it explains a failure."""
        paths = [] if job.codec == "verify" else [job.output + ".part"]
        if job.state != "failed":
            paths.append(job.log_path)
        else:
            try:
                if os.path.getsize(job.log_path) == 0:
                    paths.append(job.log_path)
            except OSError:
                pass
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass

    def _schedule(self):
        busy = len(self.running())
        for job in self.jobs:
            if job.state != "queued":
                continue
            if busy >= self.parallel and not job.follow:
                break
            self._spawn(job)
            busy += 1

    def _spawn(self, job):
        if job.codec == "verify" or job.verify_only:
            return self._spawn_verify(job)
        o = job.opts
        cmd = [sys.executable, "-m", "arstro_remote.recorder.transcode", job.src, "--codec", job.codec,
               "--quality", o.get("quality", "high"), "--preset", o.get("preset", "fast"),
               "--chroma", o.get("chroma", "420"), "--scale", o.get("scale") or "source",
               "-o", job.output]
        if job.attempt > 1 and job.codec == "ffv1":
            cmd += ["--cpus", str(GENTLE_CPUS)]
        if o.get("bitrate"):
            cmd += ["--bitrate", str(o["bitrate"]), "--rc", o.get("rc", "vbr")]
        if job.follow:
            cmd.append("--follow")
        try:
            log = open(job.log_path, "w")
            pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            env = dict(os.environ, GST_MPP_NO_RGA="0")
            env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, env=env)
        except OSError as e:
            job.state, job.error = "failed", str(e)
            self.on_update(job)
            return
        log.close()
        job.state, job.started, job.error = "running", time.monotonic(), None
        self.on_update(job)
        GLib.io_add_watch(job.proc.stdout, GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_output, job)

    def _env(self):
        pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = dict(os.environ, GST_MPP_NO_RGA="0")
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    def _spawn_verify(self, job):
        """Phase 2 of an FFV1 job (or a standalone check): decode and compare with the RAW."""
        cmd = [sys.executable, "-m", "arstro_remote.recorder.verify", job.src, job.output]
        try:
            log = open(job.output + ".verify.log", "w")
            # low priority: never disturb a live recording or the preview
            job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, env=self._env(),
                                        preexec_fn=lambda: os.nice(10))
        except OSError as e:
            job.state, job.error = "failed", str(e)
            self.on_update(job)
            return
        log.close()
        job.state, job.frames, job.fps = "verifying", 0, 0.0
        job.started = job.started or time.monotonic()
        job.verify_started = time.monotonic()
        GLib.io_add_watch(job.proc.stdout, GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_output, job)
        self.on_update(job)

    def _on_output(self, stream, cond, job):
        line = stream.readline() if cond & GLib.IOCondition.IN else ""
        if line:
            try:
                msg = json.loads(line)
            except ValueError:
                return True
            job.frames = msg.get("frames", job.frames)
            job.total = msg.get("total", job.total)
            job.fps = msg.get("fps", job.fps)
            job.live = msg.get("live", job.live)
            st = msg.get("state")
            if msg.get("repaired"):
                r = msg["repaired"]
                job.add_note("RAW was cut off - repaired: %d frames kept, %s of damaged tail removed"
                             % (r.get("frames", 0), _mb(r.get("dropped_bytes", 0))))
            if msg.get("damaged"):
                job.add_note("skipped %s of damaged data" % _mb(msg["damaged"].get("bytes", 0)))
            if job.state == "verifying":
                if "verify" in msg:
                    job.verify = msg["verify"]
                elif st == "verifying" and job.frames:
                    job.fps = round(job.frames / max(0.1, time.monotonic() - job.verify_started), 1)
            elif st in ("done", "failed", "cancelled"):
                # the final state is set when the worker exits: a failure may still be retried
                job.result, job.error = st, msg.get("error")
            self.on_update(job)
            return True
        # worker exited
        code = job.proc.wait()
        if job.state == "verifying":
            if getattr(job, "cancelled", False):
                job.state, job.verify = "cancelled", None
            else:
                job.verify = job.verify or {"ok": False, "detail": f"the check stopped (code {code}, see {job.output}.verify.log)"}
                job.state = "done" if job.codec != "verify" or code == 0 else "failed"
                if job.verify.get("ok"):
                    self._mark_verified(job)
                elif job.codec != "verify" and self._retry(job, "the copy failed the check: " + str(job.verify.get("detail"))):
                    return False
            self._finish(job)
            return False
        if not job.finished:
            job.state = getattr(job, "result", None) or ("done" if code == 0 else "failed")
            job.result = None
            if job.state != "done":
                job.error = job.error or f"worker exited with code {code} (see {job.log_path})"
        if job.state == "failed" and code != EXIT_LOCKED and not getattr(job, "cancelled", False):
            if self._retry(job, job.error):
                return False
        if job.state == "done" and verify_wanted(job.codec, job.src, job.opts):
            self._cleanup_part(job)
            self._spawn_verify(job)                  # phase 2: prove it before the RAW may go
            return False
        self._finish(job)
        return False

    def _finish(self, job):
        self._cleanup(job)
        self.on_update(job)
        self._schedule()

    def _retry(self, job, reason):
        """Run a failed job again later (GAL-10). False when it has had all its attempts."""
        if job.attempt >= MAX_ATTEMPTS:
            job.add_note("gave up after %d attempts" % job.attempt)
            return False
        self._cleanup_part(job)
        delay = RETRY_DELAY * job.attempt
        job.add_note("attempt %d failed (%s) - retrying" % (job.attempt, (reason or "?")[:120]))
        job.attempt += 1
        job.state, job.verify, job.verify_only, job.proc = "retrying", None, False, None
        job.frames = job.fps = 0
        self.on_update(job)

        def again():
            if job.state == "retrying":
                job.state = "queued"
                self._schedule()
                self.on_update(job)
            return False
        GLib.timeout_add_seconds(delay, again)
        return True

    @staticmethod
    def _cleanup_part(job):
        try:
            os.remove(job.output + ".part")
        except OSError:
            pass

    @staticmethod
    def _mark_verified(job):
        v = dict(job.verify, raw=os.path.basename(job.src),
                 checked=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            with open(job.output + VERIFIED_SUFFIX, "w") as f:
                json.dump(v, f)
        except OSError:
            pass
