"""A ZIP written straight into a socket, its exact length known before the first byte (GAL-12).

Selected recordings are downloaded as one file. They are already compressed video (or RAW that
does not compress), so entries are STORED - no recompression, no temporary copy on the board's
disk, and the archive streams at disk / network speed. Because every entry is stored and every
size is known up front, the whole layout is computed first: the HTTP response carries a real
Content-Length and the browser shows a real progress bar for a 40 GB month.

The one thing not known in advance is each file's CRC-32, so each entry uses a data descriptor
(general-purpose bit 3: the CRC follows the data), the way streaming writers do (CPython's
zipfile on an unseekable stream writes the same shape). ZIP64 is used where a value does not
fit 32 bits - a file of 4 GB or more, an offset past 4 GB, more than 65535 entries - and only
there, so a small archive stays a plain ZIP for old tools.

    z = ZipStream([("2026-09-30/rec_x.mp4", "/data/rec_x.mp4"), ...])
    z.length            # bytes the archive will have
    z.write(sock_file)  # raises ChangedError if a file changed size after the layout
"""

import os
import struct
import time
import zlib

LIMIT = 0xFFFFFFFF                     # the largest value a 32-bit ZIP field holds
_FLAGS = 0x0008 | 0x0800               # data descriptor follows | names are UTF-8
CHUNK = 4 << 20


class ChangedError(OSError):
    """A file's size changed between the layout and the copy (the Content-Length is already sent)."""


def _dos(mtime):
    t = time.localtime(mtime)
    year = min(max(t.tm_year, 1980), 2107)
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
            ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


class _Entry:
    __slots__ = ("name", "path", "size", "mtime", "offset", "big", "crc")

    def __init__(self, name, path, size, mtime):
        self.name, self.path, self.size, self.mtime = name.encode("utf-8"), path, size, mtime
        self.offset = 0
        self.big = False
        self.crc = 0


class ZipStream:
    def __init__(self, files, limit=LIMIT, force_zip64_end=False):
        """`files`: [(name in the archive, path)]. Each file is stat'ed now; `limit` and
        `force_zip64_end` exist so a test can walk the ZIP64 paths with small files."""
        self.limit = limit
        self.entries = []
        seen = set()
        for name, path in files:
            if name in seen:
                raise ValueError("%s is in the archive twice" % name)
            seen.add(name)
            st = os.stat(path)
            self.entries.append(_Entry(name, path, st.st_size, st.st_mtime))
        offset = 0
        for e in self.entries:
            e.offset = offset
            e.big = e.size >= limit
            offset += 30 + len(e.name) + (20 if e.big else 0) + e.size + (24 if e.big else 16)
        self.cd_offset = offset
        self.cd_size = sum(46 + len(e.name) + self._cd_extra_len(e) for e in self.entries)
        self.zip64_end = (force_zip64_end or len(self.entries) >= 0xFFFF or
                          self.cd_offset >= limit or self.cd_size >= limit)
        self.length = self.cd_offset + self.cd_size + (56 + 20 if self.zip64_end else 0) + 22

    def _cd_extra_len(self, e):
        n = (16 if e.big else 0) + (8 if e.offset >= self.limit else 0)
        return 4 + n if n else 0

    def write(self, out, progress=None):
        """Write the archive into `out` (a binary file object). `progress(done_bytes)` is called
        after every chunk. Raises ChangedError if a file is shorter or longer than at the layout."""
        done = 0

        def put(b):
            nonlocal done
            out.write(b)
            done += len(b)
            if progress:
                progress(done)

        for e in self.entries:
            tm, dt = _dos(e.mtime)
            if e.big:   # sizes in the ZIP64 extra field (zero here, the descriptor carries them)
                put(struct.pack("<IHHHHHIIIHH", 0x04034B50, 45, _FLAGS, 0, tm, dt, 0, LIMIT, LIMIT, len(e.name), 20)
                    + e.name + struct.pack("<HHQQ", 0x0001, 16, 0, 0))
            else:
                put(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, _FLAGS, 0, tm, dt, 0, 0, 0, len(e.name), 0) + e.name)
            crc, left = 0, e.size
            with open(e.path, "rb") as f:
                while left > 0:
                    b = f.read(min(CHUNK, left))
                    if not b:
                        raise ChangedError("%s got shorter while it was being downloaded" % e.name.decode())
                    crc = zlib.crc32(b, crc)
                    left -= len(b)
                    put(b)
                if f.read(1):
                    raise ChangedError("%s grew while it was being downloaded" % e.name.decode())
            e.crc = crc & 0xFFFFFFFF
            if e.big:
                put(struct.pack("<IIQQ", 0x08074B50, e.crc, e.size, e.size))
            else:
                put(struct.pack("<IIII", 0x08074B50, e.crc, e.size, e.size))

        for e in self.entries:
            tm, dt = _dos(e.mtime)
            big_off = e.offset >= self.limit
            extra = b""
            if e.big or big_off:
                body = (struct.pack("<QQ", e.size, e.size) if e.big else b"") + (struct.pack("<Q", e.offset) if big_off else b"")
                extra = struct.pack("<HH", 0x0001, len(body)) + body
            put(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, (3 << 8) | 45, 45 if extra else 20, _FLAGS, 0, tm, dt,
                            e.crc, LIMIT if e.big else e.size, LIMIT if e.big else e.size, len(e.name), len(extra), 0,
                            0, 0, 0o100644 << 16, LIMIT if big_off else e.offset) + e.name + extra)

        n = len(self.entries)
        if self.zip64_end:
            end64 = self.cd_offset + self.cd_size
            put(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, (3 << 8) | 45, 45, 0, 0, n, n, self.cd_size, self.cd_offset))
            put(struct.pack("<IIQI", 0x07064B50, 0, end64, 1))
            put(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, min(n, 0xFFFF), min(n, 0xFFFF),
                            min(self.cd_size, LIMIT), min(self.cd_offset, LIMIT), 0))
        else:
            put(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, self.cd_size, self.cd_offset, 0))
        if done != self.length:                        # the layout and the bytes must agree exactly
            raise ChangedError("archive is %d bytes, %d were announced" % (done, self.length))
        return done
