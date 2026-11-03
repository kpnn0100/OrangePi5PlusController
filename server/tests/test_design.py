#!/usr/bin/env python3
"""Every screen uses the one palette and the bundled fonts of docs/design.md (UX-01).

docs/design.md defines the colours. The colour tokens of each web UI (its :root block) and the
colours of each app's theme must be among them (translucent ones: a palette colour, black or
white with an alpha), the fonts must be bundled next to the pages, and no page may load
anything from the internet.
"""

import os
import re

from harness import check, run, test

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = {                                       # stylesheet -> its fonts folder
    "relay/arstro_relay/static/style.css": "relay/arstro_relay/static/fonts",
    "server/arstro_remote/web/static/app.css": "server/arstro_remote/web/static/fonts",
    "server/arstro_backup/static/backup.css": "server/arstro_backup/static/fonts",
    "client_app/android/app/src/main/assets/phone/static/app.css": "client_app/android/app/src/main/assets/phone/static/fonts",
}
THEMES = ["devices_app/lib/src/theme.dart", "client_app/lib/src/theme.dart", "app/lib/src/theme.dart"]
FONTS = ["Geist-Variable.woff2", "GeistMono-Variable.woff2", "LICENSE.Geist.txt"]


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def palette():
    return {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", read("docs/design.md"))}


def rgb(hexcode):
    return tuple(int(hexcode[i:i + 2], 16) for i in (1, 3, 5))


def allowed_rgba(r, g, b, pal):
    return (r, g, b) in {rgb(p) for p in pal} | {(0, 0, 0), (255, 255, 255)}


@test("UX-01")
def web_tokens_come_from_the_palette():
    pal = palette()
    check(len(pal) >= 15, "docs/design.md lost its token table")
    for css in WEB:
        root = re.search(r":root\s*\{(.*?)\}", read(css), re.S)
        check(root, "%s has no :root block" % css)
        block = root.group(1)
        stray = [h for h in re.findall(r"#[0-9a-fA-F]{6}\b", block) if h.lower() not in pal]
        check(not stray, "%s :root uses colours outside docs/design.md: %s" % (css, stray))
        for r, g, b in re.findall(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,", block):
            check(allowed_rgba(int(r), int(g), int(b), pal), "%s :root: rgba(%s, %s, %s) is not a palette colour" % (css, r, g, b))
        check(re.search(r"color-scheme:\s*dark\s*;", block), "%s is not dark-only" % css)


@test("UX-01")
def app_themes_come_from_the_palette():
    pal = palette()
    for dart in THEMES:
        text = read(dart)
        stray = []
        for argb in re.findall(r"Color\(0x([0-9a-fA-F]{8})\)", text):
            alpha, code = argb[:2].lower(), "#" + argb[2:].lower()
            if code in pal or (alpha != "ff" and allowed_rgba(*rgb(code), pal)):
                continue
            stray.append("0x" + argb)
        check(not stray, "%s uses colours outside docs/design.md: %s" % (dart, stray))


@test("UX-01")
def fonts_are_bundled_and_nothing_comes_from_the_internet():
    for css, folder in WEB.items():
        for f in FONTS:
            check(os.path.isfile(os.path.join(ROOT, folder, f)), "%s/%s missing" % (folder, f))
        text = read(css)
        check('font-family: "Geist"' in text and "fonts/Geist-Variable.woff2" in text, "%s does not load Geist" % css)
        check(not re.search(r"url\(\s*['\"]?(https?:)?//", text), "%s loads something from the internet" % css)
    for app in ("devices_app", "client_app", "app"):
        spec = read(app + "/pubspec.yaml")
        check(re.search(r"family:\s*Geist\b", spec), "%s/pubspec.yaml does not bundle Geist" % app)
        check("google_fonts" not in spec, "%s fetches fonts from the internet" % app)


@test("UX-01")
def no_gradient_logo_left():
    for page in ("relay/arstro_relay/static/index.html", "relay/arstro_relay/static/boot.html",
                 "server/arstro_remote/web/static/index.html"):
        text = read(page)
        check("%237c9cff" not in text.lower() and "%233b63e0" not in text.lower(), "%s still has the old blue mark" % page)
    for css in WEB:
        check("linear-gradient(135deg" not in read(css), "%s still has the gradient logo" % css)


if __name__ == "__main__":
    raise SystemExit(run(description=__doc__))
