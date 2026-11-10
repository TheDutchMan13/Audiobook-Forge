import json, os, subprocess, sys, zipfile
from pathlib import Path
import numpy as np, soundfile as sf
PY = sys.executable
results = []
def ok(c, m):
    results.append(bool(c)); print(("PASS " if c else "FAIL ") + m, flush=True)
def run(args, opts=None, env=None):
    cmd = [PY, "convert.py", *args] + (["--options", json.dumps(opts)] if opts is not None else [])
    return subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, **(env or {})), timeout=300)
def spoken():
    return [l.split("\t", 1) for l in Path("spoken.log").read_text(encoding="utf-8").splitlines()] if Path("spoken.log").exists() else []
def fresh():
    Path("spoken.log").unlink(missing_ok=True); subprocess.run(["rm", "-rf", "work", "out_m", "casting"])
def probe(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", "-show_streams", "-show_format", str(path)], capture_output=True, text=True)
    return json.loads(r.stdout)
Path("rules.json").write_text(json.dumps({"rules": []}))
R = ["--rules", "rules.json"]
D = {"dialogue_voice": ""}

# footnotes
fresh(); r = run(["notes.epub", "--out", "out_m"] + R, dict(D, footnotes="end"))
t = [x for _, x in spoken()]
ok(r.returncode == 0 and "Notes for this chapter." in t and any(x.startswith("Note 1. The first note") for x in t), "EPUB footnotes read at the end of the chapter")
ok(not any("first note explains" in x and not x.startswith("Note") for x in t), "note text is not read in the middle of the chapter")
ok(any("Note 1. Smith" in x for x in t), "endnotes from a separate notes file are found")
fresh(); run(["notes.epub", "--out", "out_m"] + R, D)
ok(not any(x.startswith(("Note ", "Notes for")) for _, x in spoken()), "footnotes skipped by default")
fresh(); run(["notes.docx", "--out", "out_m"] + R, dict(D, footnotes="end"))
ok(any("Note 1. The footnote text." in x for _, x in spoken()) and any("An endnote on a heading" in x for _, x in spoken()), "Word footnotes and endnotes read at chapter end")

# samples
fresh(); r = run(["round.txt", "--sample", "out_m/s.wav"] + R, D)
a, sr = sf.read("out_m/s.wav") if Path("out_m/s.wav").exists() else (np.zeros(1), 24000)
ok(r.returncode == 0 and len(a) / sr > 5 and not Path("work").exists(), f"book sample made ({len(a)/sr:.0f} s) without touching the work folder")
ok(not any(x.startswith("Chapter") for _, x in spoken()), "a sample does not read the chapter title")
fresh(); r = run(["round.txt", "--sample", "out_m/p.wav", "--sample-at", "1:3"] + R, {"dialogue_voice": "auto"})
sp = spoken()
ok(r.returncode == 0 and len(sp) == 3 and sp[0][1].startswith('"Not yet') and sp[0][0] == "am_michael", f"one paragraph, with the speaker worked out from the whole chapter: {sp}")
fresh(); run(["round.txt", "--sample", "out_m/c.wav", "--chapters", "2"] + R, D)
ok(any("Bruce" in x or "morning" in x or "keeper" in x for _, x in spoken()), "sample comes from the selected chapters")

# output formats
for fmt, quality, check in (("m4b", "small", lambda p: p.suffix == ".m4b"), ("mp3", "high", lambda p: p.suffix == ".mp3"),
                            ("mp3_chapters", "standard", lambda p: p.is_dir())):
    fresh(); r = run(["round.txt", "--out", "out_m"] + R, dict(D, format=fmt, quality=quality))
    out = Path(r.stdout.strip().splitlines()[-1].split(" -> ", 1)[1]) if " -> " in r.stdout else None
    ok(r.returncode == 0 and out and out.exists() and check(out), f"{fmt} output: {out}")
    if fmt == "mp3":
        d = probe(out); ok(len(d["chapters"]) == 4 and any(s["codec_type"] == "video" for s in d["streams"]) and d["streams"][0]["bit_rate"] == "96000",
                           "single MP3 has chapters, cover and the chosen quality")
    if fmt == "mp3_chapters":
        files = sorted(out.glob("*.mp3")); d = probe(files[1])
        ok(len(files) == 4 and d["format"]["tags"].get("track") == "2/4" and any(out.glob("*.m3u")), f"one MP3 per chapter, numbered and tagged: {[f.name for f in files]}")

# per-character voices
fresh(); r = run(["cast.txt", "--out", "out_m"] + R, {"dialogue_voice": "cast"})
sp = spoken()
ok("Each character has their own voice" in r.stdout and "Tony (am_fenrir)" in r.stdout, "cast announced in the log")
v_of = lambda text: next((v for v, t in sp if text in t), None)
ok(v_of("You are late") == v_of("We are listening") == "af_nicole" and v_of("I am never late") == v_of("Never,") == "am_fenrir"
   and v_of("Are you two done") == v_of("How wrong") == "af_kore", "each recurring character keeps their own voice")
ok(v_of("The readings are wrong") == "am_michael", "a minor character uses the men's voice")
os.makedirs("casting", exist_ok=True)
Path("casting/cast.json").write_text(json.dumps({"voices": {"Tony": "am_puck", "Pepper": "af_sarah"}}))
Path("spoken.log").unlink(missing_ok=True); subprocess.run(["rm", "-rf", "work"])
r = run(["cast.txt", "--out", "out_m"] + R, {"dialogue_voice": "cast"}); sp = spoken()
ok(v_of("I am never late") == "am_puck" and v_of("You are late") == "af_sarah", "voices chosen in the cast file are used")

# logs and diagnostics
logs = sorted(Path("logs").glob("*.txt"))
ok(logs and "Done in" in logs[-1].read_text(), f"conversion log written ({logs[-1].name if logs else None})")
r = subprocess.run([PY, "diagnostics.py", "out_m/d.zip"], capture_output=True, text=True, timeout=300)
names = zipfile.ZipFile("out_m/d.zip").namelist() if Path("out_m/d.zip").exists() else []
ok("info.json" in names and any(n.startswith("logs/") for n in names) and not any(n.endswith((".epub", ".wav", ".m4b")) for n in names),
   f"diagnostics zip has logs and info but no books: {names}")
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")

# ---- chapter art
results.clear()
fresh(); r = run(["many.txt", "--out", "out_m"] + R, dict(D, format="mp3", chapter_art=True))
from mutagen.id3 import ID3
t = ID3("out_m/many.mp3"); chaps = sorted(t.getall("CHAP"), key=lambda c: c.start_time)
ok(len(chaps) == 14 and all(len(c.sub_frames.getall("APIC")) == 1 for c in chaps), "single MP3: every chapter mark carries its picture")
ok(len(t.getall("APIC")) == 1, "the book's own cover is still the MP3's main picture")
d = probe("out_m/many.mp3"); starts = [float(c["start_time"]) for c in d["chapters"]]
ok(starts == sorted(starts) and len(starts) == 14, "chapters are listed in order in the file")
fresh(); r = run(["many.txt", "--out", "out_m"] + R, dict(D, format="mp3_chapters", chapter_art=True))
pics = [ID3(str(f)).getall("APIC")[0].data for f in sorted(Path("out_m/many").glob("*.mp3"))]
ok(len(pics) == 14 and len(set(pics)) == 14, "folder of MP3s: every file has its own card as its picture")
fresh(); run(["many.txt", "--out", "out_m"] + R, dict(D, format="mp3_chapters"))
pics = [ID3(str(f)).getall("APIC")[0].data for f in sorted(Path("out_m/many").glob("*.mp3"))]
ok(len(set(pics)) == 1, "without chapter art every file carries the book's cover")
fresh(); r = run(["many.txt", "--out", "out_m"] + R, dict(D, format="m4b", chapter_art=True))
ok(r.returncode == 0 and "Chapter art needs an MP3 format" in r.stdout and list(Path("out_m").glob("*.m4b")), "M4B: explains that chapter art needs MP3 and still finishes")
ok(not Path("work").exists() or not any(Path("work").rglob("*.jpg")), "the cards are cleaned up afterwards")
import covers
cover = covers.make_cover("Book", "Me")
ok(all(len(covers.make_chapter_card(i, t, "Book", cover if i % 2 else None, size=300, total=20)) > 1000 for i, t in
       enumerate(["", "Chapter 7", "Part 3: A very long title " * 6, "Émile & Zoë — ünïcode", "…", "12"], 1)), "cards cope with odd titles")
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")

# ---- false characters, ffmpeg handling
results.clear()
import speakers
from epub_reader import Chapter
paras = Path("acotar_like.txt").read_text().split("\n\n")
prose = ["It was like a dream, and again like a nightmare.", "She would try again and again, like always.", "He’d said it, and I’d heard it."] * 8
names = {c["name"] for c in speakers.find_characters([Chapter("x", paras + prose)])}
ok(names == {"Cassian", "Rhys", "Mor", "Amren"}, f"only real characters are found (not Like, Again, He’d, I’d, possessives): {sorted(names)}")
paras2 = ["I waited. Then Rhys’s voice turned cold. “Go,” he said.", "I said nothing. Rhys’s eyes narrowed. “Because,” he said."] * 3
g = speakers.SpeakerGuesser(paras2)
who = {w[0] for w in g.speakers(paras2) if w[0]}
ok(who == {"Rhys"}, f"a possessive is the same character (Rhys’s -> Rhys): {who}")
# a broken ffmpeg in bin\ (a shared build without its DLLs) must not be used
os.makedirs("bin", exist_ok=True)
Path("bin/ffmpeg.exe").write_text("not a program")
fresh(); r = run(["round.txt", "--out", "out_m", "--chapters", "1"] + R, D)
ok(r.returncode == 0 and list(Path("out_m").glob("*.m4b")), f"a broken bin\\ffmpeg.exe is skipped when it is not a real program (exit {r.returncode})")
subprocess.run(["rm", "-rf", "bin"])
# ffmpeg failing during assembly gives a clear message, keeps the chapters, and exit code 6
Path("fakebin").mkdir(exist_ok=True)
Path("fakebin/ffmpeg").write_text("#!/bin/sh\nexit 3\n"); os.chmod("fakebin/ffmpeg", 0o755)
fresh(); r = run(["round.txt", "--out", "out_m", "--chapters", "1", "--keep-work"] + R, D, {"PATH": os.path.abspath("fakebin") + ":" + os.environ["PATH"]})
ok(r.returncode == 6 and "@problem ffmpeg failed" in r.stdout and "kept" in r.stdout, f"ffmpeg failure explained (exit {r.returncode}): {[l for l in r.stdout.splitlines() if l.startswith('@problem')][:1]}")
ok(any(Path("work").rglob("01.wav")), "the chapter audio is kept after an ffmpeg failure")
subprocess.run(["rm", "-rf", "fakebin"])
fresh(); r = run(["round.txt", "--out", "out_m", "--chapters", "1"] + R, D)
ok(r.returncode == 0, "and a normal conversion still works")
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
