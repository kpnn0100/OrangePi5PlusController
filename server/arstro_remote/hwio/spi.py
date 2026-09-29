"""SPI through spidev (/dev/spidevB.C) - IO-05.

A full-duplex transfer: the bytes clocked out and the bytes clocked in at the same time.
spidev nodes exist only when the device tree enables them (on Orange Pi images: an
overlay such as spi0-m2-cs0-spidev in /boot/orangepiEnv.txt).
"""

import ctypes
import errno
import fcntl
import glob
import os

from . import dt


class SpiError(Exception):
    pass


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("k") << 8) | nr


SPI_IOC_RD_MODE = _ioc(2, 1, 1)
SPI_IOC_WR_MODE = _ioc(1, 1, 1)
SPI_IOC_RD_LSB_FIRST = _ioc(2, 2, 1)
SPI_IOC_WR_LSB_FIRST = _ioc(1, 2, 1)
SPI_IOC_RD_BITS_PER_WORD = _ioc(2, 3, 1)
SPI_IOC_WR_BITS_PER_WORD = _ioc(1, 3, 1)
SPI_IOC_RD_MAX_SPEED_HZ = _ioc(2, 4, 4)
SPI_IOC_WR_MAX_SPEED_HZ = _ioc(1, 4, 4)

SPI_CS_HIGH = 0x04
SPI_LSB_FIRST = 0x08
SPI_3WIRE = 0x10
SPI_LOOP = 0x20
SPI_NO_CS = 0x40

MAX_TRANSFER = 4096


class _Transfer(ctypes.Structure):
    _fields_ = [("tx_buf", ctypes.c_uint64), ("rx_buf", ctypes.c_uint64), ("len", ctypes.c_uint32),
                ("speed_hz", ctypes.c_uint32), ("delay_usecs", ctypes.c_uint16),
                ("bits_per_word", ctypes.c_uint8), ("cs_change", ctypes.c_uint8),
                ("tx_nbits", ctypes.c_uint8), ("rx_nbits", ctypes.c_uint8),
                ("word_delay_usecs", ctypes.c_uint8), ("pad", ctypes.c_uint8)]


assert ctypes.sizeof(_Transfer) == 32


def SPI_IOC_MESSAGE(n):
    return _ioc(1, 0, 32 * n)


def _explain(e, what):
    if e.errno in (errno.EACCES, errno.EPERM):
        return SpiError("%s: permission denied - run install.sh (group spi), then log in again" % what)
    return SpiError("%s: %s" % (what, e.strerror or e))


def devices():
    out = []
    for p in sorted(glob.glob("/dev/spidev*")):
        name = os.path.basename(p)
        sysdev = "/sys/class/spidev/%s/device" % name
        bus, _, cs = name[len("spidev"):].partition(".")
        ctrl = os.path.realpath(sysdev + "/..") if os.path.exists(sysdev) else ""
        out.append({"path": p, "bus": int(bus) if bus.isdigit() else bus, "cs": int(cs) if cs.isdigit() else cs,
                    "dt": dt.names_of(ctrl, "spi") if ctrl else [], "access": os.access(p, os.R_OK | os.W_OK)})
    return out


def dev_path(dev):
    s = str(dev)
    if not s.startswith("/"):
        s = "/dev/" + (s if s.startswith("spidev") else "spidev" + s)
    if not s.startswith("/dev/spidev") or not os.path.basename(s)[6:].replace(".", "").isdigit():
        raise SpiError("no SPI device %r" % dev)
    return s


def transfer(dev, tx, mode=0, speed_hz=1_000_000, bits=8, lsb_first=False, cs_high=False, delay_us=0):
    path = dev_path(dev)
    tx = bytes(tx or b"")
    if not tx:
        raise SpiError("nothing to send (a read clocks out bytes too: send 00 00 ...)")
    if len(tx) > MAX_TRANSFER:
        raise SpiError("at most %d bytes per transfer" % MAX_TRANSFER)
    mode = int(mode)
    if mode not in (0, 1, 2, 3):
        raise SpiError("mode must be 0..3")
    speed_hz = int(speed_hz)
    if not 1000 <= speed_hz <= 100_000_000:
        raise SpiError("speed must be 1 kHz .. 100 MHz")
    bits = int(bits)
    if not 1 <= bits <= 32:
        raise SpiError("bits per word must be 1..32")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as e:
        raise _explain(e, path)
    try:
        m = mode | (SPI_LSB_FIRST if lsb_first else 0) | (SPI_CS_HIGH if cs_high else 0)
        fcntl.ioctl(fd, SPI_IOC_WR_MODE, bytes([m]))
        fcntl.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, bytes([bits]))
        fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, speed_hz.to_bytes(4, "little"))
        txb = ctypes.create_string_buffer(tx, len(tx))
        rxb = ctypes.create_string_buffer(len(tx))
        t = _Transfer(ctypes.addressof(txb), ctypes.addressof(rxb), len(tx), speed_hz, int(delay_us), bits,
                      0, 0, 0, 0, 0)
        fcntl.ioctl(fd, SPI_IOC_MESSAGE(1), t)
        return rxb.raw
    except OSError as e:
        raise _explain(e, path)
    finally:
        os.close(fd)
