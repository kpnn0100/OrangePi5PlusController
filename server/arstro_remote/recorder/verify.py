"""Bit-exact check of an FFV1 copy against its RAW .arh source (GAL-08).

    python3 -m arstro_remote.recorder.verify TAKE.arh TAKE_FFV1.mkv

FFV1 is lossless by design, so this does not test the codec: it proves the *whole copy*
before the RAW may be deleted - no chroma or colour conversion on the way, no missing or
duplicated frame, nothing truncated, and every audio sample. The FFV1 video is decoded in
its own (native) pixel format, the RAW frames are rearranged into the same planes
(NV16 -> Y + U + V ...), and every plane of every frame is compared byte for byte; the
audio is decoded to the RAW's sample format and compared the same way.

Prints JSON lines like the transcode worker: {"state": "verifying", "frames", "total"}
then {"state": "done", "verify": {"ok", "frames", "audio_bytes", "detail"}}.
"""

import json
import subprocess
import sys

from . import arh

# native FFV1 decode formats for each HDMI RX format (what our encoders store)
NATIVE = {"NV12": ("yuv420p",), "NV21": ("yuv420p",), "NV16": ("yuv422p",), "NV61": ("yuv422p",),
          "YUY2": ("yuv422p",), "UYVY": ("yuv422p",), "NV24": ("yuv444p",),
          "BGR": ("bgr0", "gbrp"), "RGB": ("bgr0", "gbrp")}


class Mismatch(Exception):
    pass


def _rows(data, offset, stride, row_bytes, rows):
    if stride == row_bytes:
        return data[offset:offset + row_bytes * rows]
    return b"".join(data[offset + r * stride:offset + r * stride + row_bytes] for r in range(rows))


def raw_planes(fmt, data, v):
    """The RAW frame as named planes (packed, no stride padding)."""
    w, h = v["width"], v["height"]
    offs = v.get("plane_offsets") or [0, w * h]
    strides = v.get("plane_strides") or [w, w]
    if fmt in ("NV12", "NV21", "NV16", "NV61", "NV24"):
        y = _rows(data, offs[0], strides[0], w, h)
        ch = h // 2 if fmt in ("NV12", "NV21") else h
        cw = 2 * w if fmt == "NV24" else w
        uv = _rows(data, offs[1], strides[1], cw, ch)
        u, vv = (uv[0::2], uv[1::2]) if fmt in ("NV12", "NV16", "NV24") else (uv[1::2], uv[0::2])
        return {"Y": y, "U": u, "V": vv}
    if fmt in ("YUY2", "UYVY"):
        p = _rows(data, offs[0], strides[0], 2 * w, h)
        if fmt == "YUY2":
            return {"Y": p[0::2], "U": p[1::4], "V": p[3::4]}
        return {"Y": p[1::2], "U": p[0::4], "V": p[2::4]}
    if fmt in ("BGR", "RGB"):
        p = _rows(data, offs[0], strides[0], 3 * w, h)
        b, g, r = (p[0::3], p[1::3], p[2::3]) if fmt == "BGR" else (p[2::3], p[1::3], p[0::3])
        return {"R": r, "G": g, "B": b}
    raise Mismatch("no bit-exact check for %s frames" % fmt)


def decoded_planes(pix, data, w, h):
    if pix in ("yuv420p", "yuv422p", "yuv444p"):
        cw, ch = {"yuv420p": (w // 2, h // 2), "yuv422p": (w // 2, h), "yuv444p": (w, h)}[pix]
        ys, cs = w * h, cw * ch
        return {"Y": data[:ys], "U": data[ys:ys + cs], "V": data[ys + cs:ys + 2 * cs]}
    if pix == "bgr0":
        return {"B": data[0::4], "G": data[1::4], "R": data[2::4]}
    if pix == "gbrp":
        n = w * h
        return {"G": data[:n], "B": data[n:2 * n], "R": data[2 * n:3 * n]}
    raise Mismatch("unexpected FFV1 pixel format %s" % pix)


def frame_bytes(pix, w, h):
    return {"yuv420p": w * h * 3 // 2, "yuv422p": w * h * 2, "yuv444p": w * h * 3,
            "bgr0": w * h * 4, "gbrp": w * h * 3}[pix]


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,pix_fmt,width,height,sample_fmt,sample_rate,channels",
                          "-of", "json", path], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise Mismatch("the FFV1 file can't be read: %s" % out.stderr.strip()[:200])
    streams = json.loads(out.stdout or "{}").get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return video, audio


def _first_diff(a, b):
    n = min(len(a), len(b))
    lo, hi = 0, n
    while hi - lo > 4096:                 # binary search on slices (C-speed compares)
        mid = (lo + hi) // 2
        if a[lo:mid] != b[lo:mid]:
            hi = mid
        else:
            lo = mid
    for i in range(lo, hi):
        if a[i] != b[i]:
            return i
    return n


def verify(arh_path, mkv_path, progress=None):
    """Return {"ok", "frames", "audio_bytes", "detail"}; never raises for a mismatch."""
    try:
        reader = arh.ArhReader(arh_path)
        v, a = reader.video, reader.audio
        fmt, w, h = v["format"], v["width"], v["height"]
        video, audio = probe(mkv_path)
        if not video or video.get("codec_name") != "ffv1":
            raise Mismatch("%s has no FFV1 video" % mkv_path)
        pix = video.get("pix_fmt")
        if pix not in NATIVE.get(fmt, ()):
            raise Mismatch("the copy is %s but the RAW is %s: not a lossless copy of this source" % (pix, fmt))
        if (video.get("width"), video.get("height")) != (w, h):
            raise Mismatch("size differs: copy %sx%s, RAW %dx%d" % (video.get("width"), video.get("height"), w, h))
        total = reader.estimate_frames()
        n = _check_video(reader, mkv_path, fmt, pix, v, total, progress)
        audio_bytes = 0
        if a:
            if not audio:
                raise Mismatch("the RAW has audio but the copy has none")
            audio_bytes = _check_audio(reader, mkv_path, a)
        elif audio:
            raise Mismatch("the copy has audio the RAW does not have")
        return {"ok": True, "frames": n, "audio_bytes": audio_bytes,
                "detail": "%d frames%s identical" % (n, " and %d audio bytes" % audio_bytes if audio_bytes else "")}
    except Mismatch as e:
        return {"ok": False, "frames": 0, "audio_bytes": 0, "detail": str(e)}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"ok": False, "frames": 0, "audio_bytes": 0, "detail": "check failed: %s" % e}


def _check_video(reader, mkv_path, fmt, pix, v, total, progress):
    w, h = v["width"], v["height"]
    size = frame_bytes(pix, w, h)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "0", "-i", mkv_path, "-map", "0:v:0",
           "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=size * 2)
    n = 0
    try:
        for _t, _pts, _seq, data in reader.chunks((arh.VFRM,), False):
            got = proc.stdout.read(size)
            if len(got) < size:
                raise Mismatch("the copy ends after %d frames, the RAW has more" % n)
            want = raw_planes(fmt, data, v)
            have = decoded_planes(pix, got, w, h)
            for name, plane in want.items():
                if have[name] != plane:
                    i = _first_diff(plane, have[name])
                    cw = w // 2 if name in ("U", "V") and fmt not in ("NV24",) else w
                    raise Mismatch("frame %d differs (plane %s, pixel %d, %d)" % (n, name, i % cw, i // cw))
            n += 1
            if progress and n % 10 == 0:
                progress(n, max(total, n))
        extra = proc.stdout.read(1)
        if extra:
            raise Mismatch("the copy has more frames than the RAW (%d)" % n)
        if proc.wait() != 0:
            raise Mismatch("decoding the copy failed: %s" % proc.stderr.read().decode(errors="replace")[:200])
        if n == 0:
            raise Mismatch("the RAW has no frames")
        return n
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _check_audio(reader, mkv_path, a):
    fmt = {"S16LE": "s16le", "S32LE": "s32le", "F32LE": "f32le"}.get(a.get("format", "S16LE"))
    if not fmt:
        raise Mismatch("no bit-exact check for %s audio" % a.get("format"))
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", mkv_path, "-map", "0:a:0", "-f", fmt,
           "-ac", str(a["channels"]), "-ar", str(a["rate"]), "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    total = 0
    try:
        for _t, _pts, _seq, data in reader.chunks((arh.AUDS,), False):
            got = proc.stdout.read(len(data))
            if got != data:
                if len(got) < len(data):
                    raise Mismatch("the copy's audio ends early (after %d bytes)" % (total + len(got)))
                i = _first_diff(data, got)
                raise Mismatch("audio differs at byte %d" % (total + i))
            total += len(data)
        if proc.stdout.read(1):
            raise Mismatch("the copy has more audio than the RAW (%d bytes)" % total)
        proc.wait()
        return total
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    def progress(n, total):
        print(json.dumps({"state": "verifying", "frames": n, "total": total}), flush=True)
    result = verify(argv[0], argv[1], progress)
    print(json.dumps({"state": "done", "verify": result}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
