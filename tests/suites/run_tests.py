import json, os, subprocess, sys, threading, time
from pathlib import Path
PY = sys.executable
def run(args, env=None, timeout=120):
    e = dict(os.environ, **(env or {}))
    return subprocess.run([PY, "convert.py", *args], capture_output=True, text=True, env=e, timeout=timeout)
def probe(f):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:stream_disposition=attached_pic:format=duration:format_tags=title,artist",
                        "-show_chapters", "-of", "json", str(f)], capture_output=True, text=True)
    return json.loads(r.stdout)
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)

# 1. every format converts to an m4b with chapter markers and a cover
for src, nchap in [("chaptered.txt", 5), ("notes.md", 4), ("sample.docx", 3), ("page.html", 3), ("plain.pdf", 4), ("noheadings.txt", 3)]:
    out = f"out_{Path(src).stem}"
    r = run([src, "--out", out])
    files = list(Path(out).glob("*.m4b"))
    info = probe(files[0]) if files else {}
    has_cover = any(s.get("disposition", {}).get("attached_pic") for s in info.get("streams", []))
    ok(r.returncode == 0 and files and len(info.get("chapters", [])) == nchap and has_cover,
       f"{src}: {len(info.get('chapters', []))} chapters (want {nchap}), cover={has_cover}, file={files[0].name if files else None}")

# 2. pronunciation rules are applied to the spoken text, and change the cached-audio folder
Path("rules.json").write_text(json.dumps({"rules": [{"find": "harbor", "say": "HARBOUR"}, {"find": "history", "say": "HISTORY!", "enabled": False}]}))
Path("spoken.log").unlink(missing_ok=True)
r = run(["chaptered.txt", "--out", "out_rules", "--rules", "rules.json", "--keep-work"])
spoken = Path("spoken.log").read_text()
ok("HARBOUR" in spoken and "harbor" not in spoken.lower().replace("harbour", "") and "HISTORY!" not in spoken, "rules applied; disabled rule ignored")
ok("Using 1 pronunciation fix." in r.stdout, "the run says how many fixes it is using")
dirs = sorted(p.name for p in Path("work/chaptered").glob("audio-*"))
ok(len(dirs) == 1, f"one cache folder for this voice and these fixes: {dirs}")
# same rules again -> everything cached
r2 = run(["chaptered.txt", "--out", "out_rules", "--rules", "rules.json", "--keep-work"])
ok(r2.stdout.count("already done") == 5, f"same rules reuse cached chapters ({r2.stdout.count('already done')}/5)")
# changed rule -> re-rendered, not reused
Path("rules.json").write_text(json.dumps({"rules": [{"find": "harbor", "say": "HARBOR TWO"}]}))
r3 = run(["chaptered.txt", "--out", "out_rules", "--rules", "rules.json", "--keep-work"])
ok(r3.stdout.count("already done") == 0 and "HARBOR TWO" in Path("spoken.log").read_text(), "a changed rule re-renders instead of reusing old audio")

# 3. pause: with the flag present the run waits; removing it lets the run finish
Path("pause.flag").write_text("x")
proc = subprocess.Popen([PY, "convert.py", "chaptered.txt", "--out", "out_pause", "--pause-file", "pause.flag"],
                        stdout=subprocess.PIPE, text=True, env=dict(os.environ, STUB_DELAY="0.05"))
lines, stop = [], False
def reader():
    for l in proc.stdout: lines.append(l.rstrip())
threading.Thread(target=reader, daemon=True).start()
time.sleep(3.0)
progress_while_paused = [l for l in lines if l.startswith("@progress")]
ok(any(l == "@paused 1" for l in lines) and not progress_while_paused, f"paused before the first paragraph: {len(progress_while_paused)} progress lines while paused")
Path("pause.flag").unlink()
proc.wait(timeout=60)
ok(proc.returncode == 0 and "@paused 0" in lines and Path("out_pause").glob("*.m4b") is not None, "resumes and finishes after the flag is removed")
# pause in the middle: create the flag after some progress
proc = subprocess.Popen([PY, "convert.py", "chaptered.txt", "--out", "out_pause2", "--pause-file", "pause.flag"],
                        stdout=subprocess.PIPE, text=True, env=dict(os.environ, STUB_DELAY="0.2"))
lines = []
threading.Thread(target=lambda: [lines.append(l.rstrip()) for l in proc.stdout], daemon=True).start()
time.sleep(2.0); Path("pause.flag").write_text("x"); time.sleep(1.0)
n1 = len([l for l in lines if l.startswith("@progress")]); time.sleep(2.0)
n2 = len([l for l in lines if l.startswith("@progress")])
ok(n2 - n1 <= 1 and n1 > 3, f"mid-run pause stops progress ({n1} -> {n2} lines over 2 s)")
Path("pause.flag").unlink(); proc.wait(timeout=90)
ok(proc.returncode == 0, "mid-run pause then resume finishes")

# 4. problems are reported clearly with exit code 2
r = run(["blank.pdf", "--out", "out_err"])
ok(r.returncode == 2 and "no readable text" in r.stdout, f"unreadable PDF: exit {r.returncode}: {r.stdout.strip()[:80]}")
r = run(["nothing.mobi", "--out", "out_err"])
ok(r.returncode == 2 and "Calibre" in r.stdout, "unsupported format explains what to do")

# 5. output naming without an author
names = sorted(p.name for p in Path("out_chaptered").glob("*.m4b"))
ok(names and not names[0].startswith("book - ") and not names[0].startswith(" - "), f"no author -> plain title: {names}")
