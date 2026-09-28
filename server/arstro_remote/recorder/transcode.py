"""Worker entry point for one background conversion (spawned by JobManager).

    python3 -m arstro_remote.recorder.transcode INPUT --codec h264-vpu [...]

Prints one JSON progress object per line on stdout (see jobs.py).
"""
import argparse
import os
import sys


def main(argv=None):
    os.environ.setdefault("GST_MPP_NO_RGA", "0")
    from . import jobs
    ap = argparse.ArgumentParser(prog="arstro_remote.recorder.transcode")
    jobs.add_transcode_args(ap)
    return jobs.transcode(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
