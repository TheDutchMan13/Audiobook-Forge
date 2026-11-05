"""Name scan: find the words Kokoro's dictionary does not know, so its pronunciation is a guess.

Names and invented words (Thanos, Wakanda, Favreau) are read by a letter-to-sound guesser. Most guesses
are fine, some are not. This lists them by how often they appear, so the ones that matter most can be
checked once and fixed with a pronunciation fix before converting.

    python namescan.py "book.epub"          # print the list
    python namescan.py "book.epub" --json   # machine-readable, used by the window
"""
import json
import re
import sys
from collections import Counter, defaultdict

import normalize
from pronunciations import apply_rules, compile_rules, load_rules

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*[A-Za-z]")
SENTENCE_RE = re.compile(r"[^.!?…]*[.!?…]+[\"”’)]?|[^.!?…]+$")


def _example(paragraph, word):
    for m in SENTENCE_RE.finditer(paragraph):
        s = m.group(0).strip()
        if re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", s):
            return s if len(s) <= 160 else s[:157].rsplit(" ", 1)[0] + "..."
    return paragraph[:157]


def scan(chapters, rules=None, options=None, limit=200):
    """[(word, times, example sentence)] for words the dictionary does not know, most frequent first.
    Words already covered by a pronunciation fix are left out. Returns None when the dictionary
    (Kokoro's misaki package) is not available."""
    if normalize.known_word("the") is None:
        return None
    rules = load_rules() if rules is None else rules
    compiled = compile_rules(rules)
    covered = {r["find"].strip().lower() for r in rules if r.get("enabled", True)}
    said_as = set()
    for r in rules:
        said_as.update(w.lower() for w in WORD_RE.findall(r.get("say", "")))
    opts = {"clutter": True, "normalize": True, "heteronyms": True, "shouting": True}
    opts.update({k: v for k, v in (options or {}).items() if k in opts})

    counts, forms, examples = Counter(), defaultdict(Counter), {}
    verdict = {}
    for ch in chapters:
        for par in [ch.title] + list(ch.paragraphs):
            text = normalize.prepare(par, opts, (lambda x: apply_rules(x, compiled)) if compiled else None)
            text = normalize.MARKUP_RE.sub(" ", text)               # words with a phoneme fix are covered
            for m in WORD_RE.finditer(text):
                word = re.sub(r"['’]s$|['’]$", "", m.group(0))
                for part in word.split("-"):
                    if len(part) < 3 or part.isupper() and len(part) <= 3:
                        continue                                    # short acronyms are spelled out
                    key = part.lower()
                    if key in covered or key in said_as:
                        continue
                    if key not in verdict:
                        verdict[key] = not normalize.known_word(part)
                    if not verdict[key]:
                        continue
                    counts[key] += 1
                    forms[key][part] += 1
                    if key not in examples:
                        examples[key] = _example(par, part)
    out = []
    for key, n in counts.most_common():
        form = forms[key].most_common(1)[0][0]
        if form.isupper() and len(forms[key]) > 1:
            form = next((f for f, _ in forms[key].most_common() if not f.isupper()), form)
        out.append((form, n, examples[key]))
        if len(out) >= limit:
            break
    return out


def main():
    from books import BookError, read_book
    if len(sys.argv) < 2:
        sys.exit('usage: python namescan.py "book.epub" [--json]')
    try:
        _, chapters = read_book(sys.argv[1])
    except BookError as e:
        sys.exit(f"Could not read the book: {e}")
    found = scan(chapters)
    if found is None:
        sys.exit("Kokoro's dictionary (misaki) is not installed, so words cannot be checked.")
    if "--json" in sys.argv:
        print(json.dumps(found, ensure_ascii=False))
        return
    for word, n, example in found:
        print(f"{n:5d}  {word:20}  {example}")
    print(f"\n{len(found)} words the voice will have to guess.")


if __name__ == "__main__":
    main()
