"""GStreamer buffer helpers that work around bugs in this board's plugin versions."""
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("GstAllocators", "1.0")
from gi.repository import Gst, GstAllocators, GstVideo  # noqa: E402


def gst_buffer(data):
    """Gst.Buffer holding a copy of `data`.

    (Gst.Buffer.new_wrapped leaks the wrapped bytes in PyGObject 3.42, which at
    4K60 exhausts memory within seconds.)
    """
    buf = Gst.Buffer.new_allocate(None, len(data), None)
    buf.fill(0, data)
    return buf


# mpph265enc (gst-rockchip 1.14) leaks one frame-sized MPP buffer per frame when
# given plain system memory that did not come from its own pool: at 4K memory is
# gone after ~250 frames and the stream turns undecodable. dmabuf input (HDMI RX)
# and buffers written by an upstream videoconvert are fine.
#
# RGA scaling (GST_MPP_NO_RGA=0 + width=/height=) leaks the same way, and faster: measured on
# a 4K H.265 file -> 1080p H.264, mppvideodec width/height ate 4.4 GB in 45 s of video (and lost
# frames), mpph264enc width/height 5.4 GB in 20 s. GL scaling into mpph264enc too (5.4 GB in
# 34 s) unless a videoconvert writes the encoder's buffers (then too slow for real time). The
# board has no OOM escape - it freezes. Decoder output straight into the encoder at the same
# size is fine (~450 MB, flat). Whether HDMI RX dmabuf + encoder RGA scaling (capture.py's
# zero-copy preview) leaks too is not measured yet.


def is_dmabuf(buf):
    return buf.n_memory() > 0 and GstAllocators.is_dmabuf_memory(buf.peek_memory(0))
