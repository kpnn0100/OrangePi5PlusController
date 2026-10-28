#!/usr/bin/env python3
"""FFV1 on several encoders at once, joined in frame order (GAL-04) - needs no server.

    python3 tests/test_ffv1lanes.py [-k NAME]

Needs an FFmpeg: the GPU-encoding one when setup_gpu.sh was run (a GPU lane then works next to
the CPU lanes), else the system's; without any, every test passes as "skipped". A small test
picture is cut into frames, handed out to lanes by ffv1lanes.Join, the joined stream muxed and
decoded again: every frame must come back identical and in order - also when a lane dies at
once, dies half way, hangs, or every lane is lost at the end (a fresh lane takes over).
"""

import os
import shutil
import subprocess
import tempfile
import threading

from harness import check, run, test

from arstro_remote.recorder import ffv1lanes, gpu

W, H, N = 640, 480, 80    # the GPU encoder takes no smaller picture; more frames than MAX_SPAN
V = {"width": W, "height": H, "fps_n": 30000, "fps_d": 1001}
FS = W * H * 2                           # yuv422p


class Ctx:
    pass


def setup():
    c = Ctx()
    c.dir = tempfile.mkdtemp(prefix="lanes-test-")
    g = gpu.ffmpeg()
    c.exe, c.env, c.gpu = (g[0], g[1], True) if g else (shutil.which("ffmpeg"), dict(os.environ), False)
    if c.exe:
        c.raw = os.path.join(c.dir, "frames.yuv")
        subprocess.run([c.exe, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30000/1001",
                        "-frames:v", str(N), "-pix_fmt", "yuv422p", "-f", "rawvideo", "-y", c.raw], check=True)
    return c


def teardown(c):
    shutil.rmtree(c.dir, ignore_errors=True)


def skip(c):
    if not c.exe:
        print("    skipped: no FFmpeg on this machine")
        return True
    return False


def lane(c, kind="cpu", extra=(), cmd=None):
    base = ffv1lanes.lane_cmd(kind, c.exe, V, "yuv422p", "yuv422p", 2)
    return ffv1lanes.Lane(kind, cmd or base[:-3] + list(extra) + base[-3:], c.env)


def join(c, lanes, frames=None, stall_s=ffv1lanes.STALL_S):
    """Runs the lanes over the frames: (the joined file decoded, the Join)."""
    spans = [(i * FS, FS) for i in range(N)]
    stop = threading.Event()
    guard = threading.Timer(60, stop.set)        # a hang fails the test instead of blocking it
    guard.start()
    j = ffv1lanes.Join(lanes, frames(spans, lanes) if frames else iter(spans), c.raw, stop,
                       lambda kind: lane(c, "cpu"), stall_s)
    j.start()
    try:
        check(j.ref_ready.wait(30), "no lane wrote its header: %s" % j.error)
        tick = V["fps_d"] * 1e9 / V["fps_n"] / j.ref.scale
        body = bytearray(ffv1lanes.stream_header(j.ref))
        for n, packet in j.packets():
            body += ffv1lanes.cluster(round(n * tick), packet)
    finally:
        guard.cancel()
    out = os.path.join(c.dir, "joined.mkv")
    p = subprocess.run([c.exe, "-v", "error", "-y", "-f", "matroska", "-i", "pipe:0", "-c:v", "copy", out],
                       input=bytes(body), capture_output=True)
    check(p.returncode == 0, "the joined stream does not mux: %s" % p.stderr.decode()[-300:])
    dec = subprocess.run([c.exe, "-v", "error", "-i", out, "-f", "rawvideo", "-pix_fmt", "yuv422p", "-"],
                         capture_output=True).stdout
    for x in j.lanes:
        x.proc.wait(10)
    return dec, j


def identical(c, dec):
    src = open(c.raw, "rb").read()
    check(len(dec) == len(src), "%d frames back of %d" % (len(dec) // FS, N))
    bad = next((i for i in range(N) if dec[i * FS:(i + 1) * FS] != src[i * FS:(i + 1) * FS]), None)
    check(bad is None, "frame %s is not the frame given" % bad)


@test("GAL-04", "GAL-08")
def frames_from_several_encoders_join_losslessly(c):
    if skip(c):
        return
    lanes = [lane(c), lane(c)] + ([lane(c, "gpu")] if c.gpu else [])
    dec, j = join(c, lanes)
    identical(c, dec)
    for x in lanes:      # a GPU lane only stays when its FFV1 header is the CPU lanes' own
        check(not x.dead and x.made > 0, "%s lane: %s frames, %s" % (x.kind, x.made, x.dead))


@test("GAL-04", "GAL-10")
def a_lane_that_dies_gives_its_frames_back(c):
    if skip(c):
        return
    lanes = [lane(c), lane(c, extra=["-frames:v", "6"]), lane(c, "gpu", cmd=["false"])]
    dec, j = join(c, lanes)
    identical(c, dec)
    check(not lanes[0].dead, "the working lane stopped: %s" % lanes[0].dead)
    check(lanes[1].dead and lanes[1].made == 6, "the lane that stopped after 6 frames: %s, %s"
          % (lanes[1].made, lanes[1].dead))
    check(lanes[2].dead and not lanes[2].made, "the lane that never started: %s" % lanes[2].dead)


@test("GAL-04", "GAL-10")
def a_stuck_lane_is_replaced(c):
    if skip(c):
        return
    # takes frames, never gives any back, keeps its output open (the shell holds it)
    lanes = [lane(c), lane(c, "gpu", cmd=["sh", "-c", "cat > /dev/null; sleep 600"])]
    dec, j = join(c, lanes, stall_s=2)
    identical(c, dec)
    check(lanes[1].dead and "no frame back" in lanes[1].dead, "the stuck lane: %s" % lanes[1].dead)


@test("GAL-04", "GAL-10")
def every_lane_lost_at_the_end_a_fresh_one_finishes(c):
    if skip(c):
        return

    def frames(spans, lanes):
        yield from spans
        for x in list(lanes):            # every lane dies holding its last frames
            x.proc.kill()
    lanes = [lane(c), lane(c)]
    dec, j = join(c, lanes, frames)
    identical(c, dec)
    check(j.rescues >= 1 and len(j.lanes) > 2, "no fresh lane took over: %d lanes" % len(j.lanes))


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, __doc__))
