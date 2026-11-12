import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, ".")
import speakers
from books import read_book
PY = sys.executable
res = []
def ok(c, m): res.append(bool(c)); print(("PASS " if c else "FAIL ") + m, flush=True)
def run(opts, keep=False):
    Path("spoken.log").unlink(missing_ok=True)
    if not keep: subprocess.run(["rm", "-rf", "work"])
    return subprocess.run([PY, "convert.py", "cast.txt", "--out", "out_a", "--rules", "rules.json", "--keep-work", "--options", json.dumps(opts)],
                          capture_output=True, text=True, timeout=600)
def v_of(text):
    for l in Path("spoken.log").read_text(encoding="utf-8").splitlines():
        v, t = l.split("\t", 1)
        if text in t: return v
Path("rules.json").write_text(json.dumps({"rules": []}))
subprocess.run(["rm", "-rf", "work", "out_a", "casting", "logs"])
meta, chs = read_book("cast.txt")
ch = chs[0]
r = run({"dialogue_voice": "cast"})
base_next = v_of("Then we go without you")
base_dir = sorted(p.name for p in Path("work").glob("*/audio-*"))
ok("AI speaker tags" not in r.stdout, "no tags file: nothing said about AI tags")
idx = next(i for i, p in enumerate(ch.paragraphs) if "Then we go without you" in p)
pep = next(i for i, p in enumerate(ch.paragraphs) if "You are late" in p)
os.makedirs("casting", exist_ok=True)
data = {"version": 1, "model": "x", "chapters": {speakers.chapter_key(ch.paragraphs):
        {str(idx): ["Bruce", "m"], str(pep): ["Bruce", "m"]}}, "genders": {}}
Path("casting/cast.speakers.json").write_text(json.dumps(data))
r = run({"dialogue_voice": "cast"})
ok("Using the AI speaker tags for 1 of" in r.stdout, "log says the AI tags are used")
cast = r.stdout; print([l for l in cast.splitlines() if "voice" in l.lower()][:4])
nat = [l for l in cast.splitlines() if "Natasha (" in l]
ok(v_of("Then we go without you") != base_next and v_of("Then we go without you").startswith("am_"), f"untagged line follows the AI ({base_next} -> {v_of('Then we go without you')})")
ok(v_of("You are late") == v_of("We are listening"), "explicit 'Pepper said' still beats the AI")
r = run({"dialogue_voice": "cast", "ai_speakers": False}); r = run({"dialogue_voice": "cast"}, keep=True)
dirs = sorted(p.name for p in Path("work").glob("*/audio-*"))
ok(set(dirs) - set(base_dir), "the tags change the chapter fingerprint")
r = run({"dialogue_voice": "cast", "ai_speakers": False})
ok("AI speaker tags" not in r.stdout and v_of("Then we go without you") == base_next, "turning AI tags off goes back to the rules")
r = run({"dialogue_voice": "cast", "ai_speakers": False}, keep=True)
ok(not Path("spoken.log").exists() or not Path("spoken.log").read_text().strip(), "switching back reuses the old audio")
# auto (men/women) mode uses them too
r = run({"dialogue_voice": "auto"})
ok("Using the AI speaker tags" in r.stdout, "men/women voices mode uses the tags too")
# tags for other text are ignored
data["chapters"] = {"deadbeefdeadbeef": {"1": ["Bruce", "m"]}}
Path("casting/cast.speakers.json").write_text(json.dumps(data))
r = run({"dialogue_voice": "cast"})
ok("AI speaker tags" not in r.stdout and r.returncode == 0, "tags made for a different text are not used")
Path("casting/cast.speakers.json").write_text("{broken")
r = run({"dialogue_voice": "cast"})
ok(r.returncode == 0, "a broken tags file is ignored")
print(f"{sum(res)}/{len(res)}")
