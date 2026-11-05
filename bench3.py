"""Which experimental speed settings help on this PC?

    runtime\\python\\python.exe bench3.py "book.epub" [chapter number] [--two]

Reads up to 40 paragraphs of one chapter in normal mode, faster mode and half precision (twice each,
after a warm-up) and says which to turn on. Half precision changes the sound very slightly, so both
versions are saved in the "bench" folder to compare by ear.
With --two it also converts two chapters one after the other and then both at once (takes longer).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HOME", str(HERE / "models"))
warnings.filterwarnings("ignore")

import numpy as np
import soundfile as sf
import torch

torch.backends.cudnn.enabled = False
from kokoro import KPipeline

import convert
from books import read_book
from epub_reader import Chapter
from pronunciations import compile_rules, load_rules


def timed(speaker, chapter, voice, opts, rules):
    t = time.time()
    with contextlib.redirect_stdout(io.StringIO()):          # hide the progress lines
        audio, _, _ = convert.synth_chapter(speaker, chapter, voice, 0, opts, rules)
    return time.time() - t, audio


def two_at_once(book, n, total):
    """Convert chapters n and n+1 normally, then both at once, from a temporary copy of the book."""
    if total < 2:
        print("The book needs at least two chapters for this test.")
        return
    pair = f"{n}-{n + 1}" if n < total else f"{n - 1}-{n}"
    tmp = Path(tempfile.mkdtemp(prefix="forge-bench-"))
    copy = HERE / f"bench-two-{os.getpid()}{Path(book).suffix}"
    shutil.copy(book, copy)
    times = {}
    try:
        for parallel in (False, True):
            opts = {"parallel": parallel, "book_loudness": False}
            t = time.time()
            r = subprocess.run([sys.executable, str(HERE / "convert.py"), str(copy), "--chapters", pair, "--out", str(tmp),
                                "--options", json.dumps(opts)], cwd=HERE, capture_output=True, text=True)
            times[parallel] = time.time() - t
            if r.returncode != 0:
                print("  The test conversion failed:\n" + r.stdout[-800:])
                return
            print(f"  chapters {pair} {'both at once' if parallel else 'one after the other'}: {times[parallel]:.0f} s",
                  flush=True)
    finally:
        copy.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(HERE / "work" / copy.stem, ignore_errors=True)
    gain = (times[False] / times[True] - 1) * 100
    print(f"Two chapters at once is {gain:+.0f}% compared with one at a time.")
    print("Worth turning on: Settings > Speed > Two chapters at once." if gain >= 10 else
          "Not worth it on this PC. Leave it off.")


def main():
    args = [a for a in sys.argv[1:] if a != "--two"]
    if not args:
        sys.exit('usage: bench3.py "book.epub" [chapter number] [--two]')
    meta, chapters = read_book(args[0])
    n = max(1, min(int(args[1]) if len(args) > 1 else 1, len(chapters)))
    ch = chapters[n - 1]
    sample = Chapter(ch.title, ch.paragraphs[:40])
    opts = dict(convert.load_options(), room_tone=False)
    rules = compile_rules(load_rules())
    voice = "af_heart"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"{meta.get('title')}: \"{ch.title}\", {sample.words:,} words, on {device}")
    pipe = KPipeline(lang_code="a", device=device)
    print("Warming up the graphics card...", flush=True)
    timed(convert.Speaker(pipe, 1.0), Chapter("Warm up", sample.paragraphs[:8]), voice, opts, rules)

    modes = {"normal": dict(fast=False, half=False), "faster": dict(fast=True, half=False)}
    if device == "cuda":
        modes["half precision"] = dict(fast=False, half=True)
    results = {m: [] for m in modes}
    audio = {}
    for _round in range(2):
        for name, kw in modes.items():
            try:
                took, a = timed(convert.Speaker(pipe, 1.0, **kw), sample, voice, dict(opts, half=kw["half"]), rules)
            except Exception as e:  # noqa: BLE001
                print(f"  {name}: failed ({type(e).__name__}: {e})")
                results.pop(name, None)
                continue
            if name in results:
                results[name].append(took)
                audio[name] = a
                print(f"  {name}: {len(a) / 24000:.0f} s of audio in {took:.1f} s ({len(a) / 24000 / took:.1f}x realtime)",
                      flush=True)
        modes = {m: kw for m, kw in modes.items() if m in results}
    normal = min(results["normal"])
    print()
    if "faster" in results:
        gain = (normal / min(results["faster"]) - 1) * 100
        print(f"Faster mode: {gain:+.0f}%. " + ("Worth turning on (Settings > Speed)." if gain >= 8 else "Leave it off."))
    if "half precision" in results:
        gain = (normal / min(results["half precision"]) - 1) * 100
        out_dir = HERE / "bench"
        out_dir.mkdir(exist_ok=True)
        sf.write(str(out_dir / "normal.wav"), audio["normal"], 24000)
        sf.write(str(out_dir / "half precision.wav"), audio["half precision"], 24000)
        print(f"Half precision: {gain:+.0f}%. " + ("Faster, but listen first: " if gain >= 8 else "Not faster here; leave it off. ")
              + f"compare the two files in {out_dir}.")
    elif device != "cuda":
        print("Half precision needs the graphics card, so it was not tested.")
    if "--two" in sys.argv[1:]:
        print("\nTesting two chapters at once (this converts two chapters twice)...", flush=True)
        two_at_once(args[0], n, len(chapters))


if __name__ == "__main__":
    main()
