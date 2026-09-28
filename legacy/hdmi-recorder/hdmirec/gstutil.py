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


def is_dmabuf(buf):
    return buf.n_memory() > 0 and GstAllocators.is_dmabuf_memory(buf.peek_memory(0))
