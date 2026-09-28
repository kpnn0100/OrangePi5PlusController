"""Playback of recordings in the video area (gallery).

  H.265 / FFV1 files  playbin. For H.265 the hardware decoder (mppvideodec) is told to
                      output the picture already scaled to fit the screen (it scales with
                      RGA, ~130 fps from 4K); FFV1 is decoded on the CPU.
  RAW (.arh)          appsrc fed from the file's chunk index, so seeking is instant;
                      audio (if recorded) is played alongside.

rkximagesink never scales, so everything is scaled to `size` before it, like the live
preview. The live capture must be stopped while playing: both use the same display plane.
"""
import bisect
import os

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from . import arh, library  # noqa: E402
from .gstutil import gst_buffer  # noqa: E402
from .v4l2 import DISPLAY_FORMATS  # noqa: E402


def fit(w, h, box):
    """Largest size with the video's aspect that fits box. The width is a multiple of 16:
    the RGA scaler behind mppvideodec fails on other widths ("RGA_BLIT fail", e.g. 1706),
    and the decoder then outputs nothing ("No valid frames decoded")."""
    bw, bh = box
    k = min(bw / w, bh / h, 1.0) if w and h else 1.0
    pw = max(16, int(w * k) // 16 * 16)
    return pw, max(2, round(pw * h / w) & ~1) if w else max(2, int(h * k) & ~1)


class Player:
    """listener: on_sync_message(bus, msg), on_player_event(player, event, detail)
    with event one of 'playing', 'paused', 'ended', 'error'."""

    def __init__(self, item, box, listener, plane_id=None):
        self.item = item
        self.listener = listener
        self.plane_id = plane_id
        self.size = fit(item.width or 1920, item.height or 1080, box)
        self.pipeline = None
        self.playing = False
        self._raw = None
        self.hw_scale = True          # let mppvideodec scale (RGA); off after a failure
        self._build()

    # ------------------------------------------------------------ building

    def _video_chain(self, src_format=None):
        pw, ph = self.size
        if src_format and src_format in DISPLAY_FORMATS:
            target = src_format
        elif src_format in ("BGR", "RGB", "BGRx", "RGBx"):
            target = "BGRx"
        elif src_format in ("NV24", "NV42", "Y444"):
            target = "NV16"
        else:
            target = "NV12"
        plane = f" plane-id={self.plane_id}" if self.plane_id is not None else ""
        return (f"videoscale method=nearest-neighbour ! video/x-raw,width={pw},height={ph} ! "
                f"videoconvert n-threads=4 ! video/x-raw,format={target} ! "
                f"rkximagesink name=psink force-aspect-ratio=true sync=true{plane}")

    def _build(self):
        if self.item.kind == library.RAW:
            self._build_raw()
        else:
            self.pipeline = Gst.ElementFactory.make("playbin", "player")
            self.pipeline.set_property("uri", Gst.filename_to_uri(self.item.path))
            vsink = Gst.parse_bin_from_description(self._video_chain(), True)
            self.pipeline.set_property("video-sink", vsink)
            self.pipeline.connect("element-setup", self._on_element_setup)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        bus.enable_sync_message_emission()
        bus.connect("sync-message::element", self.listener.on_sync_message)

    def _on_element_setup(self, _playbin, element):
        # let the hardware decoder scale (RGA) instead of the CPU
        factory = element.get_factory()
        if factory and factory.get_name() == "mppvideodec" and self.hw_scale:
            element.set_property("width", self.size[0])
            element.set_property("height", self.size[1])

    def _build_raw(self):
        r = arh.ArhReader(self.item.path)
        v, a = r.video, r.audio
        video, audio = arh.chunk_index(self.item.path)
        if not video:
            raise ValueError("no complete frames in this file")
        self._raw = {
            "video": video, "audio": audio, "vpos": 0, "apos": 0,
            "pts0": video[0][1], "frame_size": v["frame_size"],
            "frame_ns": int(1e9 * v["fps_d"] / v["fps_n"]),
            "vf": open(self.item.path, "rb"), "af": open(self.item.path, "rb") if audio else None,
            "vpts": [p for _, p, _ in video], "apts": [p for _, p, _ in audio],
            "audio_fmt": a,
        }
        self.duration_ns = video[-1][1] - video[0][1] + self._raw["frame_ns"]
        desc = (f"appsrc name=vsrc format=time stream-type=seekable max-buffers=3 "
                f"caps=video/x-raw,format={v['format']},width={v['width']},height={v['height']},"
                f"framerate={v['fps_n']}/{v['fps_d']} ! queue max-size-buffers=3 ! "
                f"{self._video_chain(v['format'])}")
        if audio:
            desc += (f" appsrc name=asrc format=time stream-type=seekable max-bytes=400000 "
                     f"caps=audio/x-raw,format={a['format']},rate={a['rate']},"
                     f"channels={a['channels']},layout=interleaved ! queue ! "
                     "audioconvert ! audioresample ! autoaudiosink")
        self.pipeline = Gst.parse_launch(desc)
        vsrc = self.pipeline.get_by_name("vsrc")
        vsrc.connect("need-data", self._need_video)
        vsrc.connect("seek-data", self._seek_video)
        if audio:
            asrc = self.pipeline.get_by_name("asrc")
            asrc.connect("need-data", self._need_audio)
            asrc.connect("seek-data", self._seek_audio)

    # appsrc callbacks run on GStreamer threads
    def _need_video(self, src, _length):
        r = self._raw
        if r["vpos"] >= len(r["video"]):
            src.emit("end-of-stream")
            return
        off, pts, _seq = r["video"][r["vpos"]]
        r["vf"].seek(off)
        buf = gst_buffer(r["vf"].read(r["frame_size"]))
        buf.pts, buf.duration = pts - r["pts0"], r["frame_ns"]
        r["vpos"] += 1
        src.emit("push-buffer", buf)

    def _seek_video(self, _src, offset):
        r = self._raw
        r["vpos"] = min(bisect.bisect_left(r["vpts"], r["pts0"] + offset), len(r["video"]))
        return True

    def _need_audio(self, src, _length):
        r = self._raw
        if r["apos"] >= len(r["audio"]):
            src.emit("end-of-stream")
            return
        off, pts, size = r["audio"][r["apos"]]
        r["af"].seek(off)
        data = r["af"].read(size)
        a = r["audio_fmt"]
        buf = gst_buffer(data)
        buf.pts = max(0, pts - r["pts0"])
        buf.duration = int(len(data) / (a["channels"] * 2) * 1e9 / a["rate"])
        r["apos"] += 1
        src.emit("push-buffer", buf)

    def _seek_audio(self, _src, offset):
        r = self._raw
        r["apos"] = min(bisect.bisect_left(r["apts"], r["pts0"] + offset), len(r["audio"]))
        return True

    # ------------------------------------------------------------ control

    def play(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.listener.on_player_event(self, "error", "could not start playback")
            return
        self.playing = True
        self.listener.on_player_event(self, "playing", None)

    def pause(self):
        self.pipeline.set_state(Gst.State.PAUSED)
        self.playing = False
        self.listener.on_player_event(self, "paused", None)

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            if self._ended:
                self.seek(0)
            self.play()

    _ended = False

    def position(self):
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        return pos / 1e9 if ok and pos >= 0 else 0.0

    def duration(self):
        if self._raw:
            return self.duration_ns / 1e9
        ok, dur = self.pipeline.query_duration(Gst.Format.TIME)
        return dur / 1e9 if ok and dur > 0 else (self.item.duration or 0.0)

    def seek(self, seconds):
        self._ended = False
        seconds = max(0.0, min(seconds, max(0.0, self.duration() - 0.05)))
        self.pipeline.seek_simple(Gst.Format.TIME,
                                  Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                  int(seconds * Gst.SECOND))

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline = None
        if self._raw:
            for k in ("vf", "af"):
                if self._raw[k]:
                    self._raw[k].close()
            self._raw = None

    def _on_message(self, _bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self._ended = True
            self.pipeline.set_state(Gst.State.PAUSED)
            self.playing = False
            self.listener.on_player_event(self, "ended", None)
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if self.item.kind != library.RAW and self.hw_scale:
                # e.g. RGA refusing a size: decode at full size and scale on the CPU instead
                print(f"[warn] playback: hardware scaling failed ({err.message}); "
                      "retrying without it")
                self.hw_scale = False
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline.get_bus().remove_signal_watch()
                self._build()
                self.play()
                return
            print(f"[error] playback {os.path.basename(self.item.path)}: {err.message}\n"
                  f"        {dbg}")
            self.playing = False
            self.listener.on_player_event(self, "error", err.message)


def start(item, box, listener, plane_id=None):
    """Create and start a Player; returns it, or raises ValueError / GLib.Error."""
    p = Player(item, box, listener, plane_id)
    GLib.idle_add(lambda: p.play() and False)
    return p
