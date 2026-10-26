#!/usr/bin/env python3
"""The streamed ZIP behind "download many at once" (GAL-12) - self-contained, no server needed.

    python3 tests/test_zipstream.py [-k NAME]

The archive's length is computed before the first byte (it becomes the HTTP Content-Length),
so the tests hold the writer to that number exactly, read every archive back with Python's
zipfile and - when installed - Info-ZIP's `unzip -t`, and walk the ZIP64 paths with small files
by lowering the 32-bit limit the layout switches at.
"""

import io
import os
import shutil
import subprocess
import tempfile
import zipfile

from harness import check, run, test

from arstro_remote.zipstream import ChangedError, ZipStream


def make_files(d):
    files = []
    for name, data in (("empty.mkv", b""), ("note.txt", "größe – ünïcode\n".encode()),
                       ("rec_20260930_101500_H264.mp4", os.urandom(3 << 20) + b"tail")):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data)
        files.append(p)
    return files


def build(files, **kw):
    z = ZipStream([("2026-09-30/" + os.path.basename(p), p) for p in files], **kw)
    out = io.BytesIO()
    n = z.write(out)
    return z, n, out.getvalue()


def read_back(blob, files, tag):
    zf = zipfile.ZipFile(io.BytesIO(blob))
    check(zf.testzip() is None, "%s: a CRC does not match" % tag)
    names = zf.namelist()
    check(names == ["2026-09-30/" + os.path.basename(p) for p in files], "%s: names %s" % (tag, names))
    for p in files:
        with open(p, "rb") as f:
            check(zf.read("2026-09-30/" + os.path.basename(p)) == f.read(), "%s: %s differs" % (tag, p))
        info = zf.getinfo("2026-09-30/" + os.path.basename(p))
        check(info.compress_type == zipfile.ZIP_STORED, "%s: stored, never recompressed" % tag)


def unzip_test(blob, d, tag):
    if not shutil.which("unzip"):
        return
    p = os.path.join(d, "t.zip")
    with open(p, "wb") as f:
        f.write(blob)
    r = subprocess.run(["unzip", "-t", p], capture_output=True, text=True)
    check(r.returncode == 0 and "No errors detected" in r.stdout, "%s: unzip -t: %s %s" % (tag, r.stdout[-300:], r.stderr))


@test("GAL-12")
def the_length_is_known_before_the_first_byte(_t):
    with tempfile.TemporaryDirectory() as d:
        files = make_files(d)
        z, n, blob = build(files)
        check(n == len(blob) == z.length, "announced %d, wrote %d" % (z.length, len(blob)))
        check(not z.zip64_end and not any(e.big for e in z.entries), "a small archive stays a plain ZIP")
        read_back(blob, files, "plain")
        unzip_test(blob, d, "plain")


@test("GAL-12")
def zip64_where_a_value_passes_32_bits(_t):
    # With the switch-over lowered to 1000 bytes the 3 MB file is a "big" entry, every later
    # offset is past the limit and the central directory needs the ZIP64 end record - the same
    # code a 6 GB RAW and a 40 GB month take.
    with tempfile.TemporaryDirectory() as d:
        files = make_files(d)
        z, n, blob = build(files, limit=1000)
        check(z.zip64_end and any(e.big for e in z.entries), "the ZIP64 paths were not taken")
        check(n == len(blob) == z.length, "announced %d, wrote %d" % (z.length, len(blob)))
        read_back(blob, files, "zip64")
        unzip_test(blob, d, "zip64")
        z, n, blob = build(files, force_zip64_end=True)
        check(n == z.length, "forced end record")
        read_back(blob, files, "zip64-end")


@test("GAL-12")
def a_file_that_changes_is_caught(_t):
    with tempfile.TemporaryDirectory() as d:
        files = make_files(d)
        z = ZipStream([(os.path.basename(p), p) for p in files])
        with open(files[1], "ab") as f:                  # it grew after the layout
            f.write(b"more")
        try:
            z.write(io.BytesIO())
            raise AssertionError("a changed file was not noticed")
        except ChangedError as e:
            check("grew" in str(e), str(e))
        try:
            ZipStream([("a", files[0]), ("a", files[1])])
            raise AssertionError("a name twice was accepted")
        except ValueError:
            pass


if __name__ == "__main__":
    raise SystemExit(run(description=__doc__))
