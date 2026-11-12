import json, os, subprocess, sys, time
from pathlib import Path
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
    Path("spoken.log").unlink(missing_ok=True); subprocess.run(["rm", "-rf", "work", "out_f", "casting", "voices", "logs"])
Path("rules.json").write_text(json.dumps({"rules": []}))
R = ["--rules", "rules.json"]; D = {"dialogue_voice": ""}

# --- keep awake: released while paused, taken back afterwards
sys.path.insert(0, ".")
import importlib, convert
calls = []
convert.stay_awake = lambda on: (calls.append(on), convert._awake.__setitem__("on", on))
convert._awake["on"] = True
Path("pausetest.flag").write_text("x")
import threading
threading.Timer(1.0, lambda: Path("pausetest.flag").unlink()).start()
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    convert.wait_while_paused("pausetest.flag")
ok(calls == [False, True], f"the PC may sleep while paused and is kept awake again after: {calls}")

# --- first-person narrator
fresh(); r = run(["first.txt", "--out", "out_f"] + R, {"dialogue_voice": "cast"})
sp = spoken(); v = lambda text: next((voice for voice, t in sp if text in t), None)
ok(v('"I know,"') == "af_heart" and v('"Painting."') == "af_heart" and v('"Since always."') == "af_heart"
   and v('"Maybe later,"') == "af_heart", "the narrator's own lines (I said, I shrugged, turn-taking) stay in the narrator's voice")
ok(v('"You are late,"') not in (None, "af_heart") and v('"Busy with what?"') == v('"You are late,"')
   and v('"Since when do you paint') == v('"You are late,"'), "the other speaker keeps their own voice across the exchange")
ok("(narrator)" not in r.stdout, "the narrator is not listed as a character")
fresh(); run(["first.txt", "--out", "out_f"] + R, {"dialogue_voice": "auto"})
sp = spoken()
ok(v('"I know,"') == "af_heart" and v('"You are late,"') == "am_michael", "he/she mode: narrator's lines stay with the narrator")

# --- voice mixer
fresh()
import voicemix
r = subprocess.run([PY, "voicemix.py", "make", "Warm Mix", "af_heart", "af_bella", "0.7"], capture_output=True, text=True,
                   env=dict(os.environ, HF_HOME=os.path.abspath("models")))
ok(r.returncode == 0 and Path("voices/Warm Mix.pt").exists() and "custom:Warm Mix" in r.stdout, f"a mix is saved: {r.stdout.strip()} {r.stderr[-200:]}")
r = subprocess.run([PY, "voicemix.py", "list"], capture_output=True, text=True)
ok("70% Heart + 30% Bella" in r.stdout, f"the mix is listed with its recipe: {r.stdout.strip()}")
r = run(["round.txt", "--out", "out_f", "--voice", "custom:Warm Mix", "--chapters", "2", "--keep-work"] + R, D)
sp = spoken()
ok(r.returncode == 0 and all(voice == "mix:Warm Mix" for voice, _ in sp) and sp, f"a mix can be the narrator: {set(x for x, _ in sp)}")
ok(any(d.name.startswith("audio-custom-Warm-Mix-") for d in Path("work/round").iterdir()), "its work folder name is valid on Windows")
Path("spoken.log").unlink()
run(["round.txt", "--out", "out_f", "--voice", "custom:Warm Mix", "--chapters", "2", "--keep-work"] + R, D)
ok(not spoken(), "the same mix again reuses everything")
subprocess.run([PY, "voicemix.py", "make", "Warm Mix", "af_heart", "af_bella", "0.4"], capture_output=True, env=dict(os.environ, HF_HOME=os.path.abspath("models")))
run(["round.txt", "--out", "out_f", "--voice", "custom:Warm Mix", "--chapters", "2", "--keep-work"] + R, D)
ok(len(spoken()) > 0, "changing the mix reads the book again with the new voice")
r = subprocess.run([PY, "voicemix.py", "try", "af_heart", "af_sky", "0.5", "out_f/try.wav"], capture_output=True, text=True,
                   env=dict(os.environ, HF_HOME=os.path.abspath("models")))
ok(r.returncode == 0 and Path("out_f/try.wav").exists(), f"a mix can be heard without saving it {r.stderr[-200:]}")
voicemix.delete("custom:Warm Mix")
fresh(); r = run(["round.txt", "--out", "out_f", "--voice", "custom:Warm Mix", "--chapters", "2"] + R, D)
ok(r.returncode == 0 and all(voice == "af_heart" for voice, _ in spoken()), "a deleted mix falls back to Heart instead of failing")

# --- listening report
fresh(); r = run(["report.txt", "--out", "out_f"] + R, D, {"STUB_TRUNCATE": "1", "STUB_STALL": "1"})
rep = Path("logs/reports/report - listening report.json")
data = json.loads(rep.read_text()) if rep.exists() else {}
kinds = sorted(e["kind"] for e in data.get("events", []))
ok("@report" in r.stdout and kinds == ["fixed", "silence"], f"report lists the re-read paragraph and the stall: {kinds}")
ok({n["word"] for n in data.get("names", [])} >= {"Natasha", "Bruce", "Thanos", "Wakanda"}, "report lists the unfamiliar words")
ok(Path("logs/reports/report - listening report.txt").exists(), "a readable text copy is saved too")
stall = next((e for e in data.get("events", []) if e["kind"] == "silence"), None)
ok(stall and stall["chapter"] == 2 and abs(stall["book_at"] - stall["at"] - 28.2) < 3, f"times are positions in the whole book: {stall}")
ok(not Path("work").exists() or not list(Path("work").rglob("*.json")), "chapter notes are cleaned up after a finished book")
# notes survive a reused paragraph
fresh(); run(["report.txt", "--out", "out_f", "--keep-work"] + R, D, {"STUB_TRUNCATE": "1", "STUB_STALL": "1"})
Path("rules.json").write_text(json.dumps({"rules": [{"find": "lantern", "say": "lamp"}]}))
run(["report.txt", "--out", "out_f", "--keep-work"] + R, D, {"STUB_TRUNCATE": "1", "STUB_STALL": "1"})
Path("rules.json").write_text(json.dumps({"rules": []}))
data = json.loads(Path("logs/reports/report - listening report.json").read_text())
ok(sorted(e["kind"] for e in data["events"]) == ["fixed", "silence"], "a re-read paragraph is still reported when its audio is reused")
# two at once: the second process's chapters are reported too
fresh(); run(["report.txt", "--out", "out_f"] + R, dict(D, parallel=True), {"STUB_TRUNCATE": "1", "STUB_STALL": "1"})
data = json.loads(Path("logs/reports/report - listening report.json").read_text())
ok(sorted(e["kind"] for e in data["events"]) == ["fixed", "silence"], "reports also cover chapters made by the second process")

# --- setup check
r = subprocess.run([PY, "selfcheck.py", "--json", "--idle"], capture_output=True, text=True, timeout=300)
line = next((l for l in r.stdout.splitlines() if l.startswith("@results ")), None)
res = json.loads(line[9:]) if line else []
titles = {x["title"] for x in res}
ok({"Program folder", "Graphics card", "Dictionary", "Save folder"} <= titles and any(t.startswith("ffmpeg") for t in titles),
   f"setup check covers the main parts: {sorted(titles)}")
ok(all(set(x) == {"status", "title", "found", "fix", "action"} for x in res), "every result has status, title, what was found, the fix")
Path("pause-gui.flag").write_text("x")
r = subprocess.run([PY, "selfcheck.py", "--json", "--idle"], capture_output=True, text=True, timeout=300)
res = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("@results "))[9:])
ok(any(x["action"] == "remove_flags" for x in res), "a leftover pause file is found and can be removed")
Path("pause-gui.flag").unlink()
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
