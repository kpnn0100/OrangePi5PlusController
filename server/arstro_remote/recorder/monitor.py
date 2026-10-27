"""On-set monitoring (REC-13): exposure analysis and the preview's LUT. Needs numpy.

ANALYSIS - a few times a second the worker samples the luma of the FULL captured frame (not
the compressed preview) on a GW x GH grid and sends every viewer that asked for it the same
data, so the web page and the app draw identical scopes and overlays:

    map    GW x GH bytes, row by row from the top: each cell's level as IRE % x 2
           (0 = black, 200 = 100 % white, 255 = 127.5 %); zebras and false colour are drawn
           from it by the viewer, so changing the zebra level needs no round trip
    hist   128 bytes: share of the picture per 1 % IRE (0..127 %), sqrt-scaled to 0..255
    wave   WL x WW bytes, luma waveform: row i = IRE [i, i+1) x 110 / WL (row 0 = black, the
           BOTTOM of the scope), column = a 1/WW slice of the picture width, sqrt-scaled 0..255

Levels: YCbCr from HDMI is limited range (16 = 0 %, 235 = 100 %) unless the caps say full;
RGB is full range. FALSE_COLOUR is the one band table every controller uses.

LUT - the preview (never the recording) can be shown through a look-up table: a .cube file
(the camera maker's own log -> Rec.709 LUT) or a built-in approximation. It is applied to
the preview's I420 frames in YCbCr through a 128 x 32 x 32 table (luma per pixel, chroma per
2x2 block), cheap enough for 30 fps at 720p.
"""

import os
import re

try:
    import numpy as np
except ImportError:                     # the capabilities say so; nothing here is then used
    np = None

GW = 256                                # grid width; the height follows the picture's shape
WW, WL = 128, 64                        # waveform columns x levels
IRE_MAX = 110

# (from %, to %, colour, meaning) - drawn over the picture; anything else shows through
FALSE_COLOUR = [
    (-10, 2.5, "#7b2cbf", "black clipped"),
    (2.5, 10, "#1e5bff", "near black"),
    (40, 46, "#2ecc40", "18 % grey"),
    (52, 58, "#ff7eb6", "skin (one stop over grey)"),
    (92, 98, "#ffdc00", "near white"),
    (98, 200, "#ff2a2a", "white clipped"),
]

LUMA_RGB = (0.2126, 0.7152, 0.0722)     # Rec.709


def available():
    return np is not None


def grid_size(width, height):
    return GW, max(1, round(GW * height / max(1, width)))


def _plane(mem, offset, stride, height):
    return np.frombuffer(mem, dtype=np.uint8, count=stride * height, offset=offset).reshape(height, stride)


def sample_luma(mem, fmt, width, height, offsets, strides, full_range=None):
    """-> (GH x GW uint8 grid of 8-bit luma, full range?) read from a mapped frame."""
    gw, gh = grid_size(width, height)
    rows = ((np.arange(gh) + 0.5) * height / gh).astype(np.intp)
    cols = ((np.arange(gw) + 0.5) * width / gw).astype(np.intp)
    r, c = rows[:, None], cols[None, :]
    if fmt in ("BGR", "RGB", "BGRx", "BGRA", "RGBx", "RGBA", "xRGB", "ARGB"):
        bpp = 3 if fmt in ("BGR", "RGB") else 4
        p = _plane(mem, offsets[0], strides[0], height)
        order = {"BGR": (2, 1, 0), "RGB": (0, 1, 2), "BGRx": (2, 1, 0), "BGRA": (2, 1, 0),
                 "RGBx": (0, 1, 2), "RGBA": (0, 1, 2), "xRGB": (1, 2, 3), "ARGB": (1, 2, 3)}[fmt]
        rgb = [p[r, c * bpp + i].astype(np.uint16) for i in order]
        y = (54 * rgb[0] + 183 * rgb[1] + 19 * rgb[2]) >> 8
        return y.astype(np.uint8), True if full_range is None else full_range
    if fmt in ("YUY2", "YVYU"):
        p = _plane(mem, offsets[0], strides[0], height)
        return p[r, c * 2], bool(full_range)
    if fmt == "UYVY":
        p = _plane(mem, offsets[0], strides[0], height)
        return p[r, c * 2 + 1], bool(full_range)
    p = _plane(mem, offsets[0], strides[0], height)        # planar / semi-planar: Y first
    return p[r, c], bool(full_range)


def analyze(y8, full_range):
    """-> (meta, payload bytes) from a luma grid (see the module doc for the layout)."""
    gh, gw = y8.shape
    y = y8.astype(np.float32)
    ire = y * (100.0 / 255.0) if full_range else (y - 16.0) * (100.0 / 219.0)
    cells = np.clip(np.rint(ire * 2.0), 0, 255).astype(np.uint8)
    hist = np.bincount((cells >> 1).ravel(), minlength=128)[:128].astype(np.float32)
    hist = np.sqrt(hist / max(1.0, hist.max())) * 255.0
    lvl = np.clip((ire * (WL / IRE_MAX)).astype(np.int32), 0, WL - 1)
    col = (np.arange(gw) * WW // gw)[None, :].repeat(gh, 0)
    wave = np.bincount((lvl * WW + col).ravel(), minlength=WL * WW)[:WL * WW].astype(np.float32)
    wave = np.sqrt(wave / max(1.0, wave.max())) * 255.0
    meta = {"gw": gw, "gh": gh, "ww": WW, "wl": WL, "ire_max": IRE_MAX,
            "range": "full" if full_range else "limited"}
    payload = hist.astype(np.uint8).tobytes() + wave.astype(np.uint8).tobytes() + cells.tobytes()
    return meta, payload


# ------------------------------------------------------------------------------- LUTs
def _rec709_display(x):
    """Scene linear -> Rec.709 signal 0..1, with a soft shoulder instead of a hard clip."""
    t = 0.75
    x = np.where(x > t, t + (1 - t) * (1 - np.exp(-(x - t) / (1 - t))), x)
    x = np.clip(x, 0, 1)
    return np.where(x < 0.018, 4.5 * x, 1.099 * np.power(x, 0.45) - 0.099)


def _slog3(cv):                         # cv: 10-bit code value
    return np.where(cv >= 171.2102946929, np.power(10.0, (cv - 420.0) / 261.5) * 0.19 - 0.01,
                    (cv - 95.0) * 0.01125 / (171.2102946929 - 95.0))


def _vlog(cv):
    v = (cv - 64.0) / 876.0
    return np.where(v < 0.181, (v - 0.125) / 5.6, np.power(10.0, (v - 0.598206) / 0.241514) - 0.00873)


def _hlg(cv):
    v = np.clip((cv - 64.0) / 876.0, 0, 1)
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    e = np.where(v <= 0.5, v * v / 3.0, (np.exp((v - c) / a) + b) / 12.0)
    return e / 0.265 * 0.9                # HLG reference white (75 %) -> ~90 % of SDR white


def _generic_log(cv):
    v = np.clip((cv - 64.0) / 876.0, 0, 1)
    return 0.18 * np.power(2.0, (v - 0.41) * 14.0)      # ~14 stops around 41 % grey


# id -> (name, signal->linear, chroma gain)
BUILTIN = {
    "slog3": ("S-Log3 → Rec.709 (approx.)", _slog3, 1.55),
    "vlog": ("V-Log → Rec.709 (approx.)", _vlog, 1.5),
    "hlg": ("HLG → Rec.709", _hlg, 1.15),
    "log": ("Any log: contrast only", _generic_log, 1.35),
}


def parse_cube(text):
    """A .cube file -> ('3d', size, N^3 x 3 float array, red fastest) or ('1d', size, N x 3)."""
    size3 = size1 = None
    lo, hi = [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split()[0].upper()
        if key == "LUT_3D_SIZE":
            size3 = int(line.split()[1])
        elif key == "LUT_1D_SIZE":
            size1 = int(line.split()[1])
        elif key == "DOMAIN_MIN":
            lo = [float(x) for x in line.split()[1:4]]
        elif key == "DOMAIN_MAX":
            hi = [float(x) for x in line.split()[1:4]]
        elif re.match(r"^[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+$", line):
            rows.append([float(x) for x in line.split()])
        elif key in ("TITLE", "LUT_3D_INPUT_RANGE", "LUT_1D_INPUT_RANGE"):
            continue
        else:
            raise ValueError("not a .cube line: %s" % line[:40])
    if size3:
        if not 2 <= size3 <= 129 or len(rows) != size3 ** 3:
            raise ValueError("LUT_3D_SIZE %s needs %d rows, the file has %d" % (size3, (size3 or 0) ** 3, len(rows)))
        return "3d", size3, np.array(rows, dtype=np.float32), lo, hi
    if size1:
        if not 2 <= size1 <= 65536 or len(rows) != size1:
            raise ValueError("LUT_1D_SIZE %s needs %d rows, the file has %d" % (size1, size1, len(rows)))
        return "1d", size1, np.array(rows, dtype=np.float32), lo, hi
    raise ValueError("no LUT_3D_SIZE or LUT_1D_SIZE line")


def _apply_cube(cube, rgb):
    """rgb: (n, 3) 0..1 -> (n, 3) through a parsed .cube (trilinear for 3D)."""
    kind, n, data, lo, hi = cube
    lo, hi = np.array(lo, np.float32), np.array(hi, np.float32)
    x = np.clip((rgb - lo) / np.maximum(hi - lo, 1e-6), 0, 1) * (n - 1)
    if kind == "1d":
        out = np.empty_like(x)
        for ch in range(3):
            out[:, ch] = np.interp(x[:, ch], np.arange(n), data[:, ch])
        return out
    i0 = np.clip(np.floor(x).astype(np.int32), 0, n - 2)
    f = x - i0
    lut = data.reshape(n, n, n, 3)            # [b][g][r]: red changes fastest in the file
    out = np.zeros_like(x)
    for db in (0, 1):
        for dg in (0, 1):
            for dr in (0, 1):
                w = ((f[:, 2] if db else 1 - f[:, 2]) * (f[:, 1] if dg else 1 - f[:, 1])
                     * (f[:, 0] if dr else 1 - f[:, 0]))
                out += w[:, None] * lut[i0[:, 2] + db, i0[:, 1] + dg, i0[:, 0] + dr]
    return out


class LutTable:
    """A LUT as three 128 x 32 x 32 tables over (Y >> 1, U >> 3, V >> 3) of 8-bit I420."""

    def __init__(self, ty, tu, tv, name):
        self.ty, self.tu, self.tv, self.name = ty, tu, tv, name

    @classmethod
    def build(cls, lut_id, cube_text=None, name=None):
        y = (np.arange(128, dtype=np.float32) * 2 + 1)[:, None, None]
        u = (np.arange(32, dtype=np.float32) * 8 + 4)[None, :, None]
        v = (np.arange(32, dtype=np.float32) * 8 + 4)[None, None, :]
        y, u, v = np.broadcast_arrays(y, u, v)
        if cube_text is not None:
            # the signal's code values as data (0..1 over 0..255), the way monitors feed a camera LUT
            r = y + 1.5748 * (v - 128)
            g = y - 0.1873 * (u - 128) - 0.4681 * (v - 128)
            b = y + 1.8556 * (u - 128)
            rgb = np.stack([r, g, b], -1).reshape(-1, 3) / 255.0
            out = _apply_cube(parse_cube(cube_text), rgb) * 255.0
            r2, g2, b2 = out[:, 0], out[:, 1], out[:, 2]
            yo = LUMA_RGB[0] * r2 + LUMA_RGB[1] * g2 + LUMA_RGB[2] * b2
            uo = 128 + (b2 - yo) / 1.8556
            vo = 128 + (r2 - yo) / 1.5748
            shape = y.shape
            ty, tu, tv = yo.reshape(shape), uo.reshape(shape), vo.reshape(shape)
        else:
            title, to_linear, gain = BUILTIN[lut_id]
            name = name or title
            lin = to_linear(np.clip(y, 1, 254) * 4.0)          # 8-bit legal range -> 10-bit code value
            ty = 16 + 219 * _rec709_display(np.maximum(lin, 0))
            tu = 128 + (u - 128) * gain
            tv = 128 + (v - 128) * gain
        clip = lambda a: np.clip(np.rint(a), 0, 255).astype(np.uint8)
        return cls(clip(ty).ravel(), clip(tu).ravel(), clip(tv).ravel(), name)

    def apply_i420(self, data, width, height):
        """I420 frame bytes -> new I420 bytes through the table."""
        w2, h2 = width // 2, height // 2
        a = np.frombuffer(data, dtype=np.uint8)
        ysz = width * height
        Y = a[:ysz].reshape(h2, 2, w2, 2)
        U = a[ysz:ysz + w2 * h2].reshape(h2, w2)
        V = a[ysz + w2 * h2:ysz + 2 * w2 * h2].reshape(h2, w2)
        cidx = ((U >> 3).astype(np.uint32) << 5) | (V >> 3)
        yidx = ((Y >> 1).astype(np.uint32) << 10) | cidx[:, None, :, None]
        out = np.empty(ysz + 2 * w2 * h2, dtype=np.uint8)
        out[:ysz] = self.ty[yidx].reshape(-1)
        bidx = ((Y[:, 0, :, 0] >> 1).astype(np.uint32) << 10) | cidx
        out[ysz:ysz + w2 * h2] = self.tu[bidx].reshape(-1)
        out[ysz + w2 * h2:] = self.tv[bidx].reshape(-1)
        return out.tobytes()


def lut_dir():
    from ..paths import config_dir
    return os.path.join(config_dir(), "luts")


def lut_list():
    """[{id, name, builtin}] - the built-ins, then the .cube files the user added."""
    out = [{"id": k, "name": v[0], "builtin": True} for k, v in BUILTIN.items()]
    try:
        for n in sorted(os.listdir(lut_dir())):
            if n.lower().endswith(".cube"):
                out.append({"id": "cube:" + n[:-5], "name": n[:-5], "builtin": False})
    except OSError:
        pass
    return out


def lut_path(lut_id):
    if not lut_id.startswith("cube:"):
        return None
    return os.path.join(lut_dir(), lut_id[5:] + ".cube")


def check_lut_name(name):
    name = (name or "").strip()
    if name.lower().endswith(".cube"):
        name = name[:-5]
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,59}$", name):
        raise ValueError("a LUT name is 1-60 letters, digits, space . _ + -")
    return name


def describe_bands():
    return [{"from": a, "to": b, "colour": c, "meaning": m} for a, b, c, m in FALSE_COLOUR]

