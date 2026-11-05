"""Accent colors for the Windows 11 (sv-ttk) theme.

The theme draws its accent blue into a sprite sheet (buttons, checkboxes, sliders, progress
bars) and a few color values. For another accent we make a recolored copy of the theme once,
cache it in the app folder, and load that copy instead of the original.
"""
import colorsys
import re
import shutil
from pathlib import Path

# name: (accent on the light theme, accent on the dark theme); None = the theme's own blue
ACCENTS = {
    "blue": None,
    "teal": ("#0F766E", "#2DD4BF"),
    "purple": ("#6D28D9", "#C4B5FD"),
    "orange": ("#C2410C", "#FDBA74"),
    "pink": ("#BE185D", "#F9A8D4"),
    "green": ("#15803D", "#86EFAC"),
}
ORIGINAL = {"light": "#005fb8", "dark": "#57c8ff"}
CACHE_VERSION = "1"


def accent_color(name, mode):
    """The hex color of an accent on the light or dark theme."""
    pair = ACCENTS.get(name)
    if not pair:
        return ORIGINAL[mode]
    return pair[0] if mode == "light" else pair[1]


def _hsv(hex_color):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)


def _make_mapper(original_hex, target_hex):
    """Return f(r, g, b) -> (r, g, b) that moves the theme's blues to the target accent."""
    _, sa, va = _hsv(original_hex)
    ht, st, vt = _hsv(target_hex)

    def f(r, g, b):
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if not (0.5 <= h <= 0.653 and s > 0.12):   # 180..235 degrees: the theme's blues only
            return r, g, b
        w = min(1.0, s / sa)                        # pale tints change less than full accent pixels
        s2 = min(1.0, s * st / sa)
        v2 = min(1.0, v * (1 + (vt / va - 1) * w))
        r2, g2, b2 = colorsys.hsv_to_rgb(ht, s2, v2)
        return round(r2 * 255), round(g2 * 255), round(b2 * 255)

    return f


def _recolor_png(path, mapper):
    from PIL import Image
    img = Image.open(path).convert("RGBA")
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a:
                px[x, y] = (*mapper(r, g, b), a)
    img.save(path)


def _recolor_tcl(path, mapper):
    text = path.read_text(encoding="utf-8")

    def sub(m):
        h = m.group(1)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return "#%02x%02x%02x" % mapper(r, g, b)

    path.write_text(re.sub(r"#([0-9a-fA-F]{6})\b", sub, text), encoding="utf-8")


def prepare(name, cache_dir):
    """Return the path of a recolored sv.tcl for this accent, or None to use the original theme."""
    pair = ACCENTS.get(name)
    if not pair:
        return None
    import sv_ttk
    src = Path(sv_ttk.__file__).parent
    dst = Path(cache_dir) / name
    marker = dst / ".ready"
    if marker.exists() and marker.read_text().strip() == CACHE_VERSION:
        return dst / "sv.tcl"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyc", "py.typed"))
    for mode, target in (("light", pair[0]), ("dark", pair[1])):
        mapper = _make_mapper(ORIGINAL[mode], target)
        _recolor_png(dst / "theme" / f"spritesheet_{mode}.png", mapper)
        _recolor_tcl(dst / "theme" / f"{mode}.tcl", mapper)
    marker.write_text(CACHE_VERSION)
    return dst / "sv.tcl"
