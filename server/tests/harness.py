"""Tiny test harness shared by the server test suites.

    @test("REC-03", "ARC-03")
    def recording_is_synced(ctx): ...

Each test names the requirements (docs/requirements.md) it proves; the summary prints
PASS/FAIL per test and the requirement IDs covered, so `scripts/req_coverage.py` and the
test skill can map results back to requirements.
"""

import argparse
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

TESTS = []


def test(*reqs):
    def deco(fn):
        fn.reqs = reqs
        TESTS.append(fn)
        return fn
    return deco


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def wait_until(pred, timeout=10, interval=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return None


def nal_types(annexb):
    """NAL unit types in an H.264 Annex-B access unit."""
    return [annexb[m.end()] & 0x1F for m in re.finditer(b"\x00\x00\x01", annexb) if m.end() < len(annexb)]


def run(setup=None, teardown=None, description=""):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("-k", help="only tests whose name contains this")
    ap.add_argument("--list", action="store_true", help="list tests and their requirements")
    args, _ = ap.parse_known_args()
    tests = [t for t in TESTS if not args.k or args.k in t.__name__]
    if args.list:
        for t in tests:
            print("%-44s %s" % (t.__name__, " ".join(t.reqs)))
        return 0
    ctx = setup() if setup else None
    passed = failed = 0
    covered = {}
    try:
        for t in tests:
            t0 = time.time()
            try:
                t(ctx) if t.__code__.co_argcount else t()
                passed += 1
                ok = True
                print("PASS  %-44s %5.1fs  %s" % (t.__name__, time.time() - t0, " ".join(t.reqs)), flush=True)
            except Exception as e:
                failed += 1
                ok = False
                print("FAIL  %-44s %s" % (t.__name__, e), flush=True)
                traceback.print_exc(limit=4)
            for r in t.reqs:
                covered.setdefault(r, []).append(ok)
    finally:
        if teardown:
            try:
                teardown(ctx)
            except Exception:
                traceback.print_exc()
    reqs = " ".join("%s%s" % (r, "" if all(v) else "(FAIL)") for r, v in sorted(covered.items()))
    print("\nrequirements: %s" % reqs)
    print("%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0
