"""Make a simple, tasteful cover image for books whose EPUB has none, and the app icon."""
import hashlib
import io

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

# (background, accent) pairs; one is picked from the book's title so each book keeps its own look
PALETTE = [("#12343B", "#2DD4BF"), ("#1F2A44", "#7DD3FC"), ("#3B1F2B", "#F9A8D4"),
           ("#2D3B1F", "#BEF264"), ("#3A2A12", "#FCD34D"), ("#2B1F44", "#C4B5FD")]
BOLD_FONTS = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
REGULAR_FONTS = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def _font(size, bold=True):
    for path in (BOLD_FONTS if bold else REGULAR_FONTS):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def make_cover(title, author="", size=(600, 900)):
    """Return JPEG bytes for a generated cover."""
    w, h = size
    bg, accent = PALETTE[int(hashlib.sha1((title or "").encode("utf-8")).hexdigest(), 16) % len(PALETTE)]
    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    top = Image.new("RGB", size, "#000000")
    mask = Image.linear_gradient("L").resize(size)  # black at top, white at bottom
    img = Image.composite(top, img, mask.point(lambda v: int(v * 0.45)))
    draw = ImageDraw.Draw(img)

    margin = int(w * 0.1)
    max_w = w - 2 * margin
    draw.rectangle([margin, int(h * 0.14), margin + int(w * 0.2), int(h * 0.14) + 10], fill=accent)

    text = (title or "Untitled").strip()
    for fs in range(int(w * 0.17), 28, -4):
        font = _font(fs)
        lines = _wrap(draw, text, font, max_w)
        line_h = int(fs * 1.18)
        if len(lines) * line_h <= h * 0.5 and all(draw.textlength(l, font=font) <= max_w for l in lines):
            break
    y = int(h * 0.19)
    for line in lines:
        draw.text((margin, y), line, font=font, fill="#FFFFFF")
        y += line_h

    if author:
        afont = _font(int(w * 0.065), bold=False)
        alines = _wrap(draw, author.strip(), afont, max_w)
        ay = int(h * 0.92) - len(alines) * int(w * 0.08)
        for line in alines:
            draw.text((margin, ay), line, font=afont, fill=accent)
            ay += int(w * 0.08)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


import re

LABEL_RE = re.compile(r"^(chapter|part|book|section|act|volume|episode)\s+([0-9]+|[ivxlc]+|[a-z]+(?:[- ][a-z]+)?)\b\s*[:.\-\u2013\u2014]?\s*(.*)$",
                      re.IGNORECASE)


def card_label(number, total, title):
    """("CHAPTER 3", "Three Bells"): the label and the title without it. Titles that do not name
    themselves ("Prologue", "The Storm") are labelled by position: "3 OF 14"."""
    t = (title or "").strip()
    m = LABEL_RE.match(t)
    if m and m.group(3).strip() and len(m.group(2)) <= 12:
        return f"{m.group(1)} {m.group(2)}".upper(), m.group(3).strip()
    if m and not m.group(3).strip():
        return t.upper(), ""
    return (f"{number} OF {total}" if total else f"CHAPTER {number}"), t


def make_chapter_card(number, title, book_title="", cover=None, size=1000, total=None):
    """JPEG bytes for a chapter's picture: the book's cover blurred and darkened as background (or the
    generated colours when there is no cover), the cover itself as a small thumbnail, "CHAPTER n" and the title.
    cover: JPEG/PNG bytes of the book's cover, or None."""
    w = h = size
    bg, accent = PALETTE[int(hashlib.sha1((book_title or title or "").encode("utf-8")).hexdigest(), 16) % len(PALETTE)]
    thumb = None
    if cover:
        try:
            src = Image.open(io.BytesIO(cover)).convert("RGB")
            img = ImageOps.fit(src, (w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(w * 0.03))
            img = Image.blend(img, Image.new("RGB", (w, h), "#000000"), 0.62)
            thumb = ImageOps.contain(src, (int(w * 0.26), int(h * 0.38)), Image.LANCZOS)
        except Exception:  # noqa: BLE001  (an odd cover image must never stop a conversion)
            img = None
    else:
        img = None
    if img is None:
        img = Image.new("RGB", (w, h), bg)
        shade = Image.linear_gradient("L").resize((w, h)).point(lambda v: int(v * 0.45))
        img = Image.composite(Image.new("RGB", (w, h), "#000000"), img, shade)
    draw = ImageDraw.Draw(img)
    margin = int(w * 0.09)
    max_w = w - 2 * margin
    if thumb is not None:
        img.paste(thumb, (w - margin - thumb.width, margin))
        draw.rectangle([w - margin - thumb.width - 2, margin - 2, w - margin + 1, margin + thumb.height + 1],
                       outline=accent, width=2)
    label, text = card_label(number, total, title)
    if not text:                                                 # a title that is only "Chapter One"
        text = label.title()
        label = f"{number} OF {total}" if total else ""
    lfont = _font(int(w * 0.05))
    draw.rectangle([margin, int(h * 0.50), margin + int(w * 0.16), int(h * 0.50) + 8], fill=accent)
    draw.text((margin, int(h * 0.50) + 24), label, font=lfont, fill=accent)
    top = int(h * 0.50) + 24 + int(w * 0.09)
    room = h - top - int(h * 0.12 if book_title else h * 0.06)
    for fs in range(int(w * 0.115), 24, -4):
        font = _font(fs)
        lines = _wrap(draw, text, font, max_w)
        if len(lines) * int(fs * 1.2) <= room and all(draw.textlength(l, font=font) <= max_w for l in lines):
            break
    y = top
    for line in lines[:5]:
        draw.text((margin, y), line, font=font, fill="#FFFFFF")
        y += int(fs * 1.2)
    if book_title:
        bfont = _font(int(w * 0.04), bold=False)
        bt = book_title.strip()
        while draw.textlength(bt, font=bfont) > max_w and len(bt) > 8:
            bt = bt[:-2].rstrip() + "\u2026"
        draw.text((margin, h - int(h * 0.09)), bt, font=bfont, fill="#D0D5DB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def make_icon_image(size=256):
    """The app icon: a teal rounded square with headphones."""
    s = size * 4  # draw large, then shrink for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill="#0F766E")
    white = "#FFFFFF"
    lw = int(s * 0.07)
    # headband
    d.arc([int(s * 0.20), int(s * 0.17), int(s * 0.80), int(s * 0.77)], start=180, end=360, fill=white, width=lw)
    d.line([(int(s * 0.20), int(s * 0.47)), (int(s * 0.20), int(s * 0.60))], fill=white, width=lw)
    d.line([(int(s * 0.80), int(s * 0.47)), (int(s * 0.80), int(s * 0.60))], fill=white, width=lw)
    # ear cups
    d.rounded_rectangle([int(s * 0.14), int(s * 0.56), int(s * 0.34), int(s * 0.82)], radius=int(s * 0.06), fill=white)
    d.rounded_rectangle([int(s * 0.66), int(s * 0.56), int(s * 0.86), int(s * 0.82)], radius=int(s * 0.06), fill=white)
    return img.resize((size, size), Image.LANCZOS)


def save_icon(path):
    make_icon_image(256).save(path, format="ICO",
                              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
