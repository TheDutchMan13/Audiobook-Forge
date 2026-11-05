"""Read a book in any supported format into (meta, chapters), the same shape as epub_reader.read_epub.

Supported: .epub, .txt, .md, .docx, .html/.htm, .pdf  (DRM-free files only).
Everything except PDF uses the standard library; PDF needs the pypdf package.
"""
import html
import re
import statistics
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from epub_reader import Chapter, read_cover, read_epub

SUPPORTED = {".epub", ".txt", ".md", ".markdown", ".docx", ".html", ".htm", ".pdf"}
UNSUPPORTED_HINT = {
    ".mobi": "Kindle .mobi files are not supported. Convert the book to EPUB first (the free Calibre app does this).",
    ".azw3": "Kindle .azw3 files are not supported. Convert the book to EPUB first (the free Calibre app does this).",
    ".azw": "Kindle .azw files are not supported. Convert the book to EPUB first (the free Calibre app does this).",
    ".doc": "Old .doc files are not supported. Save the document as .docx first.",
    ".rtf": "RTF files are not supported. Save the document as .docx or .txt first.",
}
CHUNK_WORDS = 4000        # a book without chapter headings is cut into parts about this long
MIN_WORDS = 20            # ignore chapters shorter than this (stray headings, blank pages)

HEADING_RE = re.compile(
    r"^(chapter|part|book|section|prologue|epilogue|prelude|interlude|introduction|foreword|preface|afterword)\b[^\n]{0,70}$",
    re.IGNORECASE)
NUMBER_HEADING_RE = re.compile(r"^(\d{1,3}|[IVXLC]{1,7})[.)]?$")
MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


class BookError(Exception):
    """A problem with a book that is worth showing to the person, in plain words."""


def is_supported(path):
    return Path(path).suffix.lower() in SUPPORTED


def describe_unsupported(path):
    return UNSUPPORTED_HINT.get(Path(path).suffix.lower(), f"{Path(path).suffix or 'This'} files are not supported.")


def clean_title(stem):
    return re.sub(r"\s+", " ", re.sub(r"[_]+", " ", stem)).strip() or "Untitled"


# ---------------------------------------------------------------------------------- text -> chapters
def _norm_para(text):
    return re.sub(r"\s+", " ", text).strip()


def paragraphs_from_text(text):
    """Split plain text into paragraphs. Blank lines separate paragraphs; hard line wraps are joined."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("﻿", "")
    blocks = re.split(r"\n\s*\n", text)
    out = []
    for b in blocks:
        lines = [l.strip() for l in b.split("\n") if l.strip()]
        if not lines:
            continue
        # short lines that each look like a heading stay separate (a "Chapter 1" line directly above text)
        buf = []
        for line in lines:
            if len(line) <= 80 and (HEADING_RE.match(line) or MD_HEADING_RE.match(line) or NUMBER_HEADING_RE.match(line)) and not buf:
                out.append(line)
            elif len(line) <= 80 and (HEADING_RE.match(line) or MD_HEADING_RE.match(line)) and buf:
                out.append(_norm_para(" ".join(buf)))
                buf = []
                out.append(line)
            else:
                buf.append(line)
        if buf:
            out.append(_norm_para(" ".join(buf)))
    return out


def _heading_title(par, markdown=False):
    """Return the heading text if the paragraph is a chapter heading, else None."""
    if len(par) > 80:
        return None
    m = MD_HEADING_RE.match(par)
    if m:
        return m.group(2).strip()
    if markdown:
        return None
    if HEADING_RE.match(par) or NUMBER_HEADING_RE.match(par):
        return par.strip()
    return None


def chapters_from_paragraphs(paragraphs, doc_title, markdown=False, headings=None):
    """Group paragraphs into chapters by heading lines; fall back to equal-sized parts.

    headings: optional {paragraph_index: title} when the format knows its headings (docx, html).
    """
    marks = []
    for i, p in enumerate(paragraphs):
        t = headings.get(i) if headings is not None else _heading_title(p, markdown)
        if t:
            marks.append((i, t))
    # a document with a single heading and a big body is better cut into parts than kept as one blob
    chapters = []
    if len(marks) >= 2 or (len(marks) == 1 and marks[0][0] == 0 and len(paragraphs) > 1):
        first = marks[0][0]
        if first > 0:
            intro = paragraphs[:first]
            if sum(len(p.split()) for p in intro) >= 120:
                chapters.append(Chapter("Introduction", intro))
        for n, (i, t) in enumerate(marks):
            j = marks[n + 1][0] if n + 1 < len(marks) else len(paragraphs)
            body = paragraphs[i + 1:j]
            if body:
                chapters.append(Chapter(t, body))
    else:
        body = [p for p in paragraphs if not (headings is None and _heading_title(p, markdown))]
        chapters = _chunk(body, doc_title)
    chapters = [_finish(c) for c in chapters if c.words >= MIN_WORDS]
    # very long chapters read better as parts too
    final = []
    for c in chapters:
        if c.words > CHUNK_WORDS * 2.5:
            parts = _chunk(c.paragraphs, c.title)
            final.extend(parts)
        else:
            final.append(c)
    return final


def split_long(par, max_words=220):
    """Split a very long paragraph at sentence ends into pieces of roughly max_words."""
    if len(par.split()) <= max_words * 1.4:
        return [par]
    sentences = re.split(r"(?<=[.!?\u201d\"')])\s+(?=[A-Z\u201c\"'(])", par)
    out, cur, n = [], [], 0
    for sen in sentences:
        cur.append(sen)
        n += len(sen.split())
        if n >= max_words:
            out.append(" ".join(cur))
            cur, n = [], 0
    if cur:
        out.append(" ".join(cur))
    return out


def _finish(ch):
    ch.paragraphs = [piece for p in ch.paragraphs for piece in split_long(p)]
    return ch


def _chunk(paragraphs, title):
    total = sum(len(p.split()) for p in paragraphs)
    if total <= CHUNK_WORDS * 1.3:
        return [Chapter(title, list(paragraphs))] if paragraphs else []
    n_parts = max(2, round(total / CHUNK_WORDS))
    target = total / n_parts
    parts, cur, count = [], [], 0
    for p in paragraphs:
        cur.append(p)
        count += len(p.split())
        if count >= target and len(parts) < n_parts - 1:
            parts.append(cur)
            cur, count = [], 0
    if cur:
        parts.append(cur)
    return [Chapter(f"{title}, part {i}", part) for i, part in enumerate(parts, 1)]


def read_text_file(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    markdown = Path(path).suffix.lower() in (".md", ".markdown")
    if markdown:  # drop emphasis marks, link targets and fences that would be read aloud
        text = re.sub(r"```.*?```", " ", text, flags=re.S)
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"(\*\*|__|\*|_|`)", "", text)
        text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    paragraphs = paragraphs_from_text(text)
    title = clean_title(Path(path).stem)
    if markdown and paragraphs:  # a single top-level "# Title" at the top names the book
        h1 = [p for p in paragraphs if re.match(r"^#\s+", p)]
        if len(h1) == 1 and paragraphs[0] == h1[0]:
            title = MD_HEADING_RE.match(paragraphs[0]).group(2).strip()
            paragraphs = paragraphs[1:]
    chapters = chapters_from_paragraphs(paragraphs, title, markdown=markdown)
    return {"title": title, "creator": "", "language": "en"}, chapters


# ---------------------------------------------------------------------------------- docx
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


NOTE_MARK_RE = re.compile("\ue000([fe])(-?\\d+)\ue001")


def _docx_notes(z, part, tag):
    """{id: text} of the footnotes or endnotes part of a Word document."""
    try:
        root = ET.fromstring(z.read(part))
    except (KeyError, ET.ParseError):
        return {}
    out = {}
    for note in root.iter(W + tag):
        if note.get(W + "type") in ("separator", "continuationSeparator", "continuationNotice"):
            continue
        text = _norm_para("".join(t.text or "" for t in note.iter(W + "t")))
        if text:
            out[note.get(W + "id")] = text
    return out


def _attach_notes(chapters, notes):
    """Move the footnote markers left in the paragraphs into each chapter's notes list, in order."""
    for ch in chapters:
        found = []
        for kind, nid in NOTE_MARK_RE.findall(ch.title):
            text = notes.get(kind, {}).get(nid)
            if text and text not in found:
                found.append(text)
        for i, par in enumerate(ch.paragraphs):
            for kind, nid in NOTE_MARK_RE.findall(par):
                text = notes.get(kind, {}).get(nid)
                if text and text not in found:
                    found.append(text)
            ch.paragraphs[i] = _norm_para(NOTE_MARK_RE.sub("", par))
        ch.title = _norm_para(NOTE_MARK_RE.sub("", ch.title))
        ch.notes = found
    return chapters


def read_docx(path):
    try:
        z = zipfile.ZipFile(path)
        root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise BookError("That file could not be opened as a Word document.")
    meta = {"title": clean_title(Path(path).stem), "creator": "", "language": "en"}
    try:
        core = ET.fromstring(z.read("docProps/core.xml"))
        for e in core.iter():
            tag = e.tag.rsplit("}", 1)[-1]
            if tag == "title" and (e.text or "").strip():
                meta["title"] = e.text.strip()
            elif tag == "creator" and (e.text or "").strip():
                meta["creator"] = e.text.strip()
    except (KeyError, ET.ParseError):
        pass

    paras, levels = [], {}
    for p in root.iter(W + "p"):
        parts = []
        for node in p.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag in (W + "tab", W + "br"):
                parts.append(" ")
            elif node.tag in (W + "footnoteReference", W + "endnoteReference"):
                parts.append("\ue000" + ("f" if node.tag == W + "footnoteReference" else "e") + str(node.get(W + "id")) + "\ue001")
        text = _norm_para("".join(parts))
        if not NOTE_MARK_RE.sub("", text).strip():
            continue
        style = ""
        ppr = p.find(W + "pPr")
        if ppr is not None:
            ps = ppr.find(W + "pStyle")
            if ps is not None:
                style = ps.get(W + "val", "")
        m = re.match(r"(?i)heading\s*(\d)", style)
        if style.lower() == "title" and meta["title"] == clean_title(Path(path).stem):
            meta["title"] = text
            continue
        if m:
            levels[len(paras)] = int(m.group(1))
        paras.append(text)
    if not paras:
        raise BookError("That Word document has no text in it.")
    headings = None
    if levels:
        top = min(levels.values())
        headings = {i: paras[i] for i, lv in levels.items() if lv == top}
    chapters = chapters_from_paragraphs(paras, meta["title"], headings=headings) if headings else \
        chapters_from_paragraphs(paras, meta["title"])
    notes = {"f": _docx_notes(z, "word/footnotes.xml", "footnote"), "e": _docx_notes(z, "word/endnotes.xml", "endnote")}
    return meta, _attach_notes(chapters, notes)


# ---------------------------------------------------------------------------------- html
class _HTMLText(HTMLParser):
    BLOCK = {"p", "li", "blockquote", "div", "br", "tr", "section", "article"}
    HEAD = {"h1", "h2"}
    SKIP = {"script", "style", "head", "nav", "footer", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras, self.headings, self.title = [], {}, ""
        self.buf, self.skip, self.in_title, self.head_tag = [], 0, False, None

    def _flush(self):
        text = _norm_para("".join(self.buf))
        self.buf = []
        if text:
            if self.head_tag:
                self.headings[len(self.paras)] = text
            self.paras.append(text)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP and tag != "head":
            self.skip += 1
        if tag == "head":
            self.skip += 1
        if self.skip:
            return
        if tag in self.HEAD:
            self._flush()
            self.head_tag = tag
        elif tag in self.BLOCK or tag in ("h3", "h4", "h5", "h6"):
            self._flush()

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in self.HEAD:
            self._flush()
            self.head_tag = None
        elif tag in self.BLOCK or tag in ("h3", "h4", "h5", "h6"):
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def read_html(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    parser = _HTMLText()
    parser.feed(text)
    parser.close()
    if not parser.paras:
        raise BookError("No readable text was found in that web page.")
    title = _norm_para(html.unescape(parser.title)) or clean_title(Path(path).stem)
    headings = parser.headings or None
    if headings and len(headings) == 1 and 0 in headings:   # one <h1> at the top = the page title
        title = headings[0]
        headings = None
        parser.paras = parser.paras[1:]
    chapters = chapters_from_paragraphs(parser.paras, title, headings=headings)
    return {"title": title, "creator": "", "language": "en"}, chapters


# ---------------------------------------------------------------------------------- pdf
def _repeated_lines(page_texts):
    """Short lines that repeat on many pages are running headers or footers."""
    counts = {}
    for t in page_texts:
        lines = [x.strip() for x in (t or "").replace("\r", "").split("\n")]
        for l in {x for x in lines if x and len(x) < 90}:
            norm = re.sub(r"\d+", "#", l)
            counts[norm] = counts.get(norm, 0) + 1
    n = len(page_texts)
    return {k for k, v in counts.items() if n >= 6 and v >= max(4, 0.4 * n)}


def _line_key(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def _pdf_paragraphs(page_texts, repeated=None, heading_keys=None, heading_marks=None):
    """Turn raw per-page PDF text into clean paragraphs: drop headers, footers and page numbers,
    repair hyphenated line breaks and join wrapped lines.
    heading_keys: line keys known to be headings (from font sizes); their paragraph numbers are
    added to heading_marks."""
    pages = []
    for t in page_texts:
        pages.append([l.rstrip() for l in (t or "").replace("\r", "").split("\n")])
    if repeated is None:
        repeated = _repeated_lines(page_texts)
    all_lens = [len(l.strip()) for lines in pages for l in lines if len(l.strip()) > 25]
    typical = statistics.median(all_lens) if all_lens else 70

    paras, cur = [], ""
    def end_para():
        nonlocal cur
        if cur.strip():
            paras.append(_norm_para(cur))
        cur = ""

    for lines in pages:
        for raw in lines:
            line = raw.strip()
            if not line:
                end_para()
                continue
            if re.fullmatch(r"(page\s*)?[-–—]?\s*\d{1,4}\s*[-–—]?", line, re.I):
                continue                                   # a page number
            if re.sub(r"\d+", "#", line) in repeated:
                continue                                   # a running header or footer
            if heading_keys and _line_key(line) in heading_keys:
                end_para()
                if heading_marks is not None and paras and (len(paras) - 1) in heading_marks:
                    paras[-1] = paras[-1] + " " + line          # a heading set over two lines
                else:
                    paras.append(line)
                    if heading_marks is not None:
                        heading_marks.add(len(paras) - 1)
                continue
            if _heading_title(line) and not cur.strip():
                end_para()
                paras.append(line)
                continue
            if cur.endswith("-") and not cur.endswith(" -") and line[:1].islower():
                cur = cur[:-1] + line                      # "informa-" + "tion" -> "information"
            elif cur:
                cur += " " + line
            else:
                cur = line
            # a short line that ends a sentence is the last line of its paragraph
            if len(line) < 0.62 * typical and re.search(r"[.!?”\"')]$", line):
                end_para()
    end_para()
    return paras


def _font_headings(reader, repeated):
    """Lines set in a clearly bigger font than the body text, scored like a reader would judge them.
    Returns a set of line keys, or an empty set when the PDF gives no clear signal."""
    lines = []                 # (page, text, size, every run bold)
    weight = {}                # font size -> characters set in it
    for pi, page in enumerate(reader.pages):
        runs = []

        def visit(text, cm, tm, font_dict, font_size, runs=runs):
            if not text:
                return
            try:
                scale = abs(float(tm[3])) or abs(float(tm[0])) or 1.0
                size = round(float(font_size) * scale, 1)
            except Exception:  # noqa: BLE001
                size = 0.0
            try:
                name = str((font_dict or {}).get("/BaseFont", ""))
            except Exception:  # noqa: BLE001
                name = ""
            runs.append((text, size, bool(re.search(r"bold|black|heavy|semibold|demi", name, re.I))))
        try:
            page.extract_text(visitor_text=visit)
        except Exception:  # noqa: BLE001
            return set()
        cur, cur_size, cur_bold = "", 0.0, True
        for text, size, bold in runs:
            pieces = text.split("\n")
            for n, piece in enumerate(pieces):
                if piece.strip():
                    cur += piece
                    cur_size = max(cur_size, size)
                    cur_bold = cur_bold and bold
                    weight[size] = weight.get(size, 0) + len(piece.strip())
                if n < len(pieces) - 1:
                    if cur.strip():
                        lines.append((pi, cur.strip(), cur_size, cur_bold))
                    cur, cur_size, cur_bold = "", 0.0, True
        if cur.strip():
            lines.append((pi, cur.strip(), cur_size, cur_bold))
    if not weight:
        return set()
    body = max(weight.items(), key=lambda kv: kv[1])[0]
    if body <= 0:
        return set()
    def plausible(text):
        if not (2 <= len(text) <= 80) or not re.search(r"[A-Za-z]", text):
            return False
        if re.sub(r"\d+", "#", text) in repeated:
            return False                                   # a running header in a big font
        if re.search(r"[.,;:]$", text) and not HEADING_RE.match(text):
            return False                                   # ends like a sentence, so probably a pull quote
        return True

    def believable(keys):                                  # at least two, and not most of the book
        return 2 <= len(keys) <= max(3, len(reader.pages))

    bigger = {_line_key(t) for _, t, size, _ in lines if size >= body * 1.25 and plausible(t)}
    if believable(bigger):
        return bigger
    # no size difference: whole lines in bold, about body size, short, are the next best sign
    bold = {_line_key(t) for _, t, size, b in lines if b and size >= body * 0.95 and len(t) <= 60 and plausible(t)}
    return bold if believable(bold) else set()


def read_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise BookError("PDF support needs one more package. In PowerShell run:\n"
                        "uv pip install --python runtime\\python\\python.exe --break-system-packages pypdf")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                raise BookError("That PDF is password protected.")
        page_texts = [(p.extract_text() or "") for p in reader.pages]
    except BookError:
        raise
    except Exception as e:  # noqa: BLE001
        raise BookError(f"That PDF could not be read ({e}).")
    if sum(len(t.strip()) for t in page_texts) < 200:
        raise BookError("That PDF has no readable text. It may be scanned pages, which need OCR first.")
    info = reader.metadata or {}
    meta = {"title": (getattr(info, "title", None) or "").strip() or clean_title(Path(path).stem),
            "creator": (getattr(info, "author", None) or "").strip(), "language": "en"}

    # the PDF's own bookmarks are the best chapter list when it has them
    marks = []
    try:
        def walk(items, depth=0):
            for it in items:
                if isinstance(it, list):
                    if depth == 0:
                        continue                           # sub-sections: top level only
                else:
                    try:
                        marks.append((reader.get_destination_page_number(it), str(it.title).strip()))
                    except Exception:  # noqa: BLE001
                        pass
        walk(reader.outline)
    except Exception:  # noqa: BLE001
        marks = []
    marks = sorted({(p, t) for p, t in marks if t and p is not None})
    repeated = _repeated_lines(page_texts)
    chapters = []
    if len(marks) >= 3:
        for n, (page, title) in enumerate(marks):
            end = marks[n + 1][0] if n + 1 < len(marks) else len(page_texts)
            paras = _pdf_paragraphs(page_texts[page:max(end, page + 1)], repeated)
            if paras and _norm_para(paras[0]).lower() == title.lower():
                paras = paras[1:]
            ch = Chapter(title, paras)
            if ch.words >= MIN_WORDS:
                chapters.append(_finish(ch))
    if not chapters:
        keys = _font_headings(reader, repeated)
        if keys:
            marks = set()
            paras = _pdf_paragraphs(page_texts, repeated, keys, marks)
            if len(marks) >= 2:
                chapters = chapters_from_paragraphs(paras, meta["title"], headings={i: paras[i] for i in marks})
    if not chapters:
        chapters = chapters_from_paragraphs(_pdf_paragraphs(page_texts, repeated), meta["title"])
    return meta, chapters


# ---------------------------------------------------------------------------------- entry points
def read_book(path):
    """Return (meta, chapters). Raises BookError with a plain-language message when it cannot."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".epub":
        try:
            return read_epub(path)
        except Exception as e:  # noqa: BLE001
            raise BookError(f"That EPUB could not be read ({e}). Only DRM-free EPUB files are supported.")
    if ext in (".txt", ".md", ".markdown"):
        meta, chapters = read_text_file(path)
    elif ext == ".docx":
        meta, chapters = read_docx(path)
    elif ext in (".html", ".htm"):
        meta, chapters = read_html(path)
    elif ext == ".pdf":
        meta, chapters = read_pdf(path)
    else:
        raise BookError(describe_unsupported(path))
    if not chapters:
        raise BookError("No readable text was found in that file.")
    return meta, chapters


def read_book_cover(path):
    """(bytes, ext) for a book that carries its own cover (EPUB only), else None."""
    if Path(path).suffix.lower() == ".epub":
        return read_cover(path)
    return None
