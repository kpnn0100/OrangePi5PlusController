#!/usr/bin/env python3
"""Generates the Arstro Remote launcher icons (legacy + adaptive) with Pillow.

    python3 tool/make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RES = os.path.join(ROOT, "android", "app", "src", "main", "res")
SS = 4  # supersampling

TOP = (61, 123, 255)      # #3D7BFF
BOTTOM = (124, 77, 255)   # #7C4DFF
WHITE = (255, 255, 255, 255)
SOFT = (255, 255, 255, 150)


def gradient(size):
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,)
    return img


def symbol(size, scale=1.0):
    """White orbit + planet + signal arcs, centred, on transparent background."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2
    r = s * 0.17 * scale                  # planet radius
    w = max(2, int(s * 0.045 * scale))    # line width
    # orbit: tilted ellipse drawn on its own layer and rotated
    orbit = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    od = ImageDraw.Draw(orbit)
    ew, eh = s * 0.40 * scale, s * 0.15 * scale
    od.ellipse([c - ew, c - eh, c + ew, c + eh], outline=SOFT, width=w)
    # moon sits on the orbit (drawn before rotating so both turn together)
    mx, my = c + ew * math.cos(math.radians(160)), c + eh * math.sin(math.radians(160))
    mr = s * 0.05 * scale
    od.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=WHITE)
    orbit = orbit.rotate(28, resample=Image.BICUBIC, center=(c, c))
    img.alpha_composite(orbit)
    d.ellipse([c - r, c - r, c + r, c + r], fill=WHITE)
    # signal arcs (top right)
    ax, ay = c + s * 0.20 * scale, c - s * 0.20 * scale
    for i, rad in enumerate((0.10, 0.17)):
        rr = s * rad * scale
        d.arc([ax - rr, ay - rr, ax + rr, ay + rr], start=-80, end=-10, fill=WHITE, width=w)
    return img.resize((size, size), Image.LANCZOS)


def rounded_mask(size, radius):
    m = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size * SS - 1, size * SS - 1], radius=radius * SS, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def legacy(size):
    bg = gradient(size)
    shadow = symbol(size, 1.0).filter(ImageFilter.GaussianBlur(size * 0.02))
    dark = Image.new("RGBA", (size, size), (20, 20, 60, 0))
    dark.putalpha(shadow.getchannel("A").point(lambda v: v // 3))
    bg.alpha_composite(dark, (0, max(1, size // 64)))
    bg.alpha_composite(symbol(size, 1.0))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(bg, (0, 0), rounded_mask(size, int(size * 0.22)))
    return out


def main():
    dens = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}
    for name, k in dens.items():
        d = os.path.join(RES, "mipmap-" + name)
        os.makedirs(d, exist_ok=True)
        legacy(int(48 * k)).save(os.path.join(d, "ic_launcher.png"))
        # adaptive foreground: 108dp canvas, symbol kept inside the 66dp safe zone
        symbol(int(108 * k), 0.62).save(os.path.join(d, "ic_launcher_foreground.png"))
    any_dir = os.path.join(RES, "mipmap-anydpi-v26")
    os.makedirs(any_dir, exist_ok=True)
    with open(os.path.join(any_dir, "ic_launcher.xml"), "w") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <background android:drawable="@drawable/ic_launcher_background"/>\n'
                '    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>\n'
                '    <monochrome android:drawable="@mipmap/ic_launcher_foreground"/>\n'
                '</adaptive-icon>\n')
    with open(os.path.join(RES, "drawable", "ic_launcher_background.xml"), "w") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<shape xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <gradient android:angle="315" android:startColor="#FF3D7BFF" android:endColor="#FF7C4DFF"/>\n'
                '</shape>\n')
    docs = os.path.join(ROOT, "..", "docs")
    os.makedirs(docs, exist_ok=True)
    legacy(512).save(os.path.join(docs, "icon.png"))
    print("icons written")


if __name__ == "__main__":
    main()
