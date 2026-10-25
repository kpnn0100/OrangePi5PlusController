#!/usr/bin/env python3
"""Interrupted RAW recordings and conversions that always finish (GAL-09, GAL-10, GAL-08).

Self-contained: small synthetic .arh files in a temp folder (no server, no HDMI), with the
damage a power loss leaves - a zero-filled tail, a damaged stretch in the middle - and
capture drops. The conversions run the real worker (FFV1 on the CPU) and the real check.

    python3 server/tests/test_arh_recovery.py [-k NAME]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from harness import check, run, test

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from arstro_remote.recorder import arh, jobs as jobs_mod, verify  # noqa: E402

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H, FPS = 64, 36, 30
FRAME = W * H * 3 // 2
AUDIO = 48000 * 4 // FPS                   # S16LE stereo per frame


class Ctx:
    pass


def setup():
    c = Ctx()
    c.dir = tempfile.mkdtemp(prefix="arh-recovery-")
    return c


def teardown(c):
    shutil.rmtree(c.dir, ignore_errors=True)


def frame(i):
    """A frame whose bytes depend on its number (so a repeated / missing frame is visible)."""
    row = bytes((i * 7 + x) & 0xFF for x in range(W))
    return (row * (H * 3 // 2))[:FRAME]


def make(c, name, frames=60, drops=(), finalize=False, zero_tail=0):
    """Write a recording like the capture does: VFRM + AUDS chunks. drops: frame numbers the
    capture skipped (a gap in seq). zero_tail: bytes of zeros after the last chunk, as a power
    loss leaves them."""
    path = os.path.join(c.dir, name)
    meta = {"format": "ARH", "version": 1, "video": {
        "format": "NV12", "width": W, "height": H, "fps_n": FPS, "fps_d": 1, "frame_size": FRAME,
        "plane_offsets": [0, W * H], "plane_strides": [W, W], "colorimetry": "bt709"},
        "audio": {"format": "S16LE", "rate": 48000, "channels": 2}}
    w = arh.ArhWriter(path, meta)
    with open(path, "wb") as f:
        f.write(w.header())
        for i in range(frames):
            if i in drops:
                continue
            pts = i * 10**9 // FPS
            f.write(w.video_chunk(FRAME, pts, i) + frame(i))
            a = bytes(((i + k) & 0xFF) for k in range(AUDIO))
            f.write(w.audio_chunk(AUDIO, pts, i * AUDIO // 4) + a)
        f.write(b"\0" * zero_tail)
    if finalize:
        w.finalize()
    old = time.time() - 600                    # "nobody has written it for 10 minutes"
    os.utime(path, (old, old))
    return path


def damage_middle(path, frame_no, length=FRAME // 2):
    """Zero a stretch starting at the header of video frame frame_no."""
    idx, _ = arh.chunk_index(path)
    off = idx[frame_no][0] - arh.CHUNK.size
    with open(path, "r+b") as f:
        f.seek(off)
        f.write(b"\0" * length)
    old = time.time() - 600
    os.utime(path, (old, old))


def convert(path, *extra):
    out = subprocess.run([sys.executable, "-m", "arstro_remote.recorder.transcode", path, "--codec", "ffv1",
                          "--nice", "0"] + list(extra), cwd=SERVER, capture_output=True, text=True, timeout=120)
    lines = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    return out.returncode, lines, out.stderr


# ----------------------------------------------------------------------- GAL-09
@test("GAL-09")
def zero_tail_is_read_around_and_repaired(c):
    p = make(c, "REC_tail.arh", frames=40, zero_tail=3 * FRAME + 100)
    size = os.path.getsize(p)
    r = arh.ArhReader(p)
    n = sum(1 for _ in r.chunks((arh.VFRM,)))
    check(n == 40, "read %d of 40 frames past a zero tail" % n)
    check(r.damaged and max(b for _a, b in r.damaged) == size, "the zero tail is not reported: %s" % r.damaged)
    check(arh.needs_repair(p), "an old unfinalized file nobody writes should need repair")
    note = arh.repair_if_needed(p)
    check(note and note["frames"] == 40 and note["dropped_bytes"] == 3 * FRAME + 100, note)
    r = arh.ArhReader(p)
    check(r.finalized and r.repaired and r.frame_count == 40, "header after repair: %s" % r.repaired)
    check(arh.repair(p) is None and not arh.needs_repair(p), "a repaired file is repaired again")
    check(sum(1 for _ in r.chunks((arh.VFRM,))) == 40 and not r.damaged, "repaired file reads with damage")


@test("GAL-09")
def a_file_being_written_is_never_repaired(c):
    p = make(c, "REC_busy.arh", frames=5, zero_tail=10)
    with open(p, "r+b"):
        check(arh.in_use(p) and not arh.needs_repair(p), "repair would cut a file that is still written")
    fresh = make(c, "REC_fresh.arh", frames=5)
    os.utime(fresh, None)                      # written just now: maybe still recording
    check(not arh.needs_repair(fresh), "a file written seconds ago looks interrupted")
    with arh.file_lock(p) as mine:
        check(mine, "lock not taken")
        check(arh.repair_if_needed(p) is None, "repaired while another process held the file's lock")


@test("GAL-09")
def damage_in_the_middle_is_skipped(c):
    p = make(c, "REC_mid.arh", frames=50)
    damage_middle(p, 20)
    r = arh.ArhReader(p)
    seqs = [seq for _t, _pts, seq, _d in r.chunks((arh.VFRM,))]
    check(20 not in seqs and seqs[-1] == 49 and len(seqs) == 49, "frames after the damage: %s" % seqs[18:24])
    check(len(r.damaged) == 1, r.damaged)
    cfr = list(arh.cfr_frames(arh.ArhReader(p)))
    check(len(cfr) == 50 and cfr[20][3] and cfr[20][2] == frame(19), "the lost frame is not the previous one repeated")


@test("GAL-09")
def resync_ignores_chunk_names_inside_pixels(c):
    p = make(c, "REC_fake.arh", frames=30)
    idx, _ = arh.chunk_index(p)
    off = idx[10][0] - arh.CHUNK.size
    with open(p, "r+b") as f:                 # damage + a lone fake VFRM header inside it
        f.seek(off)
        f.write(b"\0" * 64 + arh.pack_chunk(arh.VFRM, FRAME, 1, 1) + b"\0" * 64)
    r = arh.ArhReader(p)
    seqs = [seq for _t, _pts, seq, _d in r.chunks((arh.VFRM,))]
    check(1 not in seqs[10:] and seqs[-1] == 29, "resynced onto a fake header: %s" % seqs)


# ------------------------------------------------------------- GAL-09 + GAL-08
@test("GAL-09", "GAL-08", "GAL-04")
def interrupted_recording_converts_and_verifies(c):
    p = make(c, "REC_conv.arh", frames=45, drops=(7, 8), zero_tail=2 * FRAME)
    code, lines, err = convert(p)
    check(code == 0 and lines[-1]["state"] == "done", "conversion failed: %s %s" % (lines[-1:], err[-300:]))
    check(any(l.get("repaired") for l in lines), "the cut-off RAW was not repaired first")
    out = jobs_mod.output_path(p, "ffv1")
    res = verify.verify(p, out)
    # 45 frames, two dropped by the capture = two repeats in the copy, checked as such
    check(res["ok"] and res["frames"] == 45, "lossless check: %s" % res)


@test("GAL-08", "GAL-09")
def damaged_middle_converts_and_verifies(c):
    p = make(c, "REC_conv2.arh", frames=40, finalize=True)
    damage_middle(p, 15)
    code, lines, err = convert(p)
    check(code == 0, "conversion failed: %s %s" % (lines[-1:], err[-300:]))
    check((lines[-1].get("damaged") or {}).get("bytes", 0) > 0, "skipped damage not reported: %s" % lines[-1])
    res = verify.verify(p, jobs_mod.output_path(p, "ffv1"))
    check(res["ok"] and res["frames"] == 40, res)


# ----------------------------------------------------------------------- GAL-10
def drive(mgr, pred, timeout=60):
    ctx = GLib.MainContext.default()
    t = time.monotonic()
    while not pred() and time.monotonic() - t < timeout:
        ctx.iteration(False)
        time.sleep(0.01)
    return pred()


@test("GAL-10")
def output_lock_keeps_two_jobs_apart(c):
    p = make(c, "REC_lock.arh", frames=10, finalize=True)
    with arh.file_lock(jobs_mod.output_path(p, "ffv1")):
        code, lines, _err = convert(p)
    check(code == jobs_mod.EXIT_LOCKED and "another job" in lines[-1].get("error", ""), lines[-1:])
    check(not os.path.exists(jobs_mod.output_path(p, "ffv1") + ".part"), "the locked-out job left a .part")


@test("GAL-10")
def failed_job_is_retried_then_given_up(c):
    bad = os.path.join(c.dir, "REC_bad.arh")
    with open(bad, "wb") as f:
        f.write(b"NOPE" * 2000)
    old = jobs_mod.RETRY_DELAY
    jobs_mod.RETRY_DELAY = 0
    try:
        seen = []
        mgr = jobs_mod.JobManager(lambda j: j and seen.append((j.state, j.attempt)), store=os.path.join(c.dir, "j1.json"))
        job = mgr.add(bad, "ffv1", {})
        check(drive(mgr, lambda: job.finished), "job never finished: %s" % job.describe())
    finally:
        jobs_mod.RETRY_DELAY = old
    check(job.state == "failed" and job.attempt == jobs_mod.MAX_ATTEMPTS, job.describe())
    check(("retrying", 2) in seen and "gave up" in (job.note or ""), "retries: %s / %s" % (seen, job.note))
    check(json.load(open(os.path.join(c.dir, "j1.json"))) == [], "finished job still in the store")


@test("GAL-10")
def unfinished_jobs_resume_after_a_restart(c):
    p = make(c, "REC_resume.arh", frames=20, zero_tail=FRAME)
    out = jobs_mod.output_path(p, "ffv1")
    open(out + ".part", "wb").write(b"half a file")     # what the power loss left
    store = os.path.join(c.dir, "j2.json")
    with open(store, "w") as f:
        json.dump([{"src": p, "codec": "ffv1", "opts": {"verify": True}, "output": out, "attempt": 1}], f)
    removed = jobs_mod.clean_stale_parts(c.dir)
    check(os.path.basename(out) + ".part" in removed, "stale .part kept: %s" % removed)
    mgr = jobs_mod.JobManager(lambda j: None, store=store)
    check(mgr.resume() == 1, "nothing resumed")
    job = mgr.jobs[0]
    check(job.attempt == 2 and "resumed" in job.note, job.describe())
    check(drive(mgr, lambda: job.finished, 120), "resumed job never finished: %s" % job.describe())
    check(job.state == "done" and (job.verify or {}).get("ok"), job.describe())
    check("repaired" in (job.note or ""), "note: %s" % job.note)


if __name__ == "__main__":
    raise SystemExit(run(setup, teardown, __doc__))
