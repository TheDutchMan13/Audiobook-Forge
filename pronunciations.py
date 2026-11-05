"""Pronunciation fixes: find-and-replace rules applied to the text just before it is spoken.

Rules live in pronunciations.json next to the program:
    {"rules": [{"find": "K.E.V.I.N.", "say": "Kevin", "whole_word": true, "match_case": false, "enabled": true}]}
The book file itself is never changed.
"""
import hashlib
import json
import re
from pathlib import Path

RULES_FILE = Path(__file__).resolve().parent / "pronunciations.json"


def _clean(rule):
    return {
        "find": str(rule.get("find", "")),
        "say": str(rule.get("say", "")),
        "whole_word": bool(rule.get("whole_word", True)),
        "match_case": bool(rule.get("match_case", False)),
        "enabled": bool(rule.get("enabled", True)),
    }


def load_rules(path=None):
    """The saved rules, or [] when there are none or the file is unreadable."""
    try:
        data = json.loads(Path(path or RULES_FILE).read_text(encoding="utf-8"))
        rules = data.get("rules", []) if isinstance(data, dict) else data
        return [_clean(r) for r in rules if isinstance(r, dict) and str(r.get("find", "")).strip()]
    except Exception:  # noqa: BLE001
        return []


def save_rules(rules, path=None):
    clean = [_clean(r) for r in rules if str(r.get("find", "")).strip()]
    target = Path(path or RULES_FILE)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"rules": clean}, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return clean


def _pattern(rule):
    body = re.escape(rule["find"].strip())
    if rule["whole_word"]:
        # \b fails next to punctuation (K.E.V.I.N.), so look at the neighbouring characters instead
        body = r"(?<![\w])" + body + r"(?![\w])"
    return re.compile(body, 0 if rule["match_case"] else re.IGNORECASE)


def compile_rules(rules):
    """Pre-compile the enabled rules: [(pattern, replacement)]."""
    out = []
    for r in rules:
        r = _clean(r)
        if r["enabled"] and r["find"].strip():
            out.append((_pattern(r), r["say"]))
    return out


def apply_rules(text, rules):
    """Apply rules (raw dicts or the result of compile_rules) to text, in order."""
    if not rules:
        return text
    if isinstance(rules[0], dict):
        rules = compile_rules(rules)
    for pattern, say in rules:
        text = pattern.sub(lambda m, s=say: s, text)
    return text


def rules_hash(rules):
    """Short fingerprint of the enabled rules, '' when there are none. Cached audio is only
    reused while this stays the same, so changing a rule re-renders the chapters it affects."""
    active = [(r["find"].strip(), r["say"], r["whole_word"], r["match_case"])
              for r in (_clean(x) for x in rules) if r["enabled"] and r["find"].strip()]
    if not active:
        return ""
    return hashlib.sha1(json.dumps(active, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]


def count_matches(text, rules):
    """How many times each rule matches (for showing 'used N times in this book')."""
    out = []
    for r in rules:
        r = _clean(r)
        out.append(len(_pattern(r).findall(text)) if r["enabled"] and r["find"].strip() else 0)
    return out


# ------------------------------------------------------------------ sharing sets of fixes
def export_rules(rules, path):
    """Save fixes to a file to share or reuse (for example one set per book series)."""
    return save_rules(rules, path)


def read_rules_file(path):
    """Fixes from a file made by Export, or from a plain text / CSV list with one fix per line:
    'written as = say it as', 'written as, say it as' or 'written as<TAB>say it as'."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig", errors="replace")
    try:
        data = json.loads(raw)
        rules = data.get("rules", []) if isinstance(data, dict) else data
        return [_clean(r) for r in rules if isinstance(r, dict) and str(r.get("find", "")).strip()]
    except ValueError:
        pass
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("\t", "=", ","):
            if sep in line:
                find, say = line.split(sep, 1)
                find, say = find.strip().strip('"'), say.strip().strip('"')
                if find and say and find.lower() not in ("written as", "find"):
                    out.append(_clean({"find": find, "say": say}))
                break
    return out


def merge_rules(existing, incoming):
    """Add imported fixes to the current ones. A fix for a word that already has one replaces it.
    Returns (merged, added, updated, unchanged)."""
    merged = [_clean(r) for r in existing]
    index = {r["find"].strip().lower(): i for i, r in enumerate(merged)}
    added = updated = same = 0
    for r in (_clean(x) for x in incoming):
        key = r["find"].strip().lower()
        if not key:
            continue
        if key in index:
            if merged[index[key]] == r:
                same += 1
            else:
                merged[index[key]] = r
                updated += 1
        else:
            index[key] = len(merged)
            merged.append(r)
            added += 1
    return merged, added, updated, same
