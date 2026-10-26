"""H.264 / H.265 on the Rockchip VPU through its own library (librockchip_mpp), not mpph26xenc.

Why: gst-rockchip 1.14 (this board's mpph264enc / mpph265enc) keeps ONE frame in the encoder
at a time and waits ~20 ms per 1080p frame for each packet, so a 4K -> 1080p H.264 conversion
ran at 7-9 fps. The same VPU through the library encodes the same real frames at 113 fps
(fixed QP 23) to 160 fps (VBR) - measured with Rockchip's mpi_enc_test. The library also
never sees GStreamer memory, so the system-memory leak of mpph265enc (gstutil) cannot happen.

The library is reached with ctypes (no build step on the board); everything it needs is in
librockchip-mpp, which the VPU elements depend on anyway. Two pieces:

    Encoder   frames (NV12 bytes, any GStreamer strides) in, Annex-B access units out
    Bridge    puts an Encoder between `appsink name=mppin` and `appsrc name=mppout` of a
              pipeline, so a conversion keeps its GStreamer decode, scale, mux and audio

`fragment(codec, ...)` returns the pipeline text for the place of the encoder element.
"""
import ctypes
import ctypes.util
import logging
import threading

log = logging.getLogger("arstro.mpp")

# rk_type.h / rk_mpi_cmd.h / mpp_buffer.h / rk_venc_cmd.h
MPP_CTX_ENC = 1
CODING = {"h264": 7, "h265": 0x01000004}          # MPP_VIDEO_CodingAVC / CodingHEVC
MPP_FMT_YUV420SP = 0                              # NV12
MPP_BUFFER_TYPE_DRM = 3
MPP_BUFFER_INTERNAL = 0
MPP_SET_INPUT_TIMEOUT = 0x00200006
MPP_SET_OUTPUT_TIMEOUT = 0x00200007
MPP_ENC_SET_CFG = 0x00320001
MPP_ENC_GET_CFG = 0x00320002
MPP_ENC_SET_HEADER_MODE = 0x00328001
MPP_ENC_HEADER_MODE_EACH_IDR = 1
MPP_POLL_BLOCK = -1
RC_MODES = {"vbr": 0, "cbr": 1, "fixqp": 2}
TAG = b"arstro"


class MppError(RuntimeError):
    pass


class _MppApi(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("version", ctypes.c_uint32)] + [
        (name, ctypes.c_void_p) for name in (
            "decode", "decode_put_packet", "decode_get_frame", "encode", "encode_put_frame",
            "encode_get_packet", "isp", "isp_put_frame", "isp_get_frame", "poll", "dequeue",
            "enqueue", "reset", "control")] + [("reserv", ctypes.c_uint32 * 16)]


_PUT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_GET = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_CONTROL = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)

_lib = None
_lib_error = None


def _load():
    """The library with its prototypes, or None (no Rockchip VPU library on this machine)."""
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    names = [ctypes.util.find_library("rockchip_mpp"), "librockchip_mpp.so.1", "librockchip_mpp.so.0"]
    for name in filter(None, names):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError as e:
            _lib_error = str(e)
    else:
        _lib_error = _lib_error or "librockchip_mpp not found"
        return None
    vp, i32, u32 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
    proto = {
        "mpp_create": (i32, [ctypes.POINTER(vp), ctypes.POINTER(ctypes.POINTER(_MppApi))]),
        "mpp_init": (i32, [vp, i32, i32]),
        "mpp_destroy": (i32, [vp]),
        "mpp_enc_cfg_init": (i32, [ctypes.POINTER(vp)]),
        "mpp_enc_cfg_deinit": (i32, [vp]),
        "mpp_enc_cfg_set_s32": (i32, [vp, ctypes.c_char_p, i32]),
        "mpp_buffer_group_get": (i32, [ctypes.POINTER(vp), i32, i32, ctypes.c_char_p, ctypes.c_char_p]),
        "mpp_buffer_group_put": (i32, [vp]),
        "mpp_buffer_get_with_tag": (i32, [vp, ctypes.POINTER(vp), ctypes.c_size_t, ctypes.c_char_p,
                                          ctypes.c_char_p]),
        "mpp_buffer_put_with_caller": (i32, [vp, ctypes.c_char_p]),
        "mpp_buffer_get_ptr_with_caller": (vp, [vp, ctypes.c_char_p]),
        "mpp_frame_init": (i32, [ctypes.POINTER(vp)]),
        "mpp_frame_deinit": (i32, [ctypes.POINTER(vp)]),
        "mpp_frame_set_width": (None, [vp, u32]),
        "mpp_frame_set_height": (None, [vp, u32]),
        "mpp_frame_set_hor_stride": (None, [vp, u32]),
        "mpp_frame_set_ver_stride": (None, [vp, u32]),
        "mpp_frame_set_fmt": (None, [vp, i32]),
        "mpp_frame_set_buffer": (None, [vp, vp]),
        "mpp_frame_set_eos": (None, [vp, u32]),
        "mpp_packet_deinit": (i32, [ctypes.POINTER(vp)]),
        "mpp_packet_get_pos": (vp, [vp]),
        "mpp_packet_get_length": (ctypes.c_size_t, [vp]),
        "mpp_packet_is_partition": (u32, [vp]),
        "mpp_packet_is_eoi": (u32, [vp]),
    }
    try:
        for fn, (res, argtypes) in proto.items():
            f = getattr(lib, fn)
            f.restype, f.argtypes = res, argtypes
    except AttributeError as e:
        _lib_error = "librockchip_mpp lacks %s" % e
        return None
    _lib = lib
    return lib


def available():
    return _load() is not None


def _align(n, a=16):
    return (n + a - 1) // a * a


def _keyframe(au, codec):
    """True when the access unit holds an IDR picture (Annex-B NAL types)."""
    i, n = 0, len(au)
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= n:
            return False
        t = au[i + 3]
        if codec == "h264" and t & 0x1F == 5:
            return True
        if codec == "h265" and (t >> 1) & 0x3F in (19, 20, 21):
            return True
        i += 3


class Encoder:
    """One VPU encoder context. Not thread-safe: one thread calls encode()/close()."""

    def __init__(self, codec, width, height, fps, gop=None, qp=None, bps=None, rc="vbr", nbuf=2):
        lib = _load()
        if lib is None:
            raise MppError("VPU library not available: %s" % _lib_error)
        self.lib, self.codec = lib, codec
        self.width, self.height = width, height
        self.hs, self.vs = _align(width), _align(height)
        self.ctx = ctypes.c_void_p()
        self.api = ctypes.POINTER(_MppApi)()
        self.cfg = ctypes.c_void_p()
        self.group = ctypes.c_void_p()
        self.bufs, self.ptrs = [], []
        self._next = 0
        try:
            self._check(lib.mpp_create(ctypes.byref(self.ctx), ctypes.byref(self.api)), "create")
            api = self.api.contents
            self._put = _PUT(api.encode_put_frame)
            self._get = _GET(api.encode_get_packet)
            self._control = _CONTROL(api.control)
            self._check(lib.mpp_init(self.ctx, MPP_CTX_ENC, CODING[codec]), "init")
            for cmd in (MPP_SET_INPUT_TIMEOUT, MPP_SET_OUTPUT_TIMEOUT):
                t = ctypes.c_int64(MPP_POLL_BLOCK)
                self._check(self._control(self.ctx, cmd, ctypes.cast(ctypes.byref(t), ctypes.c_void_p)), "timeout")
            self._configure(fps, gop or max(1, round(fps)), qp, bps, rc)
            self._check(lib.mpp_buffer_group_get(ctypes.byref(self.group), MPP_BUFFER_TYPE_DRM,
                                                 MPP_BUFFER_INTERNAL, TAG, b"Encoder"), "buffer group")
            size = _align(self.hs, 64) * _align(self.vs, 64) * 3 // 2
            for _ in range(nbuf):
                b = ctypes.c_void_p()
                self._check(lib.mpp_buffer_get_with_tag(self.group, ctypes.byref(b), size, TAG, b"Encoder"),
                            "buffer")
                self.bufs.append(b)
                self.ptrs.append(lib.mpp_buffer_get_ptr_with_caller(b, b"Encoder"))
        except Exception:
            self.close()
            raise

    def _check(self, ret, what):
        if ret:
            raise MppError("VPU encoder: %s failed (%d)" % (what, ret))

    def _configure(self, fps, gop, qp, bps, rc):
        lib, cfg = self.lib, self.cfg
        self._check(lib.mpp_enc_cfg_init(ctypes.byref(cfg)), "cfg init")
        self._check(self._control(self.ctx, MPP_ENC_GET_CFG, cfg), "get cfg")
        fps = max(1, round(fps))          # fractional rates confuse MPP's rate control
        mode = "fixqp" if qp else rc
        values = {
            "prep:width": self.width, "prep:height": self.height,
            "prep:hor_stride": self.hs, "prep:ver_stride": self.vs, "prep:format": MPP_FMT_YUV420SP,
            "rc:mode": RC_MODES[mode], "rc:gop": gop, "rc:max_reenc_times": 0,
            "rc:fps_in_flex": 0, "rc:fps_in_num": fps, "rc:fps_in_denorm": 1,
            "rc:fps_out_flex": 0, "rc:fps_out_num": fps, "rc:fps_out_denorm": 1,
            "codec:type": CODING[self.codec],
        }
        if qp:
            for k in ("rc:qp_init", "rc:qp_min", "rc:qp_max", "rc:qp_min_i", "rc:qp_max_i",
                      "rc:fqp_min_i", "rc:fqp_max_i", "rc:fqp_min_p", "rc:fqp_max_p"):
                values[k] = qp
            values["rc:qp_ip"] = 0
        else:
            bps = int(bps or self.width * self.height * fps / 8)
            values["rc:bps_target"] = bps
            values["rc:bps_max"] = bps if mode == "cbr" else bps * 5 // 4
            values["rc:bps_min"] = bps if mode == "cbr" else bps // 2
        if self.codec == "h264":
            values.update({"h264:profile": 100, "h264:level": 41 if self.width * self.height <= 1920 * 1088 else 51,
                           "h264:cabac_en": 1, "h264:cabac_idc": 0, "h264:trans8x8": 1})
        for k, v in values.items():
            self._check(lib.mpp_enc_cfg_set_s32(cfg, k.encode(), int(v)), "set " + k)
        self._check(self._control(self.ctx, MPP_ENC_SET_CFG, cfg), "set cfg")
        mode_v = ctypes.c_int(MPP_ENC_HEADER_MODE_EACH_IDR)   # SPS/PPS before every IDR
        self._check(self._control(self.ctx, MPP_ENC_SET_HEADER_MODE,
                                  ctypes.cast(ctypes.byref(mode_v), ctypes.c_void_p)), "header mode")

    def _fill(self, ptr, data, strides, offsets):
        """Copy an NV12 frame (GStreamer layout `strides`/`offsets`) into a VPU buffer."""
        src = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p).value
        dst, hs = ptr, self.hs
        planes = ((offsets[0], strides[0], 0, self.height),
                  (offsets[1], strides[1], hs * self.vs, (self.height + 1) // 2))
        for off, stride, doff, rows in planes:
            if stride == hs:
                ctypes.memmove(dst + doff, src + off, hs * rows)
            else:
                n = min(stride, hs)
                for r in range(rows):
                    ctypes.memmove(dst + doff + r * hs, src + off + r * stride, n)

    def encode(self, data, strides, offsets):
        """Encode one frame; returns [(access unit bytes, is keyframe)] (normally one)."""
        lib = self.lib
        i = self._next
        self._next = (i + 1) % len(self.bufs)
        self._fill(self.ptrs[i], data, strides, offsets)
        frame = ctypes.c_void_p()
        self._check(lib.mpp_frame_init(ctypes.byref(frame)), "frame init")
        try:
            lib.mpp_frame_set_width(frame, self.width)
            lib.mpp_frame_set_height(frame, self.height)
            lib.mpp_frame_set_hor_stride(frame, self.hs)
            lib.mpp_frame_set_ver_stride(frame, self.vs)
            lib.mpp_frame_set_fmt(frame, MPP_FMT_YUV420SP)
            lib.mpp_frame_set_buffer(frame, self.bufs[i])
            lib.mpp_frame_set_eos(frame, 0)
            self._check(self._put(self.ctx, frame), "put frame")
        finally:
            lib.mpp_frame_deinit(ctypes.byref(frame))
        out, part = [], b""
        while True:
            pkt = ctypes.c_void_p()
            self._check(self._get(self.ctx, ctypes.byref(pkt)), "get packet")
            if not pkt.value:
                continue
            try:
                n = lib.mpp_packet_get_length(pkt)
                data = ctypes.string_at(lib.mpp_packet_get_pos(pkt), n) if n else b""
                partition = lib.mpp_packet_is_partition(pkt)
                eoi = lib.mpp_packet_is_eoi(pkt) if partition else 1
            finally:
                lib.mpp_packet_deinit(ctypes.byref(pkt))
            part += data
            if eoi:
                break
        if part:
            out.append((part, _keyframe(part, self.codec)))
        return out

    def close(self):
        lib = self.lib
        for b in self.bufs:
            lib.mpp_buffer_put_with_caller(b, b"Encoder")
        self.bufs, self.ptrs = [], []
        if self.group:
            lib.mpp_buffer_group_put(self.group)
            self.group = ctypes.c_void_p()
        if self.cfg:
            lib.mpp_enc_cfg_deinit(self.cfg)
            self.cfg = ctypes.c_void_p()
        if self.ctx:
            lib.mpp_destroy(self.ctx)
            self.ctx = ctypes.c_void_p()


def fragment(codec):
    """Pipeline text for the encoder's place: NV12 frames into `mppin`, coded video out of
    `mppout` (its caps are set by the Bridge once the first frame shows the size)."""
    parse = "h264parse" if codec == "h264" else "h265parse"
    return (f"appsink name=mppin sync=false async=false max-buffers=4 emit-signals=false "
            f"appsrc name=mppout format=time block=true max-buffers=16 ! {parse}")


class Bridge:
    """Feeds `mppin`'s frames through an Encoder into `mppout` on a thread of its own.

    Create it before the pipeline starts: an appsink hands out frames only in PLAYING, and a
    sink waiting for its first buffer (preroll) would hold the pipeline in PAUSED forever -
    the file sink's data has to come through this thread - so every sink is made async=false."""

    def __init__(self, pipe, codec, fps, gop=None, qp=None, bps=None, rc="vbr"):
        from gi.repository import Gst  # Gst.init() is the caller's
        it = pipe.iterate_sinks()
        while True:
            ok, sink = it.next()
            if ok != Gst.IteratorResult.OK:
                break
            sink.set_property("async", False)
        self.pipe, self.codec = pipe, codec
        self.opts = dict(fps=fps, gop=gop, qp=qp, bps=bps, rc=rc)
        self.sink = pipe.get_by_name("mppin")
        self.src = pipe.get_by_name("mppout")
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, name="mpp-encoder", daemon=True)
        self.frames = 0

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_flag.set()
        self.thread.join(timeout=5)

    def _fail(self, text):
        from gi.repository import GLib, Gst
        err = GLib.Error.new_literal(Gst.core_error_quark(), text, Gst.CoreError.FAILED)
        self.src.post_message(Gst.Message.new_error(self.src, err, text))

    def _run(self):
        from gi.repository import Gst, GstVideo
        from .gstutil import gst_buffer
        enc = None
        try:
            while not self.stop_flag.is_set():
                sample = self.sink.emit("try-pull-sample", 200 * Gst.MSECOND)
                if sample is None:
                    if self.sink.get_property("eos"):
                        break
                    continue
                buf = sample.get_buffer()
                if enc is None:
                    vi = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
                    strides, offsets = list(vi.stride)[:2], list(vi.offset)[:2]
                    enc = Encoder(self.codec, vi.width, vi.height, **self.opts)
                    fmt = "video/x-h264" if self.codec == "h264" else "video/x-h265"
                    self.src.set_property("caps", Gst.Caps.from_string(
                        f"{fmt},stream-format=byte-stream,alignment=au,width={vi.width},height={vi.height},"
                        f"framerate={vi.fps_n}/{vi.fps_d}"))
                    log.info("VPU %s encoder %dx%d via librockchip_mpp", self.codec, vi.width, vi.height)
                for au, key in enc.encode(buf.extract_dup(0, buf.get_size()), strides, offsets):
                    out = gst_buffer(au)
                    out.pts = out.dts = buf.pts
                    out.duration = buf.duration
                    if not key:
                        out.set_flags(Gst.BufferFlags.DELTA_UNIT)
                    if self.src.emit("push-buffer", out) != Gst.FlowReturn.OK:
                        return
                self.frames += 1
        except Exception as e:  # noqa: BLE001 - any failure must fail the job, not hang it
            log.exception("VPU encoder")
            self._fail(str(e))
            return
        finally:
            if enc is not None:
                enc.close()
        self.src.emit("end-of-stream")
