"""Serial ports (UART) through termios - IO-09.

A port opened by the server becomes a *serial console* in the shared terminal pool: every
controller attaches to it like to a shell (the web page's xterm, `arstro-remote term attach`,
the app), sees the same live output and can type; raw bytes (hex) go out with io.uart.send.
"""

import errno
import fcntl
import glob
import os
import struct
import termios

from ..terminal import Terminal
from . import dt

BAUDS = [50, 75, 110, 134, 150, 200, 300, 600, 1200, 1800, 2400, 4800, 9600, 19200, 38400, 57600,
         115200, 230400, 460800, 500000, 576000, 921600, 1000000, 1152000, 1500000, 2000000,
         2500000, 3000000, 3500000, 4000000]

MODEM_BITS = {"dtr": termios.TIOCM_DTR, "rts": termios.TIOCM_RTS, "cts": termios.TIOCM_CTS,
              "dsr": termios.TIOCM_DSR, "cd": termios.TIOCM_CD, "ri": termios.TIOCM_RI}


class UartError(Exception):
    pass


def _explain(e, what):
    if e.errno in (errno.EACCES, errno.EPERM):
        return UartError("%s: permission denied - the user needs group dialout (install.sh adds it)" % what)
    if e.errno == errno.EBUSY:
        return UartError("%s: in use by another program" % what)
    return UartError("%s: %s" % (what, e.strerror or e))


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _consoles():
    """Kernel consoles from the command line (ttyS2 etc.) - typing there reaches a getty."""
    out = set()
    for arg in (_read("/proc/cmdline") or "").split():
        if arg.startswith("console="):
            out.add(arg[len("console="):].split(",")[0])
    return out


def ports():
    consoles = _consoles()
    out = []
    for d in sorted(glob.glob("/sys/class/tty/*/device")):
        tty = os.path.dirname(d)
        name = os.path.basename(tty)
        if not name.startswith(("tty", "rfcomm")):
            continue
        driver = os.path.basename(os.path.realpath(d + "/driver")) if os.path.exists(d + "/driver") else ""
        if driver == "serial8250" and _read(tty + "/type") in ("0", None):
            continue                                   # placeholder, no real UART behind it
        path = "/dev/" + name
        if not os.path.exists(path):
            continue
        kind = ("usb" if name.startswith(("ttyUSB", "ttyACM")) else "debug" if "fiq" in driver
                else "bluetooth" if name.startswith("rfcomm") else "uart")
        out.append({"port": name, "path": path, "driver": driver, "kind": kind,
                    "dt": dt.names_of(d, "serial") or dt.names_of(d, "uart"),
                    "console": name in consoles or kind == "debug",
                    "access": os.access(path, os.R_OK | os.W_OK)})
    return out


def port_path(port):
    s = str(port)
    if not s.startswith("/"):
        s = "/dev/" + s
    name = os.path.basename(s)
    ok = (os.path.dirname(s) == "/dev" and name.startswith(("tty", "rfcomm"))) or \
         (os.path.dirname(s) == "/dev/pts" and name.isdigit())        # virtual ports (socat, tests)
    if not ok or not os.path.exists(s):
        raise UartError("no serial port %r" % port)
    return s


def configure(fd, baud=115200, data_bits=8, parity="none", stop_bits=1, flow="none"):
    baud = int(baud)
    if baud not in BAUDS:
        raise UartError("unsupported baud rate %d (use one of %s)" % (baud, ", ".join(map(str, BAUDS))))
    speed = getattr(termios, "B%d" % baud, None)
    if speed is None:
        raise UartError("this system has no B%d" % baud)
    csize = {5: termios.CS5, 6: termios.CS6, 7: termios.CS7, 8: termios.CS8}.get(int(data_bits))
    if csize is None:
        raise UartError("data bits must be 5..8")
    if parity not in ("none", "even", "odd"):
        raise UartError("parity must be none, even or odd")
    if int(stop_bits) not in (1, 2):
        raise UartError("stop bits must be 1 or 2")
    if flow not in ("none", "rtscts", "xonxoff"):
        raise UartError("flow control must be none, rtscts or xonxoff")
    iflag, oflag, cflag, lflag, _i, _o, cc = termios.tcgetattr(fd)
    # raw mode (cfmakeraw)
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR |
               termios.IGNCR | termios.ICRNL | termios.IXON | termios.IXOFF | termios.IXANY)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.PARODD | termios.CSTOPB | termios.CRTSCTS)
    cflag |= csize | termios.CREAD | termios.CLOCAL
    if parity != "none":
        cflag |= termios.PARENB | (termios.PARODD if parity == "odd" else 0)
    if int(stop_bits) == 2:
        cflag |= termios.CSTOPB
    if flow == "rtscts":
        cflag |= termios.CRTSCTS
    elif flow == "xonxoff":
        iflag |= termios.IXON | termios.IXOFF
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])
    return {"baud": baud, "data_bits": int(data_bits), "parity": parity, "stop_bits": int(stop_bits), "flow": flow}


class SerialTerminal(Terminal):
    """A serial port in the terminal pool (same attach/replay/mirroring as shells)."""

    def __init__(self, term_id, cols, rows, pool, opened_by, ephemeral_owner=None, *, port, settings):
        self.port_path = port_path(port)
        self.port_settings = dict(settings)
        self.rx_bytes = self.tx_bytes = 0
        super().__init__(term_id, cols, rows, pool, opened_by, ephemeral_owner)

    def _spawn(self, cols, rows):
        try:
            fd = os.open(self.port_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            raise _explain(e, self.port_path)
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)               # nobody else opens it meanwhile
            self.port_settings = configure(fd, **self.port_settings)
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
        except OSError as e:
            os.close(fd)
            raise _explain(e, self.port_path)
        except UartError:
            os.close(fd)
            raise
        self.pid = None
        self.fd = fd
        self.rows, self.cols = int(rows), int(cols)

    def _reap(self, timeout=0):
        return None

    def append_ring(self, data):
        self.rx_bytes += len(data)
        super().append_ring(data)

    def write(self, data: bytes):
        self.tx_bytes += len(data)
        super().write(data)

    def resize(self, cols, rows):
        self.rows, self.cols = int(rows), int(cols)       # nothing to tell a serial line

    def reconfigure(self, **settings):
        merged = dict(self.port_settings, **settings)
        try:
            self.port_settings = configure(self.fd, **merged)
        except OSError as e:
            raise _explain(e, self.port_path)
        return self.port_settings

    def modem(self):
        try:
            raw = fcntl.ioctl(self.fd, termios.TIOCMGET, struct.pack("I", 0))
        except OSError as e:
            raise _explain(e, self.port_path)
        bits = struct.unpack("I", raw)[0]
        return {k: bool(bits & v) for k, v in MODEM_BITS.items()}

    def set_modem(self, dtr=None, rts=None):
        for name, value in (("dtr", dtr), ("rts", rts)):
            if value is None:
                continue
            req = termios.TIOCMBIS if value else termios.TIOCMBIC
            try:
                fcntl.ioctl(self.fd, req, struct.pack("I", MODEM_BITS[name]))
            except OSError as e:
                raise _explain(e, self.port_path)
        return self.modem()

    def send_break(self):
        """A break condition of 0.25-0.5 s (tcsendbreak(0)); resets many boot loaders."""
        try:
            termios.tcsendbreak(self.fd, 0)
        except OSError as e:
            raise _explain(e, self.port_path)
        return {}

    def kill(self):
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass
        import logging
        logging.getLogger("arstro.io").info("serial console %d (%s) closed", self.id, self.port_path)

    def describe(self):
        d = super().describe()
        d.update(kind="serial", port=os.path.basename(self.port_path), settings=self.port_settings,
                 rx_bytes=self.rx_bytes, tx_bytes=self.tx_bytes)
        return d
