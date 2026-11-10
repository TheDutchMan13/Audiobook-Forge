"""Read a DRM-free EPUB into a clean list of chapters ready for text-to-speech.

Standard library only. Usage:
    python epub_reader.py "book.epub"            # list chapters, write text to work/<book>/
"""
import html
import posixpath
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Sections that should not be read aloud (matched against the lowercased title).
SKIP_TITLES = (
    "notes", "index", "about the authors", "copyright", "contents",
    "timeline", "acknowledgments", "acknowledgements", "also by", "dedication",
)
MIN_CHARS = 1500  # ignore tiny pages (cover, part dividers, half-title)...
MIN_STORY_WORDS = 40     # ...unless it is a short prologue, epilogue or chapter with real text
SPLIT_WORDS = 12000      # a document longer than this (about 80 minutes) is split at its headings
JUNK_TITLES = {"pages", "page", "text", "untitled", "content", "contents1", "section", "body", "document", "chapter",
               "main", "index", "default", "split", "html", "xhtml"}

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}


@dataclass
class Chapter:
    title: str
    paragraphs: list = field(default_factory=list)
    notes: list = field(default_factory=list)      # footnote texts referenced in this chapter, in order

    @property
    def text(self):
        return "\n\n".join(self.paragraphs)

    @property
    def words(self):
        return len(self.text.split())


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(el, name):
    for k, v in el.attrib.items():
        if k.rsplit("}", 1)[-1] == name:
            return v
    return None


def _sentence_case_caps(text):
    """'JON FAVREAU WASN'T JUST' -> 'Jon Favreau Wasn't Just' (avoids spelled-out acronyms)."""
    out = []
    for w in text.split(" "):
        if w.isupper() and len(w) > 1 and "." not in w:
            w = w[0] + w[1:].lower()
        out.append(w)
    return " ".join(out)


def _is_note_marker(node, tag):
    """A footnote reference that is only a number or symbol (a raised 12, a linked *). Note links that
    wrap real words are kept, because the words are part of the sentence."""
    text = "".join(node.itertext()).strip()
    if not text or len(text) > 6:
        return False
    if not re.fullmatch(r"[\d*\u2020\u2021\u00a7,\u2013\-\[\]()ivxIVX]+", text):
        return False
    if tag == "sup":
        return True
    kind = ((_attr(node, "type") or "") + " " + (_attr(node, "role") or "") + " " + (_attr(node, "class") or "")).lower()
    return "noteref" in kind or "footnote" in kind or "endnote" in kind


def _block_text(el):
    """Text of one block element, joining drop-cap spans and tidying small-caps lead-ins."""
    parts = []
    drop = []  # pending drop-cap letter(s), joined to the small-caps lead-in that follows

    def walk(node):
        cls = (_attr(node, "class") or "")
        tag = _local(node.tag)
        if tag == "span" and _attr(node, "type") == "pagebreak":
            pass  # page-break marker, no text
        elif tag == "sup" and "".join(node.itertext()).strip() in ("2", "3") and \
                re.search(r"(?:^|[\s(=+\-*/\u00d7])[A-Za-z]{1,3}$", "".join(parts)):
            parts.append(" squared" if "".join(node.itertext()).strip() == "2" else " cubed")   # mc2, km2
        elif tag in ("sup", "a") and _is_note_marker(node, tag):
            pass  # footnote number, not read aloud
        elif tag == "span" and "dropcap" in cls:
            drop.append("".join(node.itertext()))
        elif tag == "span" and "san-cap" in cls:
            parts.append(_sentence_case_caps("".join(drop) + "".join(node.itertext())))
            drop.clear()
        else:
            if node.text:
                parts.append(node.text)
            for child in node:
                walk(child)
        if node.tail and node is not el:
            parts.append(node.tail)

    walk(el)
    if drop:  # drop cap with no small-caps lead-in after it
        parts.insert(0, "".join(drop))
    text = "".join(parts)
    text = html.unescape(text)
    text = re.sub(r"\s*\.\s\.\s\.\s*", "… ", text)  # ". . ." -> ellipsis
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_xhtml(raw):
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        # Fall back: strip the doctype/entities that break the XML parser.
        cleaned = re.sub(rb"<!DOCTYPE[^>]*>", b"", raw)
        return ET.fromstring(cleaned)


SKIP_TITLES_EXTRA = ("bibliography", "references", "endnotes", "permissions", "credits", "praise for",
                     "title page", "table of contents", "list of illustrations", "list of figures", "copyright page",
                     "about the author", "about the publisher", "books by", "other books", "newsletter", "sign up")
SKIP_TYPES = {"toc", "index", "copyright-page", "colophon", "titlepage", "halftitlepage", "dedication",
              "acknowledgments", "bibliography", "endnotes", "footnotes", "rearnotes", "loi", "lot", "landmarks",
              "imprint", "seriespage", "other-credits", "contributors", "errata", "cover", "frontispiece"}
KEEP_TYPES = {"chapter", "prologue", "epilogue", "introduction", "preface", "foreword", "part", "afterword",
              "conclusion", "bodymatter"}


def _toc_titles(z, opf, base, manifest_items):
    """{file path inside the zip: title} from the book's own table of contents (EPUB 3 nav or EPUB 2 NCX).
    The first entry pointing at a file wins, so a chapter split over several files keeps one name."""
    from urllib.parse import unquote
    titles = {}

    def add(href, label, rel_base):
        label = re.sub(r"\s+", " ", html.unescape(label or "")).strip()
        if not href or not label:
            return
        path = posixpath.normpath(posixpath.join(rel_base, unquote(href.split("#", 1)[0])))
        titles.setdefault(path, label)

    nav = next((i for i in manifest_items if "nav" in (i.get("properties") or "").split()), None)
    if nav is not None:
        nav_path = posixpath.join(base, unquote(nav["href"])) if base else unquote(nav["href"])
        try:
            root = _parse_xhtml(z.read(nav_path))
            for el in root.iter():
                if _local(el.tag) == "nav" and (_attr(el, "type") or "toc") == "toc":
                    for a in el.iter():
                        if _local(a.tag) == "a":
                            add(_attr(a, "href"), "".join(a.itertext()), posixpath.dirname(nav_path))
                    break
        except Exception:  # noqa: BLE001
            pass
    if not titles:
        ncx = next((i for i in manifest_items if i.get("media-type") == "application/x-dtbncx+xml"), None)
        if ncx is not None:
            ncx_path = posixpath.join(base, unquote(ncx["href"])) if base else unquote(ncx["href"])
            try:
                root = ET.fromstring(z.read(ncx_path))
                for point in root.iter():
                    if _local(point.tag) != "navPoint":
                        continue
                    label = next((t for t in point.iter() if _local(t.tag) == "text"), None)
                    content = next((c for c in point if _local(c.tag) == "content"), None)
                    if label is not None and content is not None:
                        add(content.attrib.get("src"), label.text, posixpath.dirname(ncx_path))
            except Exception:  # noqa: BLE001
                pass
    return titles


def _first_heading(root):
    for el in root.iter():
        if _local(el.tag) in ("h1", "h2", "h3"):
            text = _block_text(el)
            if text and len(text) <= 120:
                return text
    return None


def _doc_types(root):
    """epub:type values of the body and its outer wrappers (not of notes sections inside a chapter)."""
    body = next((e for e in root.iter() if _local(e.tag) == "body"), None)
    types, el = set(), body
    for _ in range(3):
        if el is None:
            break
        types.update((_attr(el, "type") or "").lower().split())
        kids = [c for c in el if _local(c.tag) in ("section", "div", "article")]
        el = kids[0] if len(kids) == 1 else (kids[0] if kids and el is body else None)
    return types


def junk_title(title):
    """True for a 'title' that is really a file or anchor name: part0001, 51248-0012, index_split_003, Pages."""
    t = (title or "").strip()
    if not t:
        return True
    low = t.lower()
    if low.endswith((".html", ".xhtml", ".htm", ".xml")):
        return True
    if low in JUNK_TITLES or re.fullmatch(r"(?:x?html?|page|pages|text|split|sect(?:ion)?)[\W_]*\d*", low):
        return True
    if " " not in t and re.search(r"\d", t) and re.search(r"[A-Za-z_\-]", t) and \
            not re.fullmatch(r"(?:chapter|part|book|act)\d{1,3}", low):
        return bool(re.search(r"\d{3,}|[_\-]\d|\d[_\-]|\.x?html?$", low) or re.fullmatch(r"[a-z]+\d+", low))
    return False


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


CITE_RE = re.compile(r"\bIbid\b|\bop\. cit|https?://|\bdoi\b|\bRetrieved\b|"
                     r"(?=.*\b(?:1[5-9]|20)\d{2}\b)(?=.*(?:\b[A-Z][a-z]+, [A-Z][a-z]*\.|\bPress\b|\bUniversity\b|"
                     r"\bpp?\.\s*\d|\bVol\.|\beds?\.|\bPublish|\bJournal\b|\bBooks\b))")


def looks_like_matter(paragraphs, root, position):
    """Why this document is front or back matter rather than story, or None. Judged by how it looks,
    for books that do not name their sections: mostly links (a contents page), page-number lists (an index),
    copyright lines, or numbered notes at the back of the book."""
    n = len(paragraphs)
    if not n:
        return None
    text = " ".join(paragraphs)
    words = len(text.split())
    link_chars = sum(len("".join(a.itertext())) for a in root.iter() if _local(a.tag) == "a")
    if link_chars > 0.5 * max(1, len(text)):
        return "mostly links (contents page)"
    if words < 1500 and re.search(r"\bISBN\b|All rights reserved|©\s*\d{4}|Copyright\s+©|Library of Congress",
                                  text, re.IGNORECASE):
        return "copyright page"
    index_like = sum(1 for p in paragraphs if re.search(r"\d+(?:\s*[,–\-]\s*\d+)+\.?\s*$", p) and len(p.split()) <= 25)
    if n >= 15 and index_like > 0.4 * n:
        return "page-number list (index)"
    back = position > 0.6
    numbered = sum(1 for p in paragraphs if re.match(r"^\s*\d{1,3}[.)\s]", p))
    cites = sum(1 for p in paragraphs if CITE_RE.search(p))
    if back and n >= 8 and numbered > 0.5 * n and cites > 0.3 * n:
        return "numbered notes"
    if back and n >= 10 and cites > 0.6 * n and sum(len(p.split()) for p in paragraphs) / n < 45:
        return "list of sources"
    return None


NOTE_TYPES = {"footnote", "footnotes", "endnote", "endnotes", "rearnote", "rearnotes", "note", "notes"}
NOTE_ROLES = {"doc-footnote", "doc-endnote", "doc-endnotes", "doc-footnotes"}


def _is_note_container(el):
    types = set((_attr(el, "type") or "").lower().split())
    role = (_attr(el, "role") or "").lower()
    cls = (_attr(el, "class") or "").lower()
    if types & NOTE_TYPES or role in NOTE_ROLES:
        return True
    return _local(el.tag) in ("aside", "section", "div", "ol", "ul") and \
        any(w in cls for w in ("footnote", "endnote"))


def _note_refs(root, skip):
    """Hrefs of footnote references in reading order (outside the notes themselves)."""
    refs = []

    def walk(node, in_sup=False):
        if node in skip:
            return
        tag = _local(node.tag)
        if tag == "a" and "#" in (_attr(node, "href") or ""):
            kind = ((_attr(node, "type") or "") + " " + (_attr(node, "role") or "")).lower()
            text = "".join(node.itertext()).strip()
            numeric = bool(re.fullmatch(r"\[?[\d*\u2020\u2021\u00a7ivxIVX]{1,5}\]?", text))
            has_sup = in_sup or any(_local(c.tag) == "sup" for c in node.iter())
            if "noteref" in kind or (numeric and has_sup):
                refs.append(_attr(node, "href"))
                return
        for child in node:
            walk(child, in_sup or tag == "sup")
    walk(root)
    return refs


def _note_text(el):
    blocks = [b for b in el.iter() if _local(b.tag) in BLOCK_TAGS
              and not any(_local(c.tag) in BLOCK_TAGS for c in b.iter() if c is not b)]   # innermost only
    texts = [_block_text(b) for b in blocks] if blocks else [_block_text(el)]
    text = " ".join(t for t in texts if t)
    text = re.sub(r"^\s*(?:\[?\d{1,4}\]?[.):]?|[*\u2020\u2021\u00a7]+)\s+", "", text)        # the note's own number
    text = re.sub(r"\s*[\u21a9\u2191\u2934\ufe0e\ufe0f]+\s*$", "", text)                     # back-link arrows
    text = re.sub(r"\s*\b(?:Return to text|Back to text|Back)\.?$", "", text, flags=re.IGNORECASE)
    return text.strip()


def read_epub(path, report=None):
    """report: optional list that receives (title, reason) for every section that is skipped."""
    z = zipfile.ZipFile(path)
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = next(e.attrib["full-path"] for e in container.iter() if _local(e.tag) == "rootfile")
    opf = ET.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)

    meta = {}
    for e in opf.iter():
        t = _local(e.tag)
        if t in ("title", "creator", "language") and t not in meta:
            meta[t] = (e.text or "").strip()

    from urllib.parse import unquote
    items = [e.attrib for e in opf.iter() if _local(e.tag) == "item"]
    manifest = {i["id"]: i["href"] for i in items if "id" in i and "href" in i}
    spine = [e.attrib["idref"] for e in opf.iter() if _local(e.tag) == "itemref"
             and (e.attrib.get("linear", "yes") != "no")]
    toc = _toc_titles(z, opf, base, items)
    skip = report if report is not None else []
    parsed = {}

    def doc(path_in_zip):
        if path_in_zip not in parsed:
            try:
                r = _parse_xhtml(z.read(path_in_zip))
                parents = {c: par for par in r.iter() for c in par}
                ids = {_attr(e, "id"): e for e in r.iter() if _attr(e, "id")}
                parsed[path_in_zip] = (r, parents, ids)
            except Exception:  # noqa: BLE001
                parsed[path_in_zip] = None
        return parsed[path_in_zip]

    def resolve(href, here):
        """The block that holds a note, and its document."""
        target, _, frag = href.partition("#")
        path_in_zip = posixpath.normpath(posixpath.join(posixpath.dirname(here), unquote(target))) if target else here
        d = doc(path_in_zip)
        if not d or not frag:
            return None, None
        _, parents, ids = d
        el = ids.get(unquote(frag))
        if el is None:
            return None, None
        while _local(el.tag) in ("a", "span", "sup", "em", "i", "b", "strong") and el in parents:
            el = parents[el]                 # an anchor inside the note: use the block around it
        return el, path_in_zip

    chapters = []
    for n_doc, idref in enumerate(spine):
        if idref not in manifest:
            continue
        href = unquote(manifest[idref])
        doc_path = posixpath.normpath(posixpath.join(base, href) if base else href)
        d = doc(doc_path)
        if not d:
            continue
        root = d[0]
        # footnotes: note bodies are never read where they sit; they are collected (in reference order)
        # and can be read at the end of the chapter
        skip_els = set()
        for el in root.iter():
            if _is_note_container(el):
                skip_els.update(el.iter())
        notes, seen_notes = [], set()
        for href in _note_refs(root, skip_els):
            el, where = resolve(href, doc_path)
            if el is None or id(el) in seen_notes:
                continue
            seen_notes.add(id(el))
            text = _note_text(el)
            if text:
                notes.append(text)
            if where == doc_path:
                skip_els.update(el.iter())
        section = next((e for e in root.iter() if _local(e.tag) == "section" and _attr(e, "title")), None)
        title = toc.get(doc_path) or (_attr(section, "title") if section is not None else None) \
            or _first_heading(root) or Path(href).stem
        title = html.unescape(title).strip()

        paragraphs, heads = [], []
        for el in root.iter():
            if _local(el.tag) in BLOCK_TAGS and el not in skip_els:
                text = _block_text(el)
                if text:
                    if _local(el.tag) in ("h1", "h2") and len(text) <= 120:
                        heads.append(len(paragraphs))
                    paragraphs.append(text)
        if junk_title(title):
            better = _first_heading(root)
            title = better if better and not junk_title(better) else ""
        nt = _norm(title)
        while paragraphs and len(paragraphs[0].split()) <= 12 and _norm(paragraphs[0]) and \
                (_norm(paragraphs[0]) in nt or nt in _norm(paragraphs[0])):
            paragraphs = paragraphs[1:]        # the heading is read as the chapter title already

        types = _doc_types(root)
        if sum(len(p) for p in paragraphs) < MIN_CHARS:
            words = sum(len(p.split()) for p in paragraphs)
            storyish = types & {"chapter", "prologue", "epilogue"} or \
                re.search(r"\b(prologue|epilogue|chapter|interlude)\b", title, re.IGNORECASE)
            if not (storyish and words >= MIN_STORY_WORDS):
                if words >= MIN_STORY_WORDS:
                    skip.append((title or Path(href).stem, f"short page ({words} words)"))
                continue
        low = title.lower()
        chapterish = types & KEEP_TYPES - {"bodymatter"} or \
            re.match(r"^\s*(?:chapter|part|book|prologue|epilogue|act|interlude|\d+\b)", low)
        if not chapterish and any(s in low for s in SKIP_TITLES + SKIP_TITLES_EXTRA):
            skip.append((title, "its title"))     # ("Chapter Six: Post-Credits Scene" was once dropped for "credits")
            continue
        if types & SKIP_TYPES and not types & KEEP_TYPES:
            skip.append((title, "marked as " + ", ".join(sorted(types & SKIP_TYPES))))
            continue
        reason = None if types & KEEP_TYPES else looks_like_matter(paragraphs, root, n_doc / max(1, len(spine) - 1))
        if reason:
            skip.append((title, reason))
            continue
        chapters.extend(split_long(title, paragraphs, heads, notes))
    for n, ch in enumerate(chapters, 1):
        if not ch.title:
            ch.title = f"Chapter {n}"           # better read aloud than "part0001"
    meta["skipped"] = list(skip)
    return meta, chapters


def split_long(title, paragraphs, heads, notes):
    """One document, or several chapters when it is very long and has headings inside (some books put a whole
    part, or the whole book, in one file: one 150-minute "chapter" with no chapter marks in the audiobook)."""
    words = sum(len(p.split()) for p in paragraphs)
    cuts = [h for h in heads if h > 0]
    if words <= SPLIT_WORDS or len(cuts) < 1:
        return [Chapter(title, paragraphs, notes)]
    bounds = [0] + cuts + [len(paragraphs)]
    parts = []
    for a, b in zip(bounds, bounds[1:]):
        chunk = paragraphs[a:b]
        if not chunk:
            continue
        if a in heads and len(chunk) > 1:
            name, chunk = chunk[0], chunk[1:]
        else:
            name = title
        if parts and sum(len(p.split()) for p in chunk) < 150:
            parts[-1].paragraphs.extend(([name] if name != title else []) + chunk)   # a stray heading, not a chapter
            continue
        parts.append(Chapter(name, chunk, []))
    if len(parts) < 2:
        return [Chapter(title, paragraphs, notes)]
    parts[-1].notes = notes                      # the document's notes are read after its last part
    return parts


def read_cover(path):
    """Return (image_bytes, extension) for the book's cover, or None if it has none."""
    from urllib.parse import unquote
    try:
        z = zipfile.ZipFile(path)
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(e.attrib["full-path"] for e in container.iter() if _local(e.tag) == "rootfile")
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        items = [e.attrib for e in opf.iter() if _local(e.tag) == "item"]
        images = [i for i in items if i.get("media-type", "").startswith("image/")]
        pick = next((i for i in images if "cover-image" in i.get("properties", "").split()), None)
        if pick is None:
            cover_id = next((e.attrib.get("content") for e in opf.iter()
                             if _local(e.tag) == "meta" and e.attrib.get("name") == "cover"), None)
            pick = next((i for i in images if i.get("id") == cover_id), None)
        if pick is None:
            pick = next((i for i in images if "cover" in (i.get("id", "") + i.get("href", "")).lower()), None)
        if pick is None:
            return None
        href = unquote(pick["href"])
        data = z.read(posixpath.join(base, href) if base else href)
        ext = {"image/jpeg": ".jpg", "image/png": ".png"}.get(pick.get("media-type"), None)
        return (data, ext) if ext else None
    except Exception:  # a missing or odd cover must never stop a conversion
        return None


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._ -]+", "", s).strip()[:80] or "book"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python epub_reader.py "book.epub"')
    epub = Path(sys.argv[1])
    meta, chapters = read_epub(epub)
    print(f"{meta.get('title')} - {meta.get('creator')} ({meta.get('language')})")
    out_dir = Path("work") / safe_name(epub.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for i, ch in enumerate(chapters, 1):
        total += ch.words
        (out_dir / f"{i:02d} {safe_name(ch.title)}.txt").write_text(
            f"{ch.title}\n\n{ch.text}\n", encoding="utf-8")
        print(f"{i:2d}. {ch.title}  ({ch.words:,} words)")
    print(f"\n{len(chapters)} chapters, {total:,} words (~{total / 150 / 60:.1f} hours of audio)")
    print(f"Text written to: {out_dir.resolve()}")
