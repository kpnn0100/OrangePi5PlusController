#!/usr/bin/env python3
"""HDMI RX recorder for RK3588 (Orange Pi 5 Plus) — touch-screen camera-style app.

    python3 hdmi_recorder.py                     # live view + recording UI
    python3 hdmi_recorder.py --simulate 3840x2160@60   # try it without a source
    python3 hdmi_recorder.py cli                 # same recorder, no screen (SSH / headless)
    python3 hdmi_recorder.py serve               # web page + downloads on port 8000
    python3 hdmi_recorder.py convert             # convert a clip (menus, progress bar)
    python3 hdmi_recorder.py info CLIP.arh       # describe a raw recording
    python3 hdmi_recorder.py transcode CLIP.arh --codec ffv1   # encode a raw file

Recording modes:
  H.265   real-time hardware (VPU) encode at a chosen bitrate -> .mp4 / .mkv
  RAW     uncompressed frames straight from HDMI RX -> .arh (Arstro Raw HDMI),
          optionally encoded in the background, during and after recording, to
          high-quality H.265 (.mov) and/or lossless FFV1 (.mkv)
"""
import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _fix_blacklisted_mpp():
    """The Rockchip MPP plugin sometimes lands on GStreamer's blacklist (e.g. after a
    registry scan without access to /dev/mpp_service); drop the cache so it rescans."""
    from gi.repository import Gst
    if Gst.ElementFactory.find("mpph265enc"):
        return
    if not glob.glob("/usr/lib/*/gstreamer-1.0/libgstrockchipmpp.so"):
        return
    if os.environ.get("HDMIREC_REGISTRY_RESET"):
        print("[warn] mpph265enc (VPU encoder) unavailable: H.265 recording disabled")
        return
    for f in glob.glob(os.path.expanduser("~/.cache/gstreamer-1.0/registry.*.bin")):
        os.remove(f)
    os.environ["HDMIREC_REGISTRY_RESET"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])


def main():
    ap = argparse.ArgumentParser(description="RK3588 HDMI RX recorder.")
    sub = ap.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="live view + recording UI (default)")
    for p in (ap, run):
        p.add_argument("-d", "--device", help="V4L2 device (default: auto-detect hdmirx)")
        p.add_argument("--edid", choices=["4k60", "4k30", "1080p", "keep"],
                       help="EDID to advertise (default: from settings, 4k60)")
        p.add_argument("--edid-file", help="custom EDID file (hex, as from v4l2-ctl --get-edid)")
        p.add_argument("--sink", default="rkximagesink",
                       choices=["rkximagesink", "xvimagesink", "glimagesink"])
        p.add_argument("--plane-id", type=int, help="DRM plane for the video (default: auto)")
        p.add_argument("--windowed", action="store_true", help="start windowed")
        p.add_argument("--simulate", metavar="WxH@FPS[:FMT[:PATTERN]]",
                       help="use a test pattern instead of HDMI RX, e.g. 3840x2160@60:NV12")

    from hdmirec import cli, jobs
    cl = sub.add_parser("cli", help="command-line recorder (no screen needed)")
    cli.add_cli_args(cl)
    sv = sub.add_parser("serve", help="web server so other computers can download recordings")
    sv.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    sv.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    sv.add_argument("--folder", help="folder to serve (default: the app's storage setting)")
    from hdmirec import convert
    cv = sub.add_parser("convert", help="convert a clip to H.265 / x265 / FFV1 (progress bar)",
                        description=convert.__doc__,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    convert.add_convert_args(cv)
    tr = sub.add_parser("transcode", help="(worker) encode a clip, JSON progress on stdout")
    jobs.add_transcode_args(tr)
    inf = sub.add_parser("info", help="describe an .arh recording")
    inf.add_argument("files", nargs="+")
    rep = sub.add_parser("repair", help="finalize .arh files cut off by a crash or power loss")
    rep.add_argument("files", nargs="+")
    dg = sub.add_parser("diag", help="write a diagnostic report (stop the recorder app first)")
    dg.add_argument("-d", "--device", help="V4L2 device (default: auto-detect hdmirx)")
    dg.add_argument("--no-live", action="store_true", help="skip the live device tests")
    args = ap.parse_args()

    # RGA lets the hardware decoder scale 4K playback to the screen (~130 fps); some
    # images disable it globally (GST_MPP_NO_RGA=1), which makes that scaling fail.
    # Recording is unaffected: the encoder only ever gets NV12 / I420.
    os.environ["GST_MPP_NO_RGA"] = "0"
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    _fix_blacklisted_mpp()

    if args.cmd == "transcode":
        sys.exit(jobs.transcode(args))
    if args.cmd == "cli":
        cli.run(args, os.path.abspath(__file__))
        return
    if args.cmd == "convert":
        sys.exit(convert.run(args, os.path.abspath(__file__)))
    if args.cmd == "serve":
        from hdmirec import server
        server.serve(args.host, args.port, args.folder)
        return
    if args.cmd == "info":
        from hdmirec import arh
        for f in args.files:
            print(arh.info(f), end="\n\n")
        return
    if args.cmd == "repair":
        from hdmirec import arh
        for f in args.files:
            n = arh.repair(f)
            print(f"{f}: " + ("already finalized" if n is None else f"repaired, {n} frames kept"))
        return
    if args.cmd == "diag":
        from hdmirec import diag
        print("Running diagnostics (about 20 s)...")
        path = diag.write_report(args.device, live=not args.no_live)
        print(f"Report written to {path}")
        return

    if not os.environ.get("DISPLAY"):
        sys.exit("The recorder needs the desktop (X11). For a headless view use hdmi_rx_viewer.py.")
    from hdmirec import app, log
    log.setup()
    app.run(args, os.path.abspath(__file__))


if __name__ == "__main__":
    main()
