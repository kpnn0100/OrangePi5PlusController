"""I2C through i2c-dev (/dev/i2c-N) - IO-04.

scan() probes like i2cdetect (SMBus quick write, read byte for EEPROM/sensor ranges that
quick write can corrupt); transfer() is a raw combined write-then-read with a repeated
start (I2C_RDWR), which is what register reads of almost every chip need.
"""

import ctypes
import errno
import fcntl
import glob
import os

from . import dt

I2C_SLAVE = 0x0703
I2C_FUNCS = 0x0705
I2C_RDWR = 0x0707
I2C_SMBUS = 0x0720
I2C_M_RD = 0x0001
I2C_M_TEN = 0x0010

I2C_FUNC_I2C = 0x00000001
I2C_FUNC_SMBUS_QUICK = 0x00010000
I2C_FUNC_SMBUS_READ_BYTE = 0x00020000

SMBUS_READ, SMBUS_WRITE = 1, 0
SMBUS_QUICK, SMBUS_BYTE = 0, 1

MAX_TRANSFER = 4096


class I2cError(Exception):
    pass


class _Msg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16), ("len", ctypes.c_uint16),
                ("buf", ctypes.c_void_p)]


class _RdwrData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(_Msg)), ("nmsgs", ctypes.c_uint32)]


class _SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class _SmbusIoctl(ctypes.Structure):
    _fields_ = [("read_write", ctypes.c_uint8), ("command", ctypes.c_uint8), ("size", ctypes.c_uint32),
                ("data", ctypes.POINTER(_SmbusData))]


def _explain(e, what):
    if e.errno in (errno.EACCES, errno.EPERM):
        return I2cError("%s: permission denied - run install.sh (group i2c), then log in again" % what)
    if e.errno in (errno.ENXIO, errno.EREMOTEIO, errno.EIO):
        return I2cError("%s: no answer (NACK) - nothing at that address, or it refused the transfer" % what)
    if e.errno == errno.ETIMEDOUT:
        return I2cError("%s: timed out (bus stuck? check pull-ups)" % what)
    return I2cError("%s: %s" % (what, e.strerror or e))


def bus_path(bus):
    s = str(bus)
    if s.isdigit():
        return "/dev/i2c-%s" % s
    if s.startswith("/dev/i2c-") and s[len("/dev/i2c-"):].isdigit():
        return s
    raise I2cError("no I2C bus %r" % bus)


def buses():
    out = []
    for p in sorted(glob.glob("/dev/i2c-*"), key=lambda p: int(p.rsplit("-", 1)[1])):
        n = int(p.rsplit("-", 1)[1])
        sysdev = "/sys/class/i2c-dev/i2c-%d/device" % n
        try:
            with open("/sys/class/i2c-dev/i2c-%d/name" % n) as f:
                name = f.read().strip()
        except OSError:
            name = ""
        out.append({"bus": n, "path": p, "name": name, "dt": dt.names_of(sysdev, "i2c"),
                    "access": os.access(p, os.R_OK | os.W_OK)})
    return out


def _addr(addr):
    a = int(addr, 0) if isinstance(addr, str) else int(addr)
    if not 0 <= a <= 0x3FF:
        raise I2cError("address %r out of range" % addr)
    return a


def _open(bus):
    path = bus_path(bus)
    try:
        return os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as e:
        raise _explain(e, path)


def functionality(bus):
    fd = _open(bus)
    try:
        buf = ctypes.c_ulong()
        fcntl.ioctl(fd, I2C_FUNCS, buf)
        return buf.value
    finally:
        os.close(fd)


def scan(bus, first=0x08, last=0x77):
    """{"found": [addr], "busy": [addr]} - busy = claimed by a kernel driver ("UU")."""
    fd = _open(bus)
    found, busy = [], []
    try:
        funcs = ctypes.c_ulong()
        fcntl.ioctl(fd, I2C_FUNCS, funcs)
        can_quick = bool(funcs.value & I2C_FUNC_SMBUS_QUICK)
        can_read = bool(funcs.value & I2C_FUNC_SMBUS_READ_BYTE)
        if not (can_quick or can_read):
            raise I2cError("bus %s supports neither quick write nor read byte probes" % bus)
        data = _SmbusData()
        for a in range(max(0x03, int(first)), min(0x77, int(last)) + 1):
            try:
                fcntl.ioctl(fd, I2C_SLAVE, a)
            except OSError as e:
                if e.errno == errno.EBUSY:
                    busy.append(a)
                    continue
                raise _explain(e, "address 0x%02x" % a)
            # like i2cdetect's auto mode: quick write can lock up some EEPROMs/sensors
            use_read = (0x30 <= a <= 0x37 or 0x50 <= a <= 0x5F) or not can_quick
            if use_read:
                args = _SmbusIoctl(SMBUS_READ, 0, SMBUS_BYTE, ctypes.pointer(data))
            else:
                args = _SmbusIoctl(SMBUS_WRITE, 0, SMBUS_QUICK, None)
            try:
                fcntl.ioctl(fd, I2C_SMBUS, args)
                found.append(a)
            except OSError:
                pass
    except OSError as e:
        raise _explain(e, "scanning bus %s" % bus)
    finally:
        os.close(fd)
    return {"bus": int(str(bus).rsplit("-", 1)[-1]), "found": found, "busy": busy}


def transfer(bus, addr, write=b"", read=0, ten_bit=False):
    """Write `write` then (repeated start) read `read` bytes; returns the bytes read."""
    a = _addr(addr)
    read = int(read or 0)
    write = bytes(write or b"")
    if not write and not read:
        raise I2cError("nothing to write or read")
    if len(write) > MAX_TRANSFER or read > MAX_TRANSFER:
        raise I2cError("at most %d bytes per transfer" % MAX_TRANSFER)
    flags = I2C_M_TEN if ten_bit else 0
    msgs = (_Msg * 2)()
    wbuf = ctypes.create_string_buffer(write, len(write)) if write else None
    rbuf = ctypes.create_string_buffer(read) if read else None
    n = 0
    if write:
        msgs[n] = _Msg(a, flags, len(write), ctypes.cast(wbuf, ctypes.c_void_p))
        n += 1
    if read:
        msgs[n] = _Msg(a, flags | I2C_M_RD, read, ctypes.cast(rbuf, ctypes.c_void_p))
        n += 1
    fd = _open(bus)
    try:
        fcntl.ioctl(fd, I2C_RDWR, _RdwrData(msgs, n))
    except OSError as e:
        raise _explain(e, "0x%02x on bus %s" % (a, bus))
    finally:
        os.close(fd)
    return rbuf.raw[:read] if read else b""


def dump(bus, addr, start=0, count=256, reg_bytes=1):
    """Read `count` registers from `start` (register address `reg_bytes` wide), in chunks."""
    count = max(1, min(int(count), 4096))
    out = bytearray()
    pos = int(start)
    while len(out) < count:
        n = min(32, count - len(out))
        reg = pos.to_bytes(reg_bytes, "big")
        out += transfer(bus, addr, reg, n)
        pos += n
    return bytes(out)
