"""`convert`: turn a clip into another format from the command line, with a progress bar.

    python3 hdmi_recorder.py convert                         # pick clip and target from menus
    python3 hdmi_recorder.py convert --list                  # numbered list of recordings
    python3 hdmi_recorder.py convert 3 --to ffv1             # recording #3 of that list
    python3 hdmi_recorder.py convert REC_20260927_225252.arh --to h265
    python3 hdmi_recorder.py convert a.arh b.arh --to h265-x265 --quality max --preset medium

Clips can be RAW (.arh) or encoded (.mp4/.mov/.mkv, e.g. FFV1 -> H.265 to share it).
Targets (for H.265, --bitrate MBPS sets a target bitrate instead of constant quality):
    h265        H.265 .mov on the VPU: about real time, high quality (fixed QP)
    h265-x265   H.265 .mov with x265 on the CPU: best quality per MB, very slow at 4K
    ffv1        FFV1 .mkv: lossless, 25-50% of the RAW size (CPU, ~14 fps at 4K)
    ffv1-gpu    the same FFV1 on the GPU (Vulkan): ~5 fps at 4K but leaves the CPU free
                (needs ./setup_gpu.sh once)
The conversion runs in the same worker as the app's background jobs. Ctrl+C cancels
(the unfinished output is deleted).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import gpu, jobs, library, settings

TARGETS = {
    "h265": ("h265-vpu", "H.265 (VPU, fast)"),
    "h265-x265": ("h265-x265", "H.265 (x265, best, slow)"),
    "ffv1": ("ffv1", "FFV1 (lossless)"),
    "ffv1-gpu": ("ffv1-gpu", "FFV1 on the GPU (lossless, ~0.2 CPU cores)"),
}


def add_convert_args(sp):
    sp.add_argument("clips", nargs="*",
                    help="clip paths, names in the recordings folder, or numbers from --list")
    sp.add_argument("--to", choices=list(TARGETS), help="target format (asked if omitted)")
    sp.add_argument("--list", action="store_true", help="list recordings and exit")
    sp.add_argument("--bitrate", type=float, metavar="MBPS",
                    help="H.265: target bitrate in Mbit/s (e.g. 20, 50, 100) instead of "
                         "constant quality")
    sp.add_argument("--rc", choices=["vbr", "cbr"], default="vbr",
                    help="with --bitrate on the VPU: variable (default, peaks up to 1.25x) "
                         "or constant bitrate")
    sp.add_argument("--quality", choices=["high", "higher", "max"],
                    help="H.265 constant quality when no --bitrate (default: the app's setting)")
    sp.add_argument("--preset", choices=jobs.X265_PRESETS, help="x265 speed preset")
    sp.add_argument("--chroma", choices=["420", "source"], help="x265: 4:2:0 or keep source chroma")
    sp.add_argument("--no-audio", action="store_true", help="leave the audio out")
    sp.add_argument("-o", "--output", help="output file (single clip only)")
    sp.add_argument("--force", action="store_true", help="overwrite existing outputs")
    sp.add_argument("--nice", type=int, default=5, help="CPU niceness of the encoder (default 5)")
    sp.add_argument("--folder", help="recordings folder (default: the app's storage setting)")


def _scan(folder):
    done, clips = threading.Event(), []
    library.Library().scan(folder, lambda c: (clips.extend(c), done.set()))
    done.wait(120)
    return [i for c in clips for i in c.items]


def _print_list(items):
    if not items:
        print("No recordings found.")
        return
    for n, i in enumerate(items, 1):
        warn = f"  ! {i.problem}" if i.problem else ""
        print(f"{n:3d}  [{i.kind:5s}] {i.name}  {i.summary()}{warn}")


def _ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def _resolve(clips, items, folder):
    out = []
    for c in clips:
        if c.isdigit() and 1 <= int(c) <= len(items) and not os.path.exists(c):
            out.append(items[int(c) - 1].path)
        elif os.path.exists(c):
            out.append(os.path.abspath(c))
        elif os.path.exists(os.path.join(folder, c)):
            out.append(os.path.join(folder, c))
        else:
            sys.exit(f"Clip not found: {c}  (see: convert --list)")
    return out


def _bar(frac, width=30):
    n = int(frac * width)
    return "█" * n + "░" * (width - n)


def _fmt(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _run_one(main_script, src, codec, args, opts):
    out = args.output or jobs.output_path(src, codec)
    if os.path.exists(out) and not args.force:
        if not sys.stdin.isatty() or _ask(f"{os.path.basename(out)} exists. Overwrite? [y/N] ").lower() != "y":
            print(f"skipped {os.path.basename(src)} (output exists; --force overwrites)")
            return 0
    cmd = [sys.executable, main_script, "transcode", src, "--codec", codec,
           "--quality", opts["quality"], "--preset", opts["preset"], "--chroma", opts["chroma"],
           "--nice", str(args.nice), "-o", out]
    if args.no_audio:
        cmd.append("--no-audio")
    if args.bitrate:
        cmd += ["--bitrate", str(args.bitrate), "--rc", args.rc]
    mode = "ABR" if codec == "h265-x265" else args.rc.upper()
    rate = f", {args.bitrate:g} Mbit/s {mode}" if args.bitrate and not codec.startswith("ffv1") else ""
    print(f"\n{os.path.basename(src)}  →  {os.path.basename(out)}  [{TARGETS[args.to][1]}{rate}]")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    tty = sys.stdout.isatty()
    t0, last, final = time.monotonic(), 0.0, {}
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            final = msg
            if msg.get("state") != "running":
                continue
            frames, total, fps = msg.get("frames", 0), msg.get("total", 0), msg.get("fps", 0)
            frac = min(1.0, frames / total) if total else 0.0
            eta = f"{_fmt((total - frames) / fps)} left" if fps and total > frames else ""
            text = (f"  {_bar(frac)} {frac * 100:5.1f}%  {frames}/{total} frames  "
                    f"{fps:.1f} fps  {eta}")
            if tty:
                cols = shutil.get_terminal_size((100, 20)).columns - 1
                print("\r\033[K" + text[:cols], end="", flush=True)
            elif time.monotonic() - last > 10:
                print(text, flush=True)
                last = time.monotonic()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGTERM)
        print("\n  cancelling…", flush=True)
        for line in proc.stdout:          # read the worker's final message
            try:
                final = json.loads(line)
            except ValueError:
                pass
    code = proc.wait()
    if tty:
        print("\r\033[K", end="")
    state = final.get("state")
    if state == "done":
        size = os.path.getsize(out) if os.path.exists(out) else 0
        took = time.monotonic() - t0
        print(f"  ✓ done in {_fmt(took)}: {out} ({library._size(size)})")
        return 0
    if state == "cancelled":
        print("  cancelled (unfinished output removed)")
        return 130
    err = final.get("error") or proc.stderr.read().strip()[-500:] or f"exit code {code}"
    print(f"  ✗ failed: {err}")
    return 1


def run(args, main_script):
    folder = os.path.expanduser(args.folder) if args.folder else settings.load()["storage"]
    need_list = args.list or not args.clips or any(c.isdigit() for c in args.clips)
    items = _scan(folder) if need_list else []
    if args.list:
        print(f"Recordings in {folder}:")
        _print_list(items)
        return 0
    clips = args.clips
    if not clips:
        if not sys.stdin.isatty():
            sys.exit("No clip given (see: convert --list)")
        print(f"Recordings in {folder}:")
        _print_list(items)
        if not items:
            return 1
        clips = _ask("Clip number(s) to convert (e.g. 1 or 1,3): ").replace(",", " ").split()
        if not clips:
            return 1
    paths = _resolve(clips, items, folder)
    if args.output and len(paths) > 1:
        sys.exit("-o/--output works with a single clip only")
    if not args.to:
        if not sys.stdin.isatty():
            sys.exit("No target given (--to h265 | h265-x265 | ffv1)")
        keys = list(TARGETS)
        for n, k in enumerate(keys, 1):
            print(f"  {n}  {k:10s} {TARGETS[k][1]}")
        choice = _ask("Target number: ")
        if not choice.isdigit() or not 1 <= int(choice) <= len(keys):
            sys.exit("No valid target chosen")
        args.to = keys[int(choice) - 1]
    codec = TARGETS[args.to][0]
    if codec == "ffv1-gpu" and not gpu.available():
        sys.exit("GPU encoding isn't installed yet: run ./setup_gpu.sh once")
    if args.bitrate is not None:
        if codec.startswith("ffv1"):
            sys.exit("--bitrate doesn't apply to FFV1: it is lossless, the size follows the picture")
        if args.bitrate <= 0:
            sys.exit("--bitrate must be a positive number of Mbit/s")
    raw = settings.load()["raw"]
    opts = {"quality": args.quality or raw["hq_quality"], "preset": args.preset or raw["x265_preset"],
            "chroma": args.chroma or raw["hq_chroma"]}
    if codec == "h265-x265":
        print("Note: x265 on the CPU is slow at 4K (hours per minute of footage).")
    worst = 0
    for p in paths:
        if os.path.abspath(p) == os.path.abspath(args.output or jobs.output_path(p, codec)):
            print(f"skipped {p}: it is already the {args.to} copy")
            continue
        worst = max(worst, _run_one(main_script, p, codec, args, opts))
        if worst == 130:
            break
    return worst
