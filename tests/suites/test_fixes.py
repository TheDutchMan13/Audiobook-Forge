"""Tests for the fixes from the November 2026 audit, one section per batch."""
import json, os, shutil, stat, subprocess, sys, time, types
from pathlib import Path
PY = sys.executable
res = []
def ok(c, m): res.append(bool(c)); print(("PASS " if c else "FAIL ") + m, flush=True)
def run(args, env=None, timeout=300):
    e = dict(os.environ, **(env or {}))
    return subprocess.run([PY, "convert.py", *args], capture_output=True, text=True, env=e, timeout=timeout)

# ---------------------------------------------------------------- batch A: outputs and stalls
shutil.rmtree("out_a", ignore_errors=True); Path("logs/outputs.json").unlink(missing_ok=True)
Path("bookA.txt").write_text("Same Title\n\nChapter 1\n\n" + "The first book is about the harbor. " * 40)
Path("bookB.txt").write_text("Same Title\n\nChapter 1\n\n" + "The second book is about the forest. " * 40)
r1 = run(["bookA.txt", "--out", "out_a", "--title", "Same Title", "--author", "Writer"])
r2 = run(["bookB.txt", "--out", "out_a", "--title", "Same Title", "--author", "Writer"])
names = sorted(p.name for p in Path("out_a").iterdir())
ok(names == ["Writer - Same Title (2).m4b", "Writer - Same Title.m4b"], f"a different book with the same title gets its own file: {names}")
ok("made from a different book file" in r2.stdout, "the log says why the name changed")
r3 = run(["bookA.txt", "--out", "out_a", "--title", "Same Title", "--author", "Writer"])
ok(sorted(p.name for p in Path("out_a").iterdir()) == names and "different book" not in r3.stdout,
   "converting the same book again replaces its own file")
ok(not list(Path("out_a").glob("*.partial*")), "no .partial files are left after finishing")
ok("@assemble encoding" in r1.stdout, "assembly reports its progress (@assemble lines)")
ok(any(l.startswith("@log ") for l in r1.stdout.splitlines()), "the converter tells the window where its log is")

# a hanging ffmpeg (like the old hidden-dialog hang) is stopped, and no half-made audiobook is left
fake = Path("fakebin"); fake.mkdir(exist_ok=True)
real = shutil.which("ffmpeg")
(fake / "ffmpeg").write_text(f"""#!/bin/sh
for a in "$@"; do case "$a" in *.m4b) sleep 600; exit 1;; esac; done
exec {real} "$@"
""")
(fake / "ffmpeg").chmod(0o755)
shutil.rmtree("out_h", ignore_errors=True)
t = time.time()
r = run(["bookA.txt", "--out", "out_h"], env={"PATH": f"{fake.resolve()}:{os.environ['PATH']}", "FORGE_FFMPEG_STALL": "4"}, timeout=200)
took = time.time() - t
ok(r.returncode != 0 and "stopped responding" in r.stdout and took < 120, f"a hung ffmpeg is stopped ({took:.0f} s) with a clear message")
ok(not list(Path("out_h").glob("*.m4b")) if Path("out_h").exists() else True, "a stopped assembly leaves no broken audiobook behind")

# a book whose own title has nothing to do with its file name says so
Path("One Second After by William Forstchen.txt").write_text("Chapter 1\n\n" + "Words of the story go here. " * 40)
import convert
ok(convert.title_mismatch("One Year After", "One Second After by William R. Forstchen"), "mismatched title is noticed")
ok(not convert.title_mismatch("One Second After", "One Second After by William R. Forstchen"), "matching title is fine")
ok(not convert.title_mismatch("Project Hail Mary", "51248-0012"), "code-like file names are not compared")
ok(not convert.title_mismatch("Apple: The First 50 Years", "Apple_ The First 50 Years - David Pogue"), "punctuation differences are fine")

# the second converter is not waited on for ever
class FakeProc:
    def __init__(s): s.last_seen = time.time() - 9999; s.killed = False; s.code = None
    def poll(s): return s.code
    def kill(s): s.killed = True; s.code = -9
    def wait(s): return s.code
convert.WORKER_STALL = 1
fp = FakeProc(); t = time.time(); convert.wait_for_second(fp)
ok(fp.killed and time.time() - t < 10, "a silent second converter is stopped")
Path("pause.flag").write_text("x"); fp = FakeProc()
import threading
threading.Timer(3, lambda: Path("pause.flag").unlink()).start()
t = time.time(); convert.wait_for_second(fp, "pause.flag")
ok(fp.killed and time.time() - t >= 3, "a paused second converter is not counted as stalled")

# the window explains exit codes and stops a silent converter
os.environ.setdefault("DISPLAY", "")
import forge_gui as g
ok("graphics driver" in g.describe_exit(3221225477) and "sleep" in g.describe_exit(0xC000013A) and "code 7" in g.describe_exit(7),
   "exit codes become plain reasons")
killed = []
fake_app = types.SimpleNamespace(running=True, proc=types.SimpleNamespace(kill=lambda: killed.append(1)), paused=False,
                                 cancelled=False, stalled=False, problem=None, last_line_time=time.time() - 21 * 60,
                                 log_line=lambda t: None)
g.App.check_stalled(fake_app)
ok(killed and fake_app.stalled and "No progress for 20 minutes" in fake_app.problem, "a converter silent for 20 minutes is stopped")
killed.clear(); fake_app.stalled = False; fake_app.paused = True
g.App.check_stalled(fake_app)
ok(not killed, "a paused converter is never stopped")

print(f"{sum(res)} passed, {len(res) - sum(res)} failed")
