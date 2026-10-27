"""Live HDMI RX pipeline with hot-pluggable branches (runs in the recorder worker).

    v4l2src ─ tee vt ─┬─ queue(leaky) ─ fakesink            (keeps the pipeline running)
                      ├─ [stream branch]  H.264 preview     (while someone watches; LUT, REC-13)
                      ├─ [analysis branch] exposure data    (while a viewer shows scopes, REC-13)
                      ├─ [zoom branches]  a loupe's crop    (per viewer, REC-14)
                      └─ [recording branch]                 (while recording)
    alsasrc ─ tee at ─┬─ level ─ fakesink                   (the audio meters, REC-10)
                      └─ [recording branch]

Branches are bins linked to the tees on start and unlinked + drained (EOS) on stop,
so capture never restarts when a viewer joins or a recording starts/stops.

  StreamBranch      H.264 preview on the VPU (mpph264enc; x264 without one), access units to a callback
  AnalysisBranch    the luma of a small nearest-neighbour copy -> monitor.analyze
  ZoomBranch        a square crop of the full frame for one viewer's loupe, H.264
  EncodedRecording  real-time H.265 on the VPU (mpph265enc) -> MOV/MKV (PCM) or MP4 (AAC)
  RawRecording      frames copied out of the capture buffers, framed as ARH chunks
"""
import os
import threading
import time
from datetime import datetime

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from . import arh, monitor, timecode  # noqa: E402
from .gstutil import gst_buffer, is_dmabuf  # noqa: E402
from .v4l2 import (alsa_card_present, audio_present, find_pulse_hdmiin, find_pulse_source,  # noqa: E402
                   lock_timings, query_signal)

POLL_MS = 1000
STABLE_POLLS = 2                    # identical timing readings required before (re)starting
IDLE_POLL_EVERY = 3                 # no signal and nothing running: ask the driver every 3rd poll
                                    # (rk_hdmirx logs "port has no link" for every query)
AUDIO_CAPS = "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"
AUDIO_META = {"format": "S16LE", "rate": 48000, "channels": 2}
SOFTWARE = "arstro-remote recorder 2.1"
LEVEL_INTERVAL_NS = 100_000_000     # the audio meters: 10 readings a second
ANALYSIS_EVERY = 0.25               # exposure data: 4 a second
MAX_ZOOMS = 3                       # viewers zoomed at once (one crop encoder each)
ZOOM_MAX = 1080                     # a loupe's crop larger than this (source pixels) is scaled down to it


def _db(x):
    """A level for the meters: digital silence is -inf dB, which is no JSON number - floor it."""
    return round(x, 1) if x == x and x > -100.0 else -100.0


def cma_state():
    """Contiguous-memory figures: capture buffers and the encoders allocate from CMA, and
    a 4K encoder that can't get its buffers fails with "Output state was not configured"."""
    try:
        with open("/proc/meminfo") as f:
            m = dict(line.split(":", 1) for line in f if line.startswith(("Cma", "MemAvailable")))
        return ", ".join(f"{k.strip()} {int(v.split()[0]) // 1024} MB" for k, v in m.items())
    except (OSError, ValueError):
        return "memory info unavailable"


def _fraction(fps):
    for d in (1, 1001, 1000):
        n = fps * d
        if abs(n - round(n)) < 0.01:
            return round(n), d
    return round(fps * 1000), 1000



PREVIEW_QUALITIES = {            # max (width, height), fps, H.264 bitrate
    "low": ((640, 360), 30, 1_000_000),
    "medium": ((1280, 720), 30, 3_000_000),
    "high": ((1920, 1080), 30, 6_000_000),
}


class Capture:
    """Owns the live pipeline and follows the HDMI signal (hot-plug, resolution change).

    The pipeline only runs while `wanted` is true (someone watches or records, REC-06);
    the signal itself is polled all the time (cheap)."""

    def __init__(self, dev, listener, audio_card=None, simulate=None):
        self.dev = dev
        self.listener = listener          # on_signal / on_capture_error / on_recording_stopped
        self.audio_card = audio_card
        self.sim = simulate
        self.pipeline = None
        self.timing = None                # (w, h, fps) being streamed
        self.signal = None                # (w, h, fps) of the input, even when not capturing
        self.format = None                # driver pixel format, e.g. NV12
        self.candidate = None
        self.candidate_count = 0
        self.audio_failed = False
        self.audio_in_pipeline = False
        self._audio_retried = False
        self._audio_check = 0.0
        self.recording = None
        self.stream = None
        self.wanted = False
        self.last_video_pts = None
        self.last_audio_time = 0.0
        self.seq_last = None
        self.drops = 0                    # capture frame drops (sequence gaps)
        self.fourcc = None                # V4L2 pixel format of the driver
        self.fps_measured = 0.0           # frames actually arriving per second
        self._fps_count = 0
        self._fps_t0 = 0.0
        self._first_buffer = False
        self.first_buffer_event = threading.Event()
        self.retry_at = 0.0               # after an error: don't restart before this time
        self.retry_delay = 2.0
        self.paused = False
        self.last_error = None
        self.audio_source = "hdmi"        # or "alsa:<card id>" (REC-09)
        self.analysis = None              # AnalysisBranch while a viewer shows scopes
        self.zooms = {}                   # viewer id -> ZoomBranch
        self.lut = None                   # monitor.LutTable for the preview, or None

    # -- signal

    @property
    def has_audio(self):
        return bool(self.pipeline and self.audio_in_pipeline and not self.audio_failed)

    def _want_audio(self):
        if self.audio_failed:
            return False
        if self.audio_source.startswith("alsa:"):
            return alsa_card_present(self.audio_source[5:])  # an audio interface, any video source
        if self.sim:
            return not getattr(self.sim, "device", None)      # a V4L2 camera has no audio here
        return bool(self.audio_card) and audio_present(self.dev)

    def audio_alive(self):
        return self.has_audio and time.monotonic() - self.last_audio_time < 1.0

    def signal_text(self, timing=None):
        timing = timing or self.timing or self.signal
        if not timing:
            return None
        w, h, fps = timing
        text = f"{w}×{h}p{fps:.2f}".replace(".00", "")
        return text + (f" · {self.format}" if self.format else "")

    def query(self):
        return self.sim.query() if self.sim else query_signal(self.dev)

    def set_wanted(self, wanted):
        """Start or stop capturing (the signal poll does the actual start)."""
        self.wanted = bool(wanted)
        if not self.wanted and self.pipeline and not self.recording and not self.stream:
            self.stop("not needed")
        self.poll()

    def poll(self):
        if self.paused:
            return True
        if self.signal is None and not self.pipeline and not self.sim:
            self._idle_polls = getattr(self, "_idle_polls", 0) + 1
            if self._idle_polls % IDLE_POLL_EVERY:
                return True
        timing = self.query()
        if timing != self.signal:
            self.signal = timing
            self.listener.on_signal(self.signal_text(timing) if timing else None)
        if timing == self.timing and self.pipeline:
            now = time.monotonic()
            if (not self.audio_in_pipeline and not self.recording and self.audio_card
                    and not self.sim and not self._audio_retried and now - self._audio_check > 3):
                self._audio_check = now
                if audio_present(self.dev):
                    self._audio_retried = True    # once per signal, never a restart loop
                    print("[info] HDMI audio appeared - restarting the pipeline with audio")
                    self.audio_failed = False
                    self.restart()
            return True
        if timing is None:
            if self.pipeline:
                print("[info] signal lost")
                self.stop("signal lost")
            self.candidate, self.candidate_count = None, 0
            return True
        if not self.wanted:
            if self.pipeline and not self.recording:
                self.stop("not needed")
            return True
        if timing and not self.pipeline and timing == self.candidate \
                and time.monotonic() < self.retry_at:
            return True                   # waiting before retrying after an error
        # new or changed timing: wait until it is stable before (re)starting
        if timing != self.candidate:
            self.candidate, self.candidate_count = timing, 1
            self.retry_delay = 2.0
            self.audio_failed = False
            self._audio_retried = False
            if not self.sim:
                return True
        else:
            self.candidate_count += 1
        if self.candidate_count >= (1 if self.sim else STABLE_POLLS):
            self.stop("signal changed")
            self.start(timing)
        return True

    # -- pipeline

    def _desc(self, timing):
        w, h, fps = timing
        n, d = _fraction(fps)
        if self.sim and getattr(self.sim, "device", None):
            src = self.sim.source_desc(n, d)
        elif self.sim:
            src = (f"videotestsrc name=src is-live=true pattern={self.sim.pattern} horizontal-speed=4 ! "
                   f"video/x-raw,format={self.format},width={w},height={h},framerate={n}/{d} ! ")
        else:
            # pin the format: the driver only accepts the one matching the input signal
            caps = f"video/x-raw,format={self.format},width={w},height={h}" \
                if self.format and self.format != "?" else "video/x-raw"
            # rk_hdmirx reports a bogus frame rate (120/1 for a 60 Hz signal); the DV
            # timings are right, so overwrite it or files get the wrong speed
            src = (f"v4l2src name=src device={self.dev} io-mode=dmabuf do-timestamp=true ! "
                   f"{caps} ! capssetter caps=\"video/x-raw,framerate={n}/{d}\" ! ")
        desc = (f"{src}tee name=vt allow-not-linked=true "
                "vt. ! queue max-size-buffers=1 leaky=downstream ! fakesink name=idle sync=false async=false")
        self.audio_in_pipeline = self._want_audio()
        if self.audio_in_pipeline:
            card = self.audio_source[5:] if self.audio_source.startswith("alsa:") else None
            if card:
                # a USB audio interface (REC-09); through PulseAudio when it holds the card
                pulse = find_pulse_source(card)
                asrc = (f"pulsesrc name=asrc device={pulse} buffer-time=100000 ! audioconvert ! audioresample"
                        if pulse else
                        f"alsasrc name=asrc device=plughw:CARD={card},DEV=0 buffer-time=100000 "
                        "! audioconvert ! audioresample")
            elif self.sim:
                asrc = "audiotestsrc name=asrc is-live=true wave=ticks volume=0.4"
            elif find_pulse_hdmiin():
                asrc = (f"pulsesrc name=asrc device={find_pulse_hdmiin()} buffer-time=100000 "
                        "! audioconvert ! audioresample")
            else:
                asrc = (f"alsasrc name=asrc device=plughw:CARD={self.audio_card},DEV=0 "
                        "buffer-time=100000")
            desc += (f" {asrc} ! {AUDIO_CAPS} ! tee name=at allow-not-linked=true "
                     "at. ! queue max-size-buffers=4 leaky=downstream ! "
                     f"level name=meter interval={LEVEL_INTERVAL_NS} post-messages=true ! "
                     "fakesink sync=false async=false")
        return desc

    def start(self, timing):
        if self.sim:
            self.format, self.fourcc = self.sim.format, self.sim.format
        else:
            self.format, self.fourcc = lock_timings(self.dev)
        self.timing = timing
        self._first_buffer = False
        self.first_buffer_event.clear()
        self._fps_count, self._fps_t0, self.fps_measured = 0, time.monotonic(), 0.0
        desc = self._desc(timing)
        print(f"[info] signal: {self.signal_text()} (driver fourcc {self.fourcc})")
        print(f"[info] pipeline: {desc}")
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            self._failed(f"could not build the pipeline: {e.message}")
            self.pipeline = self.timing = None
            return
        vt = self.pipeline.get_by_name("vt")
        vt.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_video_buffer)
        at = self.pipeline.get_by_name("at")
        if at:
            at.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_audio_buffer)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            reason, audio_fault = "failed to start the pipeline", False
            while True:
                msg = bus.pop_filtered(Gst.MessageType.ERROR)
                if not msg:
                    break
                err, dbg = msg.parse_error()
                print(f"[error] start: {msg.src.get_name()}: {err.message}\n        {dbg}")
                if self._is_audio_element(msg.src):
                    audio_fault = True
                else:
                    reason = f"{msg.src.get_name()}: {err.message}"
            self.stop()
            if audio_fault and reason == "failed to start the pipeline":
                print("[warn] HDMI audio could not be opened - continuing without audio")
                self.audio_failed = True
                self.start(timing)
                return
            self._failed(reason)
            return
        self.listener.on_capture(True)

    def _failed(self, message):
        """Remember the error, schedule a retry with backoff, tell the service."""
        self.last_error = message
        self.retry_at = time.monotonic() + self.retry_delay
        print(f"[error] {message} - retrying in {self.retry_delay:.0f} s")
        self.retry_delay = min(self.retry_delay * 2, 30.0)
        self.listener.on_capture_error(message)

    def stop(self, reason=None):
        if self.recording:
            rec = self.recording
            rec.stop(sync=True)
            self.listener.on_recording_stopped(rec, reason)
        if self.stream:
            self.stream.detach()
            self.stream = None
        if self.analysis:
            self.analysis.detach()
            self.analysis = None
        for z in list(self.zooms.values()):
            z.detach()
        self.zooms.clear()
        was_running = self.pipeline is not None
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_bus().remove_signal_watch()
        self.pipeline = None
        self.timing = None
        self.last_video_pts = self.seq_last = None
        if was_running:
            self.listener.on_capture(False)

    def restart(self):
        """Rebuild the pipeline (e.g. when HDMI audio appears, or another audio input is
        chosen). The worker re-attaches analysis and zoom branches when capture runs again."""
        if self.timing and not self.recording:
            t = self.timing
            want_stream = self.stream.quality if self.stream else None
            self.stop()
            self.start(t)
            if want_stream and self.pipeline:
                self.start_stream(want_stream, self._stream_cb)

    def _on_video_buffer(self, pad, info):
        buf = info.get_buffer()
        self.last_video_pts = buf.pts
        if not self._first_buffer:
            self._first_buffer = True
            caps = pad.get_current_caps()
            mem = "dmabuf" if is_dmabuf(buf) else "system memory"
            print(f"[info] first frame: {buf.get_size()} bytes in {mem}, "
                  f"caps {caps.to_string() if caps else '?'}")
            self.retry_delay = 2.0
            self.last_error = None
            self.dmabuf = mem == "dmabuf"
            self.first_buffer_event.set()
        self._fps_count += 1
        now = time.monotonic()
        if now - self._fps_t0 >= 2.0:
            self.fps_measured = self._fps_count / (now - self._fps_t0)
            self._fps_count, self._fps_t0 = 0, now
        seq = buf.offset
        if seq != Gst.BUFFER_OFFSET_NONE:
            if self.seq_last is not None and seq > self.seq_last + 1:
                self.drops += seq - self.seq_last - 1
            self.seq_last = seq
        return Gst.PadProbeReturn.OK

    def _on_audio_buffer(self, _pad, _info):
        self.last_audio_time = time.monotonic()
        return Gst.PadProbeReturn.OK

    def _attached(self, obj):
        """True if obj is still part of the running pipeline."""
        while obj is not None:
            if obj == self.pipeline:
                return True
            obj = obj.get_parent()
        return False

    def _is_audio_element(self, obj):
        at = self.pipeline and self.pipeline.get_by_name("asrc")
        while obj is not None:
            if obj == at or (isinstance(obj, Gst.Element) and obj.get_name() == "asrc"):
                return True
            obj = obj.get_parent()
        return False

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.ELEMENT:
            st = msg.get_structure()
            if st is not None and st.get_name() == "level":
                try:
                    self.listener.on_levels([_db(x) for x in st.get_value("peak")],
                                            [_db(x) for x in st.get_value("rms")])
                except (TypeError, AttributeError):
                    pass
            return
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if not self._attached(msg.src):
                # a late error from a branch that was already removed (e.g. a failed
                # recording) - it must not tear down the live capture
                print(f"[warn] ignored error from a detached element: {err.message}")
                return
            if self._is_audio_element(msg.src):
                print(f"[warn] audio disabled: {err.message}")
                self.audio_failed = True
                return
            if self.analysis and self.analysis.owns(msg.src):
                print(f"[warn] exposure analysis: {err.message}")
                self.analysis.detach()
                self.analysis = None
                return
            for zid, z in list(self.zooms.items()):
                if z.owns(msg.src):
                    print(f"[warn] zoom for viewer {zid}: {err.message}")
                    self.stop_zoom(zid)
                    self.listener.on_zoom(zid, error=err.message)
                    return
            if self.stream and self.stream.owns(msg.src):
                print(f"[warn] preview stream: {err.message} ({dbg})")
                st = self.stream
                self.stream = None
                st.detach()
                self.listener.on_stream_error(st, err.message)
                return
            if self.recording and self.recording.owns(msg.src):
                print(f"[error] recording: {err.message}\n        {dbg}\n        {cma_state()}")
                rec = self.recording
                rec.stop(sync=True)
                self.listener.on_recording_stopped(rec, f"recording error: {err.message}")
                return
            print(f"[error] video: {err.message}\n        {dbg}")
            self.stop(f"video error: {err.message}")
            detail = dbg.strip().splitlines()[-1] if dbg else ""
            self._failed(f"{err.message}" + (f" ({detail})" if detail else ""))
        elif msg.type == Gst.MessageType.EOS:
            self.stop("end of stream")

    # -- preview stream

    def start_stream(self, quality, on_au, zero_copy=True):
        if not self.pipeline or not self.timing:
            raise RuntimeError("not capturing")
        if self.stream:
            self.stream.detach()
            self.stream = None
        self._stream_cb = on_au
        st = StreamBranch(self, quality, on_au, zero_copy=zero_copy, lut=self.lut)
        st.attach()
        self.stream = st
        return st

    def stop_stream(self):
        if self.stream:
            self.stream.detach()
            self.stream = None
        if not self.wanted and not self.recording and self.pipeline:
            self.stop("not needed")

    # -- exposure analysis (REC-13)

    def set_analysis(self, on, on_data=None):
        if on and self.pipeline and self.timing and not self.analysis and monitor.available():
            if (self.format or "") not in ANALYSIS_FORMATS:
                print(f"[warn] no exposure analysis for {self.format} pictures")
                return
            a = AnalysisBranch(self, on_data)
            try:
                a.attach()
                self.analysis = a
            except (RuntimeError, GLib.Error) as e:
                print(f"[warn] exposure analysis could not start: {e}")
        elif not on and self.analysis:
            self.analysis.detach()
            self.analysis = None

    # -- focus zoom (REC-14)

    def start_zoom(self, zid, x, y, ratio, size, on_au):
        """A loupe around (x, y) (0..1 of the picture), `size` its diameter as a share of the
        picture's height, magnifying `ratio` times. Moving keeps the branch; a new size or
        ratio builds a new one (a new stream for the viewer)."""
        if not self.pipeline or not self.timing:
            raise RuntimeError("not capturing")
        z = self.zooms.get(zid)
        if z and z.ratio == ratio and z.side == ZoomBranch.side_for(self.timing, ratio, size):
            z.move(x, y)
            return z
        if z:
            self.stop_zoom(zid)
        if len(self.zooms) >= MAX_ZOOMS:
            raise RuntimeError(f"{MAX_ZOOMS} viewers are zoomed in already")
        z = ZoomBranch(self, zid, x, y, ratio, size, on_au)
        z.attach()
        self.zooms[zid] = z
        return z

    def stop_zoom(self, zid):
        z = self.zooms.pop(zid, None)
        if z:
            z.detach()

    # -- recording

    def start_recording(self, mode, path_base, settings, with_audio):
        """Attach a recording branch. Returns the Recording or raises RuntimeError."""
        if not self.pipeline or not self.timing:
            raise RuntimeError("no HDMI signal")
        if self.recording:
            raise RuntimeError("already recording")
        with_audio = with_audio and self.audio_alive()
        cls = RawRecording if mode == "raw" else EncodedRecording
        rec = cls(self, path_base, settings, with_audio)
        rec.start()
        self.recording = rec
        self.drops = 0
        return rec


# HDMI RX formats that mpph264enc takes as dmabuf and scales on the RGA itself
# (checked on the board: NV12, NV16 and BGR work; NV24 produces nothing).
ZERO_COPY_FORMATS = {"NV12", "NV16", "BGR"}


class StreamBranch:
    """Live preview: H.264 access units (Annex-B, SPS/PPS before every IDR) to a callback.

    Smooth first, sharp second (REC-02): the picture must keep up with real time.
    HDMI RX dmabuf frames (NV12 / NV16 / BGR) go straight into mpph264enc, which scales
    on the RGA - zero copy, no CPU. Anything else (NV24, test patterns in system memory)
    is scaled on the CPU with nearest-neighbour, which reads only the pixels it keeps:
    capture memory is uncached, and a bilinear scaler reading every 4K pixel managed only
    ~4 fps. A leaky one-buffer queue in front drops frames instead of building delay."""

    def __init__(self, cap, quality, on_au, zero_copy=True, lut=None):
        self.cap = cap
        self.quality = quality if quality in PREVIEW_QUALITIES else "medium"
        self.on_au = on_au
        self.zero_copy = zero_copy
        self.lut = lut                    # monitor.LutTable: the picture through a LUT (REC-13)
        self.lsrc = None
        self.bin = None
        self.tee_pad = None
        self.size = None
        self.fps = None
        self.bitrate = None
        self.frames = 0

    def owns(self, obj):
        while obj is not None:
            if obj == self.bin:
                return True
            obj = obj.get_parent()
        return False

    def _fit(self):
        w, h, _ = self.cap.timing
        (bw, bh), _, _ = PREVIEW_QUALITIES[self.quality]
        k = min(bw / w, bh / h, 1.0)
        return max(16, int(w * k) & ~15 if k < 1 else w & ~1), max(16, int(h * k) & ~1)

    def build(self):
        _, src_h, src_fps = self.cap.timing
        (_bw, _bh), max_fps, bps = PREVIEW_QUALITIES[self.quality]
        fps = max(1, min(max_fps, round(src_fps)))
        self.size, self.fps, self.bitrate = self._fit(), fps, bps
        w, h = self.size
        rate = f"videorate drop-only=true max-rate={fps} ! "
        # rounded framerate for the encoder's rate control (fractional fps confuse mpp)
        fix = f"capssetter caps=\"video/x-raw,framerate={fps}/1\" ! "
        key = max(1, fps // 2)              # keyframe every 0.5 s: a skipped viewer is back fast
        x264 = Gst.ElementFactory.find("x264enc") is not None
        mpp = Gst.ElementFactory.find("mpph264enc") is not None
        if x264:
            # Measured on the board from 4K NV16: nearest-neighbour + x264 ultrafast runs at
            # ~116 fps (360p) / ~76 fps (720p) - far above real time for every HDMI format.
            # The VPU is slower here: it pays per frame for system-memory input, and its CBR
            # rate control re-encodes frames (~22 fps even for 360p).
            # One slice per frame (sliced-threads=false, one thread): the browser's MSE muxer
            # (jmuxer) and some phone decoders cannot handle several slices per picture.
            enc = (f"x264enc name=penc tune=zerolatency speed-preset=ultrafast bitrate={bps // 1000} "
                   f"vbv-buf-capacity=400 key-int-max={key} byte-stream=true sliced-threads=false threads=1")
        elif mpp:
            enc = (f"mpph264enc name=penc rc-mode=vbr bps={bps} bps-max={bps * 3 // 2} gop={key} "
                   "header-mode=each-idr profile=main min-force-key-unit-interval=500000000")
        else:
            raise RuntimeError("no H.264 encoder for the preview (install gstreamer1.0-plugins-ugly)")
        parse = "h264parse config-interval=-1 ! " if Gst.ElementFactory.find("h264parse") else ""
        direct = not x264 and mpp and self.zero_copy and self.cap.format in ZERO_COPY_FORMATS \
            and not self.cap.sim and getattr(self.cap, "dmabuf", False) and not self.lut
        if direct:
            conv = f"{fix}{enc} width={w} height={h}"
        elif self.lut:
            # the scaled I420 frames go through the LUT in Python (monitor.LutTable), then on
            conv = (f"videoscale method=nearest-neighbour n-threads=4 ! video/x-raw,width={w},height={h} ! "
                    "videoconvert n-threads=2 ! video/x-raw,format=I420 ! "
                    "appsink name=lsink emit-signals=true sync=false async=false max-buffers=1 drop=true "
                    f"appsrc name=lsrc format=time is-live=true max-buffers=2 block=false ! {fix}{enc}")
        else:
            conv = (f"videoscale method=nearest-neighbour n-threads=4 ! video/x-raw,width={w},height={h} ! "
                    f"videoconvert n-threads=2 ! video/x-raw,format=I420 ! {fix}{enc}")
        desc = ("queue name=pq max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                f"{rate}{conv} ! {parse}"
                f"video/x-h264,stream-format=byte-stream,alignment=au{',profile=main' if x264 else ''} ! "
                "appsink name=psink emit-signals=true sync=false async=false max-buffers=4 drop=true")
        self.path = "zero-copy" if direct else "software x264" if x264 else "cpu-scale"
        if self.lut:
            self.path += " + LUT"
        self.bin = Gst.parse_bin_from_description(desc, False)
        pad = Gst.GhostPad.new("video", self.bin.get_by_name("pq").get_static_pad("sink"))
        self.bin.add_pad(pad)
        self.bin.get_by_name("psink").connect("new-sample", self._on_sample)
        if self.lut:
            self.lsrc = self.bin.get_by_name("lsrc")
            self.bin.get_by_name("lsink").connect("new-sample", self._on_lut)

    def _on_lut(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None or self.lsrc is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        if self.lsrc.get_property("caps") is None:
            self.lsrc.set_property("caps", sample.get_caps())
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = info.data
        finally:
            buf.unmap(info)
        w, h = self.size
        if len(data) == w * h * 3 // 2:
            try:
                data = self.lut.apply_i420(data, w, h)
            except Exception as e:            # never stop the preview over the LUT
                print(f"[warn] LUT: {e}")
                self.lut = None
        out = Gst.Buffer.new_wrapped(data)
        out.pts, out.dts, out.duration = buf.pts, buf.dts, buf.duration
        self.lsrc.emit("push-buffer", out)
        return Gst.FlowReturn.OK

    def attach(self):
        self.build()
        pipe = self.cap.pipeline
        pipe.add(self.bin)
        self.bin.sync_state_with_parent()
        tee = pipe.get_by_name("vt")
        self.tee_pad = tee.request_pad_simple("src_%u")
        if self.tee_pad.link(self.bin.get_static_pad("video")) != Gst.PadLinkReturn.OK:
            raise RuntimeError("could not link the preview branch")
        print(f"[info] preview {self.quality}: {self.size[0]}x{self.size[1]}@{self.fps} "
              f"{self.bitrate // 1000} kbit/s ({self.path})")

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        pts = buf.pts // 1000 if buf.pts != Gst.CLOCK_TIME_NONE else 0
        self.frames += 1
        try:
            self.on_au(key, pts, data)
        except Exception as e:  # never let the callback break the streaming thread
            print(f"[warn] preview callback: {e}")
        return Gst.FlowReturn.OK

    def force_keyframe(self):
        if self.bin:
            enc = self.bin.get_by_name("penc")
            ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            enc.get_static_pad("src").send_event(ev)

    def detach(self):
        """Unlink from the tee and remove the bin (no draining needed: nothing is saved)."""
        if not self.bin:
            return
        bin_, tpad, pipe = self.bin, self.tee_pad, self.cap.pipeline
        self.bin = None

        def unlink(pad, _info):
            if pad.is_linked():
                pad.unlink(bin_.get_static_pad("video"))

            def finish():
                bin_.set_state(Gst.State.NULL)
                if pipe:
                    try:
                        pipe.remove(bin_)
                        pipe.get_by_name("vt").release_request_pad(tpad)
                    except Exception:
                        pass
                return False
            GLib.idle_add(finish)
            return Gst.PadProbeReturn.REMOVE
        if tpad is not None and pipe is not None and pipe.get_state(0)[1] == Gst.State.PLAYING:
            tpad.add_probe(Gst.PadProbeType.IDLE, unlink)
        else:
            bin_.set_state(Gst.State.NULL)

    def describe(self):
        return {"quality": self.quality, "width": self.size[0] if self.size else 0,
                "height": self.size[1] if self.size else 0, "fps": self.fps,
                "bitrate": self.bitrate, "path": getattr(self, "path", None),
                "lut": self.lut.name if self.lut else None}


class TeeBranch:
    """A bin hung on the video tee while it is needed (analysis, zoom): attach, detach, owns."""

    bin = None
    tee_pad = None

    def owns(self, obj):
        while obj is not None:
            if obj == self.bin:
                return True
            obj = obj.get_parent()
        return False

    def attach(self):
        self.build()
        pipe = self.cap.pipeline
        pipe.add(self.bin)
        self.bin.sync_state_with_parent()
        self.tee_pad = pipe.get_by_name("vt").request_pad_simple("src_%u")
        if self.tee_pad.link(self.bin.get_static_pad("video")) != Gst.PadLinkReturn.OK:
            raise RuntimeError("could not link the %s branch" % type(self).__name__)

    def detach(self):
        if not self.bin:
            return
        bin_, tpad, pipe = self.bin, self.tee_pad, self.cap.pipeline
        self.bin = None

        def unlink(pad, _info):
            if pad.is_linked():
                pad.unlink(bin_.get_static_pad("video"))

            def finish():
                bin_.set_state(Gst.State.NULL)
                if pipe:
                    try:
                        pipe.remove(bin_)
                        pipe.get_by_name("vt").release_request_pad(tpad)
                    except Exception:
                        pass
                return False
            GLib.idle_add(finish)
            return Gst.PadProbeReturn.REMOVE
        if tpad is not None and pipe is not None and pipe.get_state(0)[1] == Gst.State.PLAYING:
            tpad.add_probe(Gst.PadProbeType.IDLE, unlink)
        else:
            bin_.set_state(Gst.State.NULL)


class AnalysisBranch(TeeBranch):
    """Exposure data (REC-13) from a small nearest-neighbour copy of the full frame - only
    the pixels it keeps are read, so a 4K capture costs next to nothing. A probe in front
    lets one frame through every ANALYSIS_EVERY seconds; the rest never enter the branch.
    The copy keeps the source's own format: its luma is read as the camera sent it (a
    colour conversion here would re-matrix it and every level would be off)."""

    def __init__(self, cap, on_data):
        self.cap = cap
        self.on_data = on_data
        self.last = 0.0
        self.count = 0

    def build(self):
        w, h, _ = self.cap.timing
        gw, gh = monitor.grid_size(w, h)
        self.gw, self.gh = gw, max(2, gh & ~1)
        desc = ("queue name=aq max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                f"videoscale method=nearest-neighbour ! video/x-raw,width={self.gw},height={self.gh} ! "
                "appsink name=asink emit-signals=true sync=false async=false max-buffers=1 drop=true")
        self.bin = Gst.parse_bin_from_description(desc, False)
        q = self.bin.get_by_name("aq").get_static_pad("sink")
        q.add_probe(Gst.PadProbeType.BUFFER, self._due)
        self.bin.add_pad(Gst.GhostPad.new("video", q))
        self.bin.get_by_name("asink").connect("new-sample", self._on_sample)

    def _due(self, _pad, _info):
        now = time.monotonic()
        if now - self.last < ANALYSIS_EVERY:
            return Gst.PadProbeReturn.DROP
        self.last = now
        return Gst.PadProbeReturn.OK

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        vi = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        n = vi.finfo.n_planes
        offsets, strides = list(vi.offset)[:n], list(vi.stride)[:n]
        vm = GstVideo.buffer_get_video_meta(buf)
        if vm:
            offsets, strides = list(vm.offset)[:n], list(vm.stride)[:n]
        full = int(vi.colorimetry.range) == 1 or None     # GST_VIDEO_COLOR_RANGE_0_255: full range
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = info.data
        finally:
            buf.unmap(info)
        try:
            y, full = monitor.sample_luma(data, vi.finfo.name, vi.width, vi.height, offsets, strides, full)
            meta, payload = monitor.analyze(y, full)
            self.count += 1
            if self.on_data:
                self.on_data(meta, payload)
        except Exception as e:
            print(f"[warn] exposure analysis: {e}")
        return Gst.FlowReturn.OK


# What a branch hung on the video tee is fed, checked on the board: a branch whose caps cannot
# be negotiated stops the whole capture - and the recording - through the tee, so only these
# are ever linked (the pad templates are no guide: videocrop's says NV16 and then refuses it).
VIDEOCROP_FORMATS = {"NV12", "NV21", "I420", "YV12", "YUY2", "UYVY", "YVYU", "BGR", "RGB",
                     "BGRx", "BGRA", "RGBx", "RGBA", "xRGB", "ARGB"}
# 4:2:2 / 4:4:4 semi-planar - what HDMI cameras send, and what videocrop cannot crop: the
# zoom cuts those itself (swap U/V?, chroma bytes per pixel)
PY_CROP = {"NV16": (False, 1), "NV61": (True, 1), "NV24": (False, 2), "NV42": (True, 2)}
ANALYSIS_FORMATS = VIDEOCROP_FORMATS | set(PY_CROP)    # videoscale nearest-neighbour takes all of these


class ZoomBranch(TeeBranch):
    """A viewer's loupe (REC-14): a square crop of the FULL frame around the point under it,
    sent next to the whole picture. The viewer says how big its circle is (`size`, a share of
    the picture's height); the crop is the part of the picture under the circle divided by the
    ratio - so the circle shows what it covers, `ratio` times larger - at the source's own
    pixels (scaled down only past ZOOM_MAX). Moving the point changes the crop of the running
    branch: no new encoder, no new stream.

    videocrop crops what it can (NV12, RGB, packed YUV ...). It cannot crop NV16 / NV24 -
    the 4:2:2 most cameras send - so for those the rows the crop needs are copied out of the
    frame (a third of it at 3x) and cut into I420 here; anything else is refused, never
    linked (a branch that cannot negotiate would stop the capture and the recording)."""

    def __init__(self, cap, zid, x, y, ratio, size, on_au):
        self.cap, self.zid, self.ratio, self.on_au = cap, zid, int(ratio), on_au
        self.x, self.y, self.size = x, y, size
        self.frames = 0
        w, h, fps = cap.timing
        self.src = (w, h)
        self.side = self.side_for(cap.timing, ratio, size)
        self.cw = self.ch = self.side
        o = min(self.side, ZOOM_MAX) & ~7
        self.out = (o, o)
        self.fps = max(1, min(30, round(fps)))

    @staticmethod
    def side_for(timing, ratio, size):
        """The crop's side in source pixels: a multiple of 8 (I420 rows need no padding on the
        cut-out path), at least 64, at most the picture's height."""
        w, h, _ = timing
        side = int(max(0.02, min(1.0, float(size))) * h / max(1, int(ratio)))
        return max(64, min(side, min(w, h))) & ~7

    def _crop(self):
        w, h = self.src
        left = int(min(max(0, self.x * w - self.cw / 2), w - self.cw)) & ~1
        top = int(min(max(0, self.y * h - self.ch / 2), h - self.ch)) & ~1
        return left, top, w - left - self.cw, h - top - self.ch

    def build(self):
        fmt = self.cap.format or ""
        self.py_crop = None
        if fmt not in VIDEOCROP_FORMATS:
            if fmt not in PY_CROP or not monitor.available():
                raise RuntimeError("the zoom cannot crop %s pictures" % (fmt or "these"))
            self.py_crop = PY_CROP[fmt]
        ow, oh = self.out
        px = ow * oh
        bps = 2_500_000 if px <= 640 * 360 else 4_000_000 if px <= 1280 * 720 else 6_000_000
        key = max(1, self.fps // 2)
        fix = f"capssetter caps=\"video/x-raw,framerate={self.fps}/1\" ! "
        if Gst.ElementFactory.find("x264enc"):
            enc = (f"x264enc name=zenc tune=zerolatency speed-preset=ultrafast bitrate={bps // 1000} "
                   f"vbv-buf-capacity=400 key-int-max={key} byte-stream=true sliced-threads=false threads=2")
        elif Gst.ElementFactory.find("mpph264enc"):
            enc = (f"mpph264enc name=zenc rc-mode=vbr bps={bps} bps-max={bps * 3 // 2} gop={key} "
                   "header-mode=each-idr profile=main")
        else:
            raise RuntimeError("no H.264 encoder for the zoom")
        parse = "h264parse config-interval=-1 ! " if Gst.ElementFactory.find("h264parse") else ""
        left, top, right, bottom = self._crop()
        scale = (f"videoscale ! video/x-raw,width={ow},height={oh} ! " if (ow, oh) != (self.cw, self.ch) else "")
        head = ("queue name=zq max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                f"videorate drop-only=true max-rate={self.fps} ! ")
        tail = (f"{fix}{enc} ! {parse}video/x-h264,stream-format=byte-stream,alignment=au"
                f"{',profile=main' if 'x264enc' in enc else ''} ! "
                "appsink name=zsink emit-signals=true sync=false async=false max-buffers=4 drop=true")
        if self.py_crop:
            desc = (head + "appsink name=csink emit-signals=true sync=false async=false max-buffers=1 drop=true "
                    "appsrc name=csrc format=time is-live=true max-buffers=2 block=false "
                    f"caps=video/x-raw,format=I420,width={self.cw},height={self.ch},framerate={self.fps}/1 ! "
                    f"{scale}{tail}")
        else:
            desc = (head + f"videocrop name=crop left={left} top={top} right={right} bottom={bottom} ! {scale}"
                    f"videoconvert n-threads=2 ! video/x-raw,format=I420 ! {tail}")
        self.bin = Gst.parse_bin_from_description(desc, False)
        self.bin.add_pad(Gst.GhostPad.new("video", self.bin.get_by_name("zq").get_static_pad("sink")))
        self.bin.get_by_name("zsink").connect("new-sample", self._on_sample)
        if self.py_crop:
            self.csrc = self.bin.get_by_name("csrc")
            self.bin.get_by_name("csink").connect("new-sample", self._on_frame)

    def _on_frame(self, sink):
        """NV16 / NV24 -> the crop as I420, copying only the rows it covers."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        np = monitor.np
        try:
            vi = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
            offsets, strides = list(vi.offset)[:2], list(vi.stride)[:2]
            vm = GstVideo.buffer_get_video_meta(buf)
            if vm:
                offsets, strides = list(vm.offset)[:2], list(vm.stride)[:2]
            swap, cbytes = self.py_crop
            left, top, _, _ = self._crop()
            cw, ch = self.cw, self.ch
            y = np.frombuffer(buf.extract_dup(offsets[0] + top * strides[0], ch * strides[0]), np.uint8)
            y = y.reshape(ch, strides[0])[:, left:left + cw]
            uv = np.frombuffer(buf.extract_dup(offsets[1] + top * strides[1], ch * strides[1]), np.uint8)
            uv = uv.reshape(ch, strides[1])[::2, left * cbytes:(left + cw) * cbytes]
            step = 2 * cbytes                     # 4:2:2: U V per 2 pixels; 4:4:4: per pixel, keep every other
            u, v = uv[:, 0::step], uv[:, 1::step]
            if swap:
                u, v = v, u
            out = Gst.Buffer.new_wrapped(y.tobytes() + u.tobytes() + v.tobytes())
            out.pts, out.duration = buf.pts, buf.duration
            self.csrc.emit("push-buffer", out)
        except Exception as e:                    # never stop the capture over the zoom
            print(f"[warn] zoom crop: {e}")
        return Gst.FlowReturn.OK

    def move(self, x, y):
        self.x, self.y = x, y                     # the Python crop reads the point on every frame
        crop = self.bin and self.bin.get_by_name("crop")
        if crop:
            left, top, right, bottom = self._crop()
            crop.set_property("left", left)
            crop.set_property("top", top)
            crop.set_property("right", right)
            crop.set_property("bottom", bottom)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        self.frames += 1
        try:
            self.on_au(self.zid, not buf.has_flags(Gst.BufferFlags.DELTA_UNIT),
                       buf.pts // 1000 if buf.pts != Gst.CLOCK_TIME_NONE else 0, data)
        except Exception as e:
            print(f"[warn] zoom callback: {e}")
        return Gst.FlowReturn.OK

    def force_keyframe(self):
        enc = self.bin and self.bin.get_by_name("zenc")
        if enc:
            ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            enc.get_static_pad("src").send_event(ev)

    def describe(self):
        left, top, _, _ = self._crop()
        w, h = self.src
        return {"id": self.zid, "on": True, "ratio": self.ratio, "x": round(self.x, 4), "y": round(self.y, 4),
                "size": self.size, "width": self.out[0], "height": self.out[1], "fps": self.fps,
                "src_width": w, "src_height": h, "crop": [left, top, self.cw, self.ch]}


class Recording:
    """Common part: a bin with 'video' (+ 'audio') ghost pads linked to the tees."""

    kind = "?"
    EOS_TIMEOUT = 8.0

    def __init__(self, cap, path, with_audio):
        self.cap = cap
        self.path = path
        self.with_audio = with_audio
        self.bin = None
        self.links = []                 # (tee, tee_pad, ghost_pad)
        self.started = None
        self.stopping = False
        self.finished = False
        self.error = None
        self._eos = threading.Event()
        self._on_done = None
        self.start_pts = 0
        # A/V sync (REC-09) and timecode (REC-11): counted where the branch takes the data
        self.first_pts = None
        self.last_end = None
        self.video_frames = 0
        self.audio_bytes = 0
        self.first_wall = None
        self.tc = None

    # subclasses build self.bin with ghost pads "video" and optionally "audio"
    def build(self):
        raise NotImplementedError

    def owns(self, obj):
        while obj is not None:
            if obj == self.bin:
                return True
            obj = obj.get_parent()
        return False

    def _ghost(self, name, element):
        pad = Gst.GhostPad.new(name, element.get_static_pad("sink"))
        self.bin.add_pad(pad)
        return pad

    def start(self):
        self.build()
        pipe = self.cap.pipeline
        pipe.add(self.bin)
        # recording time 0 = the next captured frame
        self.start_pts = self.cap.last_video_pts or 0
        self.bin.sync_state_with_parent()
        for tee_name, pad_name in (("vt", "video"), ("at", "audio")):
            ghost = self.bin.get_static_pad(pad_name)
            if not ghost:
                continue
            ghost.set_offset(-self.start_pts)
            tee = pipe.get_by_name(tee_name)
            tpad = tee.request_pad_simple("src_%u")
            tpad.add_probe(Gst.PadProbeType.BUFFER, self._drop_early)
            tpad.add_probe(Gst.PadProbeType.BUFFER, self._count, pad_name)
            if tpad.link(ghost) != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"could not link the {pad_name} branch")
            self.links.append((tee, tpad, ghost))
        self.started = time.monotonic()
        fsink = self.bin.get_by_name("fsink")
        fsink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_sink_event)
        print(f"[info] recording {self.kind} -> {self.path} ({cma_state()})")

    def _drop_early(self, _pad, info):
        buf = info.get_buffer()
        if buf.pts != Gst.CLOCK_TIME_NONE and buf.pts <= self.start_pts:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.REMOVE

    def _count(self, _pad, info, kind):
        buf = info.get_buffer()
        pts = buf.pts
        if pts == Gst.CLOCK_TIME_NONE or pts <= self.start_pts or self.stopping:
            return Gst.PadProbeReturn.OK
        if kind == "audio":
            self.audio_bytes += buf.get_size()
            return Gst.PadProbeReturn.OK
        if self.first_pts is None:
            self.first_pts = pts
            self.first_wall = self._wall_of(pts)
            w, h, fps = self.cap.timing
            n, d = _fraction(fps)
            self.tc = timecode.start_timecode(self.first_wall, n, d)
            try:
                self.cap.listener.on_recording_timecode(self)
            except Exception as e:
                print(f"[warn] timecode: {e}")
        dur = buf.duration if buf.duration != Gst.CLOCK_TIME_NONE else 0
        if not dur and self.cap.timing:
            dur = int(1e9 / max(1.0, self.cap.timing[2]))
        self.last_end = pts + dur
        self.video_frames += 1
        return Gst.PadProbeReturn.OK

    def _wall_of(self, pts):
        """Wall-clock time of a buffer's capture: its running time against the pipeline clock."""
        pipe = self.cap.pipeline
        try:
            clock = pipe.get_clock()
            now_rt = clock.get_time() - pipe.get_base_time()
            return time.time() - max(0, now_rt - pts) / 1e9
        except Exception:
            return time.time()

    def av_stats(self):
        """How far the audio ends from the video (REC-09): >0 = audio longer."""
        video_s = (self.last_end - self.first_pts) / 1e9 if self.first_pts is not None and self.last_end else 0.0
        audio_s = self.audio_bytes / (AUDIO_META["rate"] * AUDIO_META["channels"] * 2)
        d = {"video_s": round(video_s, 3), "frames": self.video_frames}
        if self.with_audio:
            d.update(audio_s=round(audio_s, 3), drift_ms=round((audio_s - video_s) * 1000, 1))
        return d

    def _on_sink_event(self, _pad, info):
        if info.get_event().type == Gst.EventType.EOS:
            self._eos.set()
        return Gst.PadProbeReturn.OK

    def elapsed(self):
        return (time.monotonic() - self.started) if self.started else 0.0

    def stop(self, sync=False, on_done=None):
        """Drain the branch (EOS) and detach it. on_done(rec) runs when finished.

        sync=True blocks until the file is closed, also when an asynchronous
        stop is already in progress (used when the pipeline is about to go away).
        """
        first = not self.stopping
        if first:
            self.stopping = True
            self._on_done = on_done
            self.duration = self.elapsed()
            self._stop_started = time.monotonic()
            for tee, tpad, ghost in self.links:
                tpad.add_probe(Gst.PadProbeType.IDLE, self._unlink, ghost)
        if sync:
            if not self.finished:
                self._eos.wait(max(0.0, self.EOS_TIMEOUT - (time.monotonic() - self._stop_started)))
                self._finish()
        elif first:
            def check():
                if self._eos.is_set() or time.monotonic() - self._stop_started > self.EOS_TIMEOUT:
                    self._finish()
                    return False
                return not self.finished
            GLib.timeout_add(50, check)

    def _unlink(self, tpad, _info, ghost):
        if tpad.is_linked():
            tpad.unlink(ghost)
            ghost.send_event(Gst.Event.new_eos())
        return Gst.PadProbeReturn.REMOVE

    def _finish(self):
        if self.finished:
            return
        self.finished = True
        if not self._eos.is_set():
            self.error = self.error or "file was not closed cleanly (timeout)"
        for tee, tpad, ghost in self.links:
            if tpad.is_linked():
                tpad.unlink(ghost)
            tee.release_request_pad(tpad)
        self.bin.set_state(Gst.State.NULL)
        if self.cap.pipeline:
            self.cap.pipeline.remove(self.bin)
        self._finalize_file()
        if self.cap.recording is self:
            self.cap.recording = None
        print(f"[info] recording stopped: {self.path} ({self.size_bytes() / 1e9:.2f} GB)")
        if self._on_done:
            self._on_done(self)

    def _finalize_file(self):
        pass

    def size_bytes(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def stats(self):
        return {"drops": self.cap.drops}


class EncodedRecording(Recording):
    """Real-time H.265 on the VPU.

    HDMI RX delivers NV12 frames as dmabuf, which mpph265enc imports without a
    copy. Frames in plain system memory (--simulate, other pixel formats) must
    instead be written into the encoder's own buffers by videoconvert: given
    system memory directly, mpph265enc leaks a frame buffer per frame (see
    gstutil). NV12 is converted to I420 there, since a same-format
    videoconvert would pass the buffer through unchanged.

    Other HDMI formats (RGB from most cameras, NV24, NV16) need a CPU conversion to
    NV12 (mpph265enc's own RGB input is broken). videoconvert reading straight from
    the capture buffers is slow and holds them, which starves the capture (40 fps
    from a 1080p60 RGB camera); a bulk copy out of the capture buffer is fast. So the
    frames go appsink -> copy -> appsrc, and the conversion runs from the copy on
    its own thread.
    """
    kind = "H.265"

    def __init__(self, cap, path_base, s, with_audio):
        self.s = s
        ext = {"mkv": ".mkv", "mov": ".mov"}.get(s["container"], ".mp4")
        super().__init__(cap, path_base + ext, with_audio)
        self.sysmem_frames = 0
        self.esrc = None
        self.late_drops = 0

    def build(self):
        s, (_w, _h, fps) = self.s, self.cap.timing
        bps = int(s["bitrate"] * 1_000_000)
        gop = max(1, round(fps * s["gop"]))
        rc = "vbr" if s["rc"] == "vbr" else "cbr"
        extra = f" bps-max={int(bps * 1.5)} bps-min={bps // 2}" if rc == "vbr" else ""
        copy = self.cap.format != "NV12"
        if self.cap.format != "NV12":
            conv = "videoconvert n-threads=4 ! video/x-raw,format=NV12 ! "
        elif self.cap.sim:
            conv = "videoconvert n-threads=4 ! video/x-raw,format=I420 ! "
        else:
            conv = ""
        if s["container"] == "mkv":
            mux = "matroskamux name=mux"
        else:
            # periodically rewritten index: the file stays playable after a crash / power cut
            mux = (f"{'qtmux' if s['container'] == 'mov' else 'mp4mux'} name=mux "
                   "reserved-max-duration=14400000000000 reserved-moov-update-period=1000000000")
        # mpph265enc's rate control breaks on fractional frame rates (59.94 fps -> ~15x the
        # bitrate); give it the rounded rate, the buffer timestamps stay exact
        enc = (f"capssetter caps=\"video/x-raw,framerate={max(1, round(fps))}/1\" ! "
               f"mpph265enc rc-mode={rc} bps={bps}{extra} gop={gop} header-mode=each-idr ! "
               f"h265parse ! {mux} ! filesink name=fsink sync=false async=false")
        vq = "queue name=vq max-size-buffers=8 max-size-bytes=0 max-size-time=0 ! "
        if copy:
            desc = (f"{vq}appsink name=vsink emit-signals=true sync=false async=false "
                    "max-buffers=0 "
                    "appsrc name=esrc format=time is-live=true max-buffers=8 block=false ! "
                    f"queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 ! {conv}{enc}")
        else:
            desc = f"{vq}{conv}{enc}"
        if self.with_audio:
            # MOV and MKV keep the audio as it came (16-bit PCM, REC-09); MP4 cannot hold PCM here
            audio = ("audioconvert ! voaacenc bitrate=256000 ! aacparse" if s["container"] == "mp4" else
                     "audioconvert ! audio/x-raw,format=S16LE,layout=interleaved")
            desc += (" queue name=aq max-size-time=3000000000 max-size-bytes=0 max-size-buffers=0 ! "
                     f"{audio} ! mux.")
        self.bin = Gst.parse_bin_from_description(desc, False)
        self.bin.get_by_name("fsink").set_property("location", self.path)
        vq = self.bin.get_by_name("vq")
        self._ghost("video", vq)
        if self.with_audio:
            self._ghost("audio", self.bin.get_by_name("aq"))
        if not conv:
            vq.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._check_memory)
        if copy:
            self.esrc = self.bin.get_by_name("esrc")
            vs = self.bin.get_by_name("vsink")
            vs.connect("new-sample", self._on_video)
            vs.connect("eos", lambda _s: self.esrc.emit("end-of-stream"))

    def _on_video(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        if self.esrc.get_property("caps") is None:
            self.esrc.set_property("caps", sample.get_caps())
        if self.esrc.get_property("current-level-buffers") >= 8:
            self.late_drops += 1          # conversion can't keep up: drop, don't stall capture
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        out = buf.copy_deep()             # fast bulk copy; the capture buffer goes back now
        out.pts = max(0, buf.pts - self.start_pts)
        out.duration = buf.duration
        self.esrc.emit("push-buffer", out)
        return Gst.FlowReturn.OK

    def _check_memory(self, _pad, info):
        # v4l2src falls back to system-memory copies when it runs short of buffers
        if not is_dmabuf(info.get_buffer()):
            self.sysmem_frames += 1
        return Gst.PadProbeReturn.OK

    def bytes_per_second(self):
        audio = (32000 if self.s["container"] == "mp4" else 192000) if self.with_audio else 0
        return self.s["bitrate"] * 1e6 / 8 + audio

    def _finalize_file(self):
        # the time-of-day timecode as a QuickTime tmcd track, where editors read it (REC-11)
        if self.tc and self.path.endswith((".mov", ".mp4")) and self.size_bytes() > 0:
            try:
                timecode.add_tmcd(self.path, self.tc)
            except (OSError, ValueError) as e:
                print(f"[warn] no timecode track in {self.path}: {e}")

    def stats(self):
        return {"drops": self.cap.drops + self.late_drops, "sysmem_frames": self.sysmem_frames,
                "av": self.av_stats()}


class RawRecording(Recording):
    kind = "RAW"
    EOS_TIMEOUT = 30.0                  # the write buffer may hold a few seconds of video

    def __init__(self, cap, path_base, s, with_audio):
        super().__init__(cap, path_base + ".arh", with_audio)
        self.s = s
        self.writer = None
        self.lock = threading.Lock()
        self.closing = False
        self.disk_drops = 0
        self.audio_samples = 0
        self.eos_pending = 2 if with_audio else 1
        # write buffer: absorbs disk stalls (~2 s of 4K60 NV12), bounded by RAM
        try:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            ram = 4 << 30
        self.max_buffer = int(min(1.5 * (1 << 30), ram * 0.25))

    def build(self):
        desc = ("queue name=vq max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! "
                "appsink name=vsink emit-signals=true sync=false async=false max-buffers=0 "
                "appsrc name=wsrc format=bytes block=false ! "
                "filesink name=fsink sync=false async=false buffer-mode=2")
        if self.with_audio:
            desc += (" queue name=aq max-size-time=2000000000 max-size-bytes=0 max-size-buffers=0 ! "
                     "appsink name=asink emit-signals=true sync=false async=false")
        self.bin = Gst.parse_bin_from_description(desc, False)
        self.bin.get_by_name("fsink").set_property("location", self.path)
        self.wsrc = self.bin.get_by_name("wsrc")
        self.wsrc.set_property("max-bytes", self.max_buffer + (256 << 20))
        vs = self.bin.get_by_name("vsink")
        vs.connect("new-sample", self._on_video)
        vs.connect("eos", self._on_branch_eos)
        self._ghost("video", self.bin.get_by_name("vq"))
        if self.with_audio:
            a = self.bin.get_by_name("asink")
            a.connect("new-sample", self._on_audio)
            a.connect("eos", self._on_branch_eos)
            self._ghost("audio", self.bin.get_by_name("aq"))

    def _meta(self, sample, buf):
        caps = sample.get_caps()
        vi = GstVideo.VideoInfo.new_from_caps(caps)
        n = vi.finfo.n_planes
        offsets, strides = list(vi.offset)[:n], list(vi.stride)[:n]
        vm = GstVideo.buffer_get_video_meta(buf)
        if vm:
            offsets, strides = list(vm.offset)[:n], list(vm.stride)[:n]
        w, h, fps = self.cap.timing
        return {
            "format": "ARH", "version": arh.VERSION,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "software": SOFTWARE,
            "source": {"device": self.cap.dev if not self.cap.sim else getattr(self.cap.sim, "device", "simulated"),
                       "signal": f"{w}x{h}p{fps:.2f}"},
            "video": {"format": vi.finfo.name, "width": vi.width, "height": vi.height,
                      "fps_n": vi.fps_n, "fps_d": vi.fps_d, "frame_size": buf.get_size(),
                      "plane_offsets": offsets, "plane_strides": strides,
                      "colorimetry": vi.colorimetry.to_string(), "caps": caps.to_string()},
            "audio": dict(AUDIO_META) if self.with_audio else None,
        }

    def _push(self, header, data_buf):
        self.wsrc.emit("push-buffer", gst_buffer(header))
        if data_buf is not None:
            self.wsrc.emit("push-buffer", data_buf)

    def _buffer_full(self, size):
        return self.wsrc.get_property("current-level-bytes") + size > self.max_buffer

    def _on_video(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None or self.closing:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        with self.lock:
            if self.writer is None:
                self.writer = arh.ArhWriter(self.path, self._meta(sample, buf))
                self.base_pts = buf.pts
                self._push(self.writer.header(), None)
            size = buf.get_size()
            if self._buffer_full(size):
                self.disk_drops += 1      # disk can't keep up: drop rather than stall capture
                return Gst.FlowReturn.OK
            # copy out of the capture buffer so the driver gets it back immediately
            data = buf.copy_deep()
            seq = buf.offset if buf.offset != Gst.BUFFER_OFFSET_NONE else self.writer.frames
            head = self.writer.video_chunk(size, max(0, buf.pts - self.base_pts), seq)
            self._push(head, data)
        return Gst.FlowReturn.OK

    def _on_audio(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None or self.closing:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        with self.lock:
            if self.writer is None or buf.pts < self.base_pts:
                return Gst.FlowReturn.OK
            size = buf.get_size()
            head = self.writer.audio_chunk(size, buf.pts - self.base_pts, self.audio_samples)
            self.audio_samples += size // 4
            self._push(head, buf.copy_deep())
        return Gst.FlowReturn.OK

    def _on_branch_eos(self, _sink):
        with self.lock:
            self.eos_pending -= 1
            if self.eos_pending == 0:
                self.closing = True
                self.wsrc.emit("end-of-stream")

    def _finalize_file(self):
        if self.writer is None:
            try:
                os.remove(self.path)       # nothing was captured
            except OSError:
                pass
            return
        if self.tc:
            self.writer.meta["timecode"] = self.tc              # REC-11
        self.writer.meta["sync"] = self.av_stats()              # REC-09
        try:
            arh.pack_header(self.writer.meta)
        except ValueError:                                      # 4 KB header: the sync figures go
            self.writer.meta.pop("sync", None)                  # to the take's sidecar only
        try:
            self.writer.finalize()
        except OSError as e:
            self.error = f"could not finalize {self.path}: {e}"

    def buffer_fill(self):
        try:
            return self.wsrc.get_property("current-level-bytes") / self.max_buffer
        except Exception:
            return 0.0

    def bytes_per_second(self):
        w, h, fps = self.cap.timing or (3840, 2160, 60)
        bpp = {"NV12": 1.5, "NV21": 1.5, "NV16": 2, "NV61": 2, "YUY2": 2, "UYVY": 2}.get(
            self.cap.format, 3)
        return w * h * bpp * fps + (192000 if self.with_audio else 0)

    def stats(self):
        return {"drops": self.cap.drops, "disk_drops": self.disk_drops,
                "buffer": self.buffer_fill(),
                "frames": self.writer.frames if self.writer else 0, "av": self.av_stats()}
