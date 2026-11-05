"""Work out who says every line of dialogue in a book with the local language model (llm.py), and save it
next to the cast: casting\\<book>.speakers.json. Conversions then use these tags instead of guessing
(an explicit tag in the text, like "said Rhys" or "I said", still wins).

  python speakertag.py BOOK [--model best|fast] [--chapters 1-3] [--fresh] [--progress]

Progress lines: "@tag <done> <total> <chapter>", "@problem <message>", "@done <file>".
Tags are saved after every chapter, so stopping keeps what is done and running again carries on.
"""
import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import speakers  # noqa: E402

CASTING_DIR = HERE / "casting"
WINDOW_CHARS = 7000        # text per question (about 2,000 words): enough for a whole conversation
CONTEXT_PARAS = 6          # paragraphs before the window, shown so the model knows who is in the room
LONG_PARA = 900            # longer paragraphs are shortened in the middle (the tags are at the ends)
TITLES = re.compile(r"^(?:the\s+)?(?:(?:high\s+)?(?:lord|lady)|mr|mrs|ms|miss|dr|sir|madam|madame|king|queen|prince|"
                    r"princess|captain|professor|aunt|uncle|master|mistress|father|mother|sister|brother)\.?\s+", re.I)

SYSTEM = ("You read novels and work out who speaks each line of dialogue. You use the speech tags, who was "
          "addressed, who was just mentioned, and the back-and-forth of a conversation. You answer only with the "
          "list you are asked for.")


def out(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def is_dialogue(p):
    return bool(speakers.QUOTE_RE.search(p)) or p.lstrip().startswith(("“", '"'))


def shorten(p):
    p = re.sub(r"\s+", " ", p).strip()
    if len(p) <= LONG_PARA:
        return p
    return p[:LONG_PARA // 2].rsplit(" ", 1)[0] + " [...] " + p[-LONG_PARA // 2:].split(" ", 1)[-1]


def windows(paragraphs):
    """[(start, end)] spans of paragraphs, each about WINDOW_CHARS long, that contain dialogue."""
    spans, start, size = [], 0, 0
    for i, p in enumerate(paragraphs):
        size += min(len(p), LONG_PARA) + 1
        if size >= WINDOW_CHARS:
            spans.append((start, i + 1))
            start, size = i + 1, 0
    if start < len(paragraphs):
        spans.append((start, len(paragraphs)))
    return [(a, b) for a, b in spans if any(is_dialogue(p) for p in paragraphs[a:b])]


def describe(characters):
    words = {"f": "woman", "m": "man"}
    return ", ".join(f"{n} ({words[g]})" if g in words else n for n, g in characters) or "none yet"


def build_prompt(paragraphs, start, end, characters, title="", author=""):
    """(prompt, {number: paragraph index})"""
    numbers, lines = {}, []
    if start:
        lines.append("Earlier in the chapter (for context, no answer needed):")
        lines += [shorten(p) for p in paragraphs[max(0, start - CONTEXT_PARAS):start]]
        lines.append("")
        lines.append("The part to answer:")
    for i in range(start, end):
        p = shorten(paragraphs[i])
        if is_dialogue(paragraphs[i]):
            n = len(numbers) + 1
            numbers[n] = i
            lines.append(f"[{n}] {p}")
        else:
            lines.append(p)
    book = f"The book is {title}" + (f" by {author}" if author else "") + ". " if title else ""
    head = (f"{book}Characters met so far: {describe(characters)}.\n\n"
            "For every paragraph marked with a number like [3], say who speaks the words in quotation marks.\n"
            "Answer with one line per number, exactly like this:\n"
            "3: Name, woman\n"
            "Use the name the book uses most for that person (no titles), and woman, man or unknown. "
            "If the one telling the story (the \"I\" of the book) speaks, write: 3: I\n"
            "If you really cannot tell, write: 3: ?\n\n")
    return head + "\n".join(lines), numbers


LINE_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)\s*[:.)\-])\s*[:\-]?\s*(.+?)\s*$")


def parse_answer(answer):
    """{number: (name or "I" or None, "f"/"m"/None)}"""
    found = {}
    for line in answer.splitlines():
        m = LINE_RE.match(line.replace("**", ""))
        if not m:
            continue
        n, rest = int(m.group(1) or m.group(2)), m.group(3)
        parts = [x.strip(" .\"'()") for x in re.split(r"[,(|–—]", rest)]
        who = parts[0] if parts else ""
        gword = " ".join(parts[1:]).lower()
        g = "f" if re.search(r"\b(woman|female|f|girl)\b", gword) else "m" if re.search(r"\b(man|male|m|boy)\b", gword) else None
        if who in ("?", "", "unknown", "Unknown", "unclear", "Unclear"):
            who = None
        elif who.lower() in ("i", "narrator", "the narrator", "storyteller", "me"):
            who, g = "I", None
        found.setdefault(n, (who, g))
    return found


class Namer:
    """Turns a name the model wrote into one the rest of the program uses, or None if it is not in the book."""

    def __init__(self, guesser, all_text):
        self.guesser = guesser
        self.caps = Counter(re.findall(r"\b([A-Z][a-z'’-]+)", all_text))

    def __call__(self, who):
        if who is None:
            return None
        if who == "I":
            return speakers.NARRATOR
        who = TITLES.sub("", who.strip())
        for w in re.findall(r"[A-Z][A-Za-z'’-]+", who):
            base = speakers._base(w)
            if base.lower() in speakers.STOP_CAPS or base in ("I",):
                continue
            if self.caps[base] >= 2:
                return self.guesser.canon(base)
        return None


def tags_file(book_stem):
    from epub_reader import safe_name
    return CASTING_DIR / f"{safe_name(book_stem)}.speakers.json"


def load(book_stem):
    return speakers.load_ai_tags(book_stem, CASTING_DIR)


def load_path(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data.get("chapters"), dict) else None
    except Exception:  # noqa: BLE001
        return None


def tag_book(chapters, model, book_stem, title="", author="", picked=None, fresh=False, progress=None,
             stop=lambda: False, path=None):
    """Tag every dialogue paragraph; returns the saved data."""
    guesser = speakers.SpeakerGuesser(p for c in chapters for p in c.paragraphs)
    namer = Namer(guesser, "\n".join(p for c in chapters for p in c.paragraphs))
    path = Path(path) if path else tags_file(book_stem)
    data = None if fresh else load_path(path)
    if not data or data.get("model") != getattr(model, "name", ""):
        data = {"version": 1, "model": getattr(model, "name", ""), "chapters": {}, "votes": {}}
    votes = defaultdict(Counter, {k: Counter(v) for k, v in (data.get("votes") or {}).items()})
    lines = Counter()
    known = [(c["name"], c["gender"]) for c in speakers.find_characters(chapters, guesser)[:25]]

    def characters():
        names = dict(known)
        for name, n in lines.most_common(30):
            if name not in names:
                v = votes.get(name)
                names[name] = v.most_common(1)[0][0] if v else None
        return list(names.items())[:40]

    indices = [i for i in range(len(chapters)) if picked is None or i + 1 in picked]
    total = sum(1 for i in indices for p in chapters[i].paragraphs if is_dialogue(p))
    done = 0
    for i in indices:
        ch = chapters[i]
        key = speakers.chapter_key(ch.paragraphs)
        count = sum(1 for p in ch.paragraphs if is_dialogue(p))
        if key in data["chapters"]:
            for name, _ in data["chapters"][key].values():
                if name and name != speakers.NARRATOR:
                    lines[name] += 1
            done += count
            if progress:
                progress(done, total, i + 1)
            continue
        tags = {}
        for a, b in windows(ch.paragraphs):
            if stop():
                return data
            prompt, numbers = build_prompt(ch.paragraphs, a, b, characters(), title, author)
            answer = model.chat(SYSTEM, prompt, max_new_tokens=14 * len(numbers) + 40)
            got = parse_answer(answer)
            for n, idx in numbers.items():
                who, g = got.get(n, (None, None))
                name = namer(who)
                if name and name != speakers.NARRATOR:
                    lines[name] += 1
                    if g:
                        votes[name][g] += 1
                if name or g:
                    tags[str(idx)] = [name, None if name == speakers.NARRATOR else g]
            done += len(numbers)
            if progress:
                progress(done, total, i + 1)
        data["chapters"][key] = tags
        data["votes"] = {k: dict(v) for k, v in votes.items()}
        data["genders"] = {}
        for name, v in votes.items():
            g, n = v.most_common(1)[0]
            if n / sum(v.values()) >= 0.7:
                data["genders"][name] = g
        data["made"] = time.strftime("%Y-%m-%d %H:%M")
        save(path, data)
    return data


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def parse_range(text, n):
    if not text:
        return None
    picked = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            picked.update(range(int(a), int(b) + 1))
        elif part:
            picked.add(int(part))
    return {x for x in picked if 1 <= x <= n}


def compare(book, chapter, models, chapters, out_file):
    """Run each model on one chapter (a separate process each, so the graphics card is freed in between), time
    them, and list the lines they tag differently. Nothing is written to the book's real tags."""
    import subprocess
    import tempfile
    paragraphs = chapters[chapter - 1].paragraphs
    key = speakers.chapter_key(paragraphs)
    results = {}
    for m in models:
        tmp = Path(tempfile.gettempdir()) / ("forge-compare-" + re.sub(r"[^A-Za-z0-9._-]", "_", m) + ".json")
        tmp.unlink(missing_ok=True)
        out(f"--- {m}: reading chapter {chapter}...")
        t = time.time()
        r = subprocess.run([sys.executable, "-u", str(Path(__file__).resolve()), str(book), "--model", m,
                            "--chapters", str(chapter), "--tags-file", str(tmp), "--fresh"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        took = time.time() - t
        data = load_path(tmp)
        if r.returncode or not data:
            problem = next((l[9:] for l in r.stdout.splitlines() if l.startswith("@problem ")), f"stopped (code {r.returncode})")
            out(f"    {m} could not run: {problem}")
            continue
        results[m] = (data["chapters"].get(key, {}), took)
        out(f"    {m}: {len(results[m][0])} lines tagged, {took:.0f} s in all (including loading the model)")
    if len(results) < 2:
        return
    names = list(results)
    a, b = results[names[0]][0], results[names[1]][0]
    idx = sorted(set(a) | set(b), key=int)
    same = sum(1 for i in idx if (a.get(i) or [None])[0] == (b.get(i) or [None])[0])
    lines = [f"Chapter {chapter}: {names[0]} and {names[1]} agree on {same} of {len(idx)} lines.", ""]
    for i in idx:
        x, y = (a.get(i) or [None])[0], (b.get(i) or [None])[0]
        if x != y:
            lines += [f"[{i}] {re.sub(chr(10), ' ', paragraphs[int(i)])[:220]}", f"    {names[0]}: {x}    {names[1]}: {y}", ""]
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    Path(out_file).write_text("\n".join(lines), encoding="utf-8")
    out(f"{lines[0]}")
    out(f"@compared {out_file}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book")
    ap.add_argument("--tags-file", default="", help="save the tags here instead of in casting\\")
    ap.add_argument("--compare", default="", help="e.g. best,big: time these models on --chapters N and list the differences")
    ap.add_argument("--model", default="best")
    ap.add_argument("--chapters", default="")
    ap.add_argument("--fresh", action="store_true", help="start again instead of carrying on")
    ap.add_argument("--progress", action="store_true")
    args = ap.parse_args()
    from books import read_book
    import llm
    meta, chapters = read_book(args.book)
    if args.compare:
        chapter = int(args.chapters or 1)
        compare(args.book, chapter, [m.strip() for m in args.compare.split(",")], chapters,
                HERE / "logs" / f"{Path(args.book).stem} - model comparison.txt")
        return
    try:
        model = llm.LocalModel(args.model)
    except llm.ModelError as e:
        out(f"@problem {e}")
        sys.exit(3)
    t = time.time()
    out(f"Reading {len(chapters)} chapters for who speaks...")
    last = [0.0]

    def progress(done, total, chapter):
        if args.progress and (time.time() - last[0] > 0.5 or done == total):
            last[0] = time.time()
            out(f"@tag {done} {total} {chapter}")
    data = tag_book(chapters, model, Path(args.book).stem, meta.get("title", ""), meta.get("creator", ""),
                    parse_range(args.chapters, len(chapters)), args.fresh, progress, path=args.tags_file or None)
    tagged = sum(len(v) for v in data["chapters"].values())
    people = Counter(n for v in data["chapters"].values() for n, _ in v.values() if n and n != speakers.NARRATOR)
    out(f"Tagged {tagged:,} lines in {time.time() - t:.0f} s. Most lines: "
        + ", ".join(f"{n} ({c})" for n, c in people.most_common(6)))
    out(f"@done {tags_file(Path(args.book).stem)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
        if "out of memory" in msg.lower():
            msg = "The graphics card ran out of memory. Close other programs that use it, or pick the faster model."
        out(f"@problem {msg}")
        sys.exit(1)
