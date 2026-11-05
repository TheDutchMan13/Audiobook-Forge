"""Check by listening: transcribe a finished audiobook with Whisper and compare it with the book's text.

    runtime\\python\\python.exe bookcheck.py "logs\\reports\\<book> - listening report.json"
        --model base.en       fast (default); small.en is slower but hears better
        --chapters 1-3        only these chapters (handy for a timing test)
        --progress            machine-readable progress lines for the window

Reads the finished file (M4B, MP3, or a folder of MP3s) at the paragraph positions the converter saved,
so it checks exactly what you will hear. Each paragraph's transcript is compared word by word with what
the narrator was given to say; names, numbers and words changed by pronunciation fixes are allowed to
differ. Places where words are missing, the end of a paragraph is cut off, or extra words appear are
added to the listening report. Transcripts are saved, so checking the same book again is quick.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HOME", str(HERE / "models"))
warnings.filterwarnings("ignore")
if os.name == "nt":      # a program that cannot start must fail, not wait behind a hidden dialog
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:  # noqa: BLE001
        pass

import numpy as np

import checks

RATE = 16000                 # what Whisper listens at
MAX_CLIP = 28.0              # Whisper hears 30 s at a time; longer paragraphs are cut at a quiet moment
MIN_CLIP = 1.0               # shorter clips make Whisper invent words
MODELS = {"base.en": "openai/whisper-base.en", "small.en": "openai/whisper-small.en", "tiny.en": "openai/whisper-tiny.en"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
KINDS = {"ending": "The end of the paragraph may be cut off",
         "missing": "Words may be missing or wrong",
         "extra": "Extra words or sounds that are not in the text"}


def out(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


# ------------------------------------------------------------------------------------------- audio
def decode(src, start=None, duration=None):
    """Audio of a file (or part of it) as 16 kHz mono float32."""
    cmd = [checks.ffmpeg_path(HERE), "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(src), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read {Path(src).name}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def split_long(clip):
    """Pieces of at most MAX_CLIP seconds, cut at the quietest moment near each limit."""
    pieces, limit = [], int(MAX_CLIP * RATE)
    while len(clip) > limit:
        window = clip[int(limit * 0.6):limit]
        frame = RATE // 50
        n = len(window) // frame
        level = np.abs(window[:n * frame]).reshape(n, frame).mean(axis=1)
        cut = int(limit * 0.6) + int(np.argmin(level)) * frame + frame // 2
        pieces.append(clip[:cut])
        clip = clip[cut:]
    pieces.append(clip)
    return pieces


# ------------------------------------------------------------------------------------------- Whisper
class Whisper:
    """Transcribes lists of 16 kHz clips on the graphics card (or the processor if there is none)."""

    def __init__(self, model="base.en", half=False):
        import torch
        torch.backends.cudnn.enabled = False
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        try:
            from transformers.utils import logging as hf_logging
            hf_logging.set_verbosity_error()                     # keep the window's log readable
            hf_logging.disable_progress_bar()
        except Exception:  # noqa: BLE001
            pass
        name = MODELS.get(model, model)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = torch.float16 if half and self.device == "cuda" else torch.float32
        self.processor = WhisperProcessor.from_pretrained(name)
        model = WhisperForConditionalGeneration.from_pretrained(name)        # works on old and new transformers
        self.model = model.to(self.device, dtype=self.dtype).eval()
        self.torch = torch
        self.batch = 8 if self.device == "cuda" else 2

    def __call__(self, clips, items=None):
        texts = []
        for i in range(0, len(clips), self.batch):
            group = clips[i:i + self.batch]
            feats = self.processor(group, sampling_rate=RATE, return_tensors="pt").input_features
            with self.torch.no_grad():
                ids = self.model.generate(feats.to(self.device, self.dtype), max_new_tokens=220)
            texts += self.processor.batch_decode(ids, skip_special_tokens=True)
        return [t.strip() for t in texts]


# ------------------------------------------------------------------------------------------- comparing
def words(text):
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[—–\-/]", " ", text)
    return [w.strip("'") for w in re.findall(r"[a-z0-9']+", text) if w.strip("'")]


def similar(a, b):
    if a == b:
        return True
    if a.isdigit() or b.isdigit():
        return False
    return len(a) >= 4 and len(b) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


class Comparer:
    """Word-by-word comparison that forgives what a transcript cannot be expected to match exactly."""

    def __init__(self, rules=()):
        self.fixed = set()
        for r in rules:                       # words a pronunciation fix put in or took out
            self.fixed.update(words(r.get("say", "")))
            self.fixed.update(words(r.get("find", "")))
        self.known = {}

    def soft(self, word):
        """A word the transcript may legitimately spell differently: numbers, fixes, unfamiliar names."""
        if any(c.isdigit() for c in word) or word in self.fixed or len(word) <= 2:
            return True
        if word not in self.known:
            try:
                import normalize
                k = normalize.known_word(word)
                self.known[word] = k is not False      # unknown to the dictionary: probably a name
            except Exception:  # noqa: BLE001
                self.known[word] = True
        return not self.known[word]

    def compare(self, said, heard):
        ref, hyp = words(said), words(heard)
        missing_at = [False] * len(ref)
        extra_runs, longest_extra = 0, 0
        sm = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            if op in ("replace", "delete"):
                pool = hyp[j1:j2]
                for i in range(i1, i2):
                    if not self.soft(ref[i]) and not any(similar(ref[i], h) for h in pool):
                        missing_at[i] = True
            if op in ("replace", "insert"):
                extra = [h for h in hyp[j1:j2] if not any(similar(h, r) for r in ref[i1:i2]) and not h.isdigit()]
                longest_extra = max(longest_extra, len(extra))
        hard = [i for i, w in enumerate(ref) if not self.soft(w)]
        missing = [ref[i] for i in hard if missing_at[i]]
        run = best = 0
        for i in hard:
            run = run + 1 if missing_at[i] else 0
            best = max(best, run)
        tail = 0
        for i in reversed(hard):
            if not missing_at[i]:
                break
            tail += 1
        return {"words": len(ref), "checked": len(hard), "missing": missing, "longest_gap": best,
                "missing_at_end": tail, "longest_extra": longest_extra}


def verdict(c):
    """None, or (kind, how bad) for a comparison."""
    if c["checked"] < 4:
        return None
    if c["missing_at_end"] >= 3 and c["missing_at_end"] >= 0.15 * c["checked"]:
        return "ending", c["missing_at_end"] / c["checked"] + 1
    share = len(c["missing"]) / c["checked"]
    if c["longest_gap"] >= 4 or (share >= 0.2 and len(c["missing"]) >= 3):
        return "missing", share + c["longest_gap"] / 10
    if c["longest_extra"] >= 6:
        return "extra", c["longest_extra"] / 20
    return None


# ------------------------------------------------------------------------------------------- the check
def parse_chapters(spec):
    if not spec:
        return None
    picked = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        picked.update(range(int(a), int(b or a) + 1))
    return picked


def snippet(text, limit=110):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3].rsplit(" ", 1)[0] + "..."


def clip_key(chapter, item, signature):
    raw = f"{signature}|{chapter}|{item['at']}|{item['dur']}|{item['said']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def check_book(report_path, model="base.en", chapters=None, transcriber=None, progress=False, half=False):
    report_path = Path(report_path)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    paragraphs = data.get("paragraphs") or []
    if not paragraphs:
        raise SystemExit("This book was converted before Check by listening existed. Convert it again (it is quick "
                         "when its work files were kept), then check it.")
    src = Path(data.get("output", ""))
    if not src.exists():
        raise SystemExit(f"The audiobook is not where it was saved: {src}")
    folder = src.is_dir()
    files = sorted(src.glob("*.mp3")) if folder else []
    stat = (files[0] if folder and files else src).stat()
    signature = f"{stat.st_size}-{int(stat.st_mtime)}"
    cache_file = report_path.with_name(report_path.name.replace(" - listening report.json", "") + " - transcripts.json")
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    try:
        from pronunciations import load_rules
        rules = load_rules()
    except Exception:  # noqa: BLE001
        rules = []
    comparer = Comparer(rules)
    todo = [c for c in paragraphs if chapters is None or c["chapter"] in chapters]
    total = sum(len(c["items"]) for c in todo)
    if transcriber is None:
        need = any(clip_key(c["chapter"], it, signature) not in cache for c in todo for it in c["items"])
        if need:
            out(f"Loading Whisper ({model}). The first time, it downloads about {'290' if model == 'base.en' else '970'} MB.")
            transcriber = Whisper(model, half=half)
            out(f"Listening on {transcriber.device}.")
    issues, done, heard_seconds, started = [], 0, 0.0, time.time()
    checked = skipped = 0
    for c in todo:
        if folder:
            if c["chapter_pos"] >= len(files):
                continue
            audio, offset = decode(files[c["chapter_pos"]]), 0.0
        else:
            audio, offset = decode(src, c["start"], c["duration"] + 0.5), 0.0
        clips, owners = [], []
        for n, it in enumerate(c["items"]):
            key = clip_key(c["chapter"], it, signature)
            if key in cache or it["dur"] < MIN_CLIP or len(words(it["said"])) < 4:
                continue
            a = int(max(0.0, it["at"] - 0.15 + offset) * RATE)
            b = int((it["at"] + it["dur"] + 0.25 + offset) * RATE)
            clip = audio[a:b]
            if len(clip) < MIN_CLIP * RATE:
                continue
            for k, part in enumerate(split_long(clip)):
                clips.append(part)
                owners.append((n, key, k))
        if clips:
            texts = transcriber(clips, [c["items"][n] for n, _, _ in owners])
            joined = {}
            for (n, key, k), text in zip(owners, texts):
                joined.setdefault(key, []).append((k, text))
            for key, parts in joined.items():
                cache[key] = " ".join(t for _, t in sorted(parts))
            heard_seconds += sum(len(x) for x in clips) / RATE
        for it in c["items"]:
            key = clip_key(c["chapter"], it, signature)
            if key not in cache:
                skipped += 1
                continue
            checked += 1
            comp = comparer.compare(it["said"], cache[key])
            v = verdict(comp)
            if v:
                kind, score = v
                issues.append({"kind": kind, "score": round(score, 3), "chapter": c["chapter"], "chapter_pos": c["chapter_pos"],
                               "chapter_title": c["title"], "at": it["at"], "book_at": round(c["start"] + it["at"], 2),
                               "said": snippet(it["said"]), "heard": snippet(cache[key]),
                               "missing": comp["missing"][:12]})
        done += len(c["items"])
        try:
            cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        if progress:
            out(f"@check {done} {total} {c['chapter']}")
        else:
            out(f"  {c['title']}: {done} of {total} paragraphs")
    took = max(0.001, time.time() - started)
    speed = heard_seconds / took if heard_seconds else None
    issues.sort(key=lambda e: e["book_at"])
    data["check"] = {"when": time.strftime("%Y-%m-%d %H:%M"), "model": model, "checked": checked, "skipped": skipped,
                     "chapters": sorted(chapters) if chapters else None, "issues": issues,
                     "speed": round(speed, 1) if speed else None, "seconds": round(took, 1)}
    tmp = report_path.with_suffix(".partial")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, report_path)
    write_text_section(report_path, data["check"])
    return data["check"]


def write_text_section(report_path, check):
    txt = report_path.with_suffix(".txt")
    try:
        body = txt.read_text(encoding="utf-8").split("\n=== Checked by listening")[0].rstrip("\n")
    except OSError:
        body = ""
    clock = lambda s: f"{int(s) // 3600}:{int(s) // 60 % 60:02d}:{int(s) % 60:02d}"   # noqa: E731
    lines = ["", f"=== Checked by listening ({check['model']}, {check['when']}): {check['checked']} paragraphs, "
             f"{len(check['issues'])} to listen to ==="]
    for kind in ("ending", "missing", "extra"):
        group = [e for e in check["issues"] if e["kind"] == kind]
        if group:
            lines.append(KINDS[kind] + ":")
            for e in group:
                lines.append(f"  {clock(e['book_at'])}  ({e['chapter_title']})")
                lines.append(f"      text:  {e['said']}")
                lines.append(f"      heard: {e['heard']}")
    if not check["issues"]:
        lines.append("Every checked paragraph matched its text.")
    try:
        txt.write_text(body + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Check a finished audiobook against its text by listening to it.")
    ap.add_argument("report", help="the book's listening report (.json) in logs\\reports")
    ap.add_argument("--model", default="base.en", help="base.en (fast) or small.en (more accurate)")
    ap.add_argument("--chapters", help="e.g. 1 or 1-3 (for a quick timing test)")
    ap.add_argument("--half", action="store_true", help="half-precision math on the graphics card")
    ap.add_argument("--progress", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    try:
        result = check_book(args.report, args.model, parse_chapters(args.chapters), progress=args.progress, half=args.half)
    except SystemExit as e:
        out(f"@problem {e}")
        out(str(e))
        sys.exit(2)
    except Exception as e:  # noqa: BLE001
        msg = f"The check could not run: {type(e).__name__}: {e}"
        out(f"@problem {msg}")
        out(msg)
        sys.exit(3)
    speed = f", {result['speed']}x realtime" if result["speed"] else ""
    out(f"Checked {result['checked']} paragraphs in {result['seconds']:.0f} s{speed}: "
        f"{len(result['issues'])} worth a listen.")
    out("@done")


if __name__ == "__main__":
    main()
