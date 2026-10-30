#!/usr/bin/env python3
"""USB / UVC cameras through the V4L2 uAPI (CAM-02, CAM-04) - no v4l2-ctl needed.

Without hardware: the ioctl numbers and structure sizes against linux/videodev2.h, fourcc
decoding, the USB device of a sysfs path, and that a missing v4l2-ctl no longer crashes.
With a camera (opt-in): ARSTRO_TEST_CAMERA=/dev/video0 [ARSTRO_TEST_CAMERA_MIC=1]
    python3 tests/test_v4l2.py
"""

import ctypes
import os

from harness import check, run, test

from arstro_remote.recorder import v4l2

CAMERA = os.environ.get("ARSTRO_TEST_CAMERA")


@test("CAM-02")
def ioctls_match_the_kernel_header():
    # the values linux/videodev2.h gives (_IOR('V', 0, ...) etc. on every Linux architecture)
    check(v4l2.VIDIOC_QUERYCAP == 0x80685600, hex(v4l2.VIDIOC_QUERYCAP))
    check(v4l2.VIDIOC_ENUM_FMT == 0xC0405602, hex(v4l2.VIDIOC_ENUM_FMT))
    check(v4l2.VIDIOC_ENUM_FRAMESIZES == 0xC02C564A, hex(v4l2.VIDIOC_ENUM_FRAMESIZES))
    check(v4l2.VIDIOC_ENUM_FRAMEINTERVALS == 0xC034564B, hex(v4l2.VIDIOC_ENUM_FRAMEINTERVALS))
    check([ctypes.sizeof(s) for s in (v4l2._Capability, v4l2._FmtDesc, v4l2._FrmSize, v4l2._FrmIval)]
          == [104, 64, 44, 52], "structure sizes")
    check(v4l2._fourcc(0x47504A4D) == "MJPG" and v4l2._fourcc(0x56595559) == "YUYV", "fourcc text")


@test("CAM-02", "ARC-08")
def a_missing_v4l2_ctl_is_an_answer_not_a_crash():
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = "/nonexistent"
    try:
        rc, out = v4l2.v4l2("/dev/video0", "-D")
    finally:
        os.environ["PATH"] = old
    check(rc == 127 and "v4l-utils" in out, (rc, out))
    check(v4l2.camera_modes("/dev/video-nonexistent") == [], "an unopenable device has no modes")


@test("CAM-04")
def the_usb_device_of_an_interface():
    base = "/sys/devices/pci0000:00/0000:00:02.1/0000:0e:00.0/usb1/1-5"
    check(v4l2._usb_device(base + "/1-5:1.2") == base, "audio interface -> its USB device")
    check(v4l2._usb_device(base + "/1-5:1.0") == base, "video interface -> the same USB device")
    check(v4l2._usb_device("/sys/devices/pci0000:00/0000:00:08.1/0000:74:00.1") is None, "not USB")


@test("CAM-02")
def a_real_camera_is_found_with_its_modes():
    if not CAMERA:
        print("      (skipped: set ARSTRO_TEST_CAMERA=/dev/videoN to check a real camera)")
        return
    cams = {c["device"]: c for c in v4l2.list_cameras()}
    check(CAMERA in cams, "%s not listed: %s" % (CAMERA, list(cams)))
    cam = cams[CAMERA]
    check(not cam.get("error"), cam.get("error"))
    check(cam["modes"] and cam["driver"] and cam["name"], cam)
    best = v4l2.best_mode(v4l2.camera_modes(CAMERA))
    check(best and best[0] in v4l2.SUPPORTED_FOURCC and best[1] * best[2] <= 1920 * 1080, best)
    # the same camera's other nodes (UVC metadata) are not offered as a second camera
    names = [c["name"] for c in cams.values()]
    check(names.count(cam["name"]) == 1, "the camera is listed more than once: %s" % names)


@test("CAM-04")
def a_real_cameras_own_microphone():
    if not (CAMERA and os.environ.get("ARSTRO_TEST_CAMERA_MIC")):
        print("      (skipped: set ARSTRO_TEST_CAMERA and ARSTRO_TEST_CAMERA_MIC=1 for a camera with a microphone)")
        return
    card = v4l2.camera_audio_card(CAMERA)
    check(card and v4l2.alsa_card_present(card), "no microphone found for %s: %r" % (CAMERA, card))


if __name__ == "__main__":
    raise SystemExit(run(description=__doc__))
