import json, os, shutil, subprocess, sys, time
os.makedirs("watch", exist_ok=True)
from pathlib import Path
PY = sys.executable
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
Path("settings.json").write_text(json.dumps({"watch_enabled": True, "watch_dir": str(Path("watch").resolve()), "voice": "af_heart",
                                             "speed": 1.0, "out_dir": str(Path("books_out").resolve()), "sound": False}))
import watcher
orig_settle = 10
env = dict(os.environ, STUB_DELAY="0.01")
w = subprocess.Popen([PY, "watcher.py"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(2)
st = watcher.read_status()
ok(st and st["state"] == "idle", f"idle watcher reports status: {st and st['state']}")
# a second watcher refuses to start
w2 = subprocess.run([PY, "watcher.py"], env=env, capture_output=True, text=True, timeout=20)
ok("already running" in w2.stdout, "a second watcher does not start")
# drop three files: a good txt, a good docx, and an unreadable pdf
shutil.copy("chaptered.txt", "watch/story.txt")
shutil.copy("sample.docx", "watch/memo.docx")
shutil.copy("blank.pdf", "watch/scan.pdf")
(Path("watch") / "notes.png").write_bytes(b"x")            # not a book: must be ignored
# a file that is still being copied: it keeps growing for ~18 seconds
import threading
grow_state = {"done": False}
def grow():
    with open("watch/growing.md", "w") as f:
        f.write("# Heading\n\n")
    for i in range(6):
        with open("watch/growing.md", "a") as f:
            f.write(("Some text for the growing file %d. " % i) * 25 + "\n\n")
        time.sleep(3)
    grow_state["done"] = True
threading.Thread(target=grow, daemon=True).start()
t0 = time.time()
saw_converting = False
picked_up_while_growing = False
while time.time() - t0 < 80:
    st = watcher.read_status()
    if st and st["state"] == "converting": saw_converting = True
    if st and st.get("current") == "growing.md" and not grow_state["done"]: picked_up_while_growing = True
    if Path("watch/done/story.txt").exists() and Path("watch/done/memo.docx").exists() and Path("watch/failed/scan.pdf").exists():
        break
    time.sleep(0.5)
ok(saw_converting, "status showed 'converting' while working")
ok(Path("watch/done/story.txt").exists() and Path("watch/done/memo.docx").exists(), "good books moved to done/")
ok(Path("watch/failed/scan.pdf").exists(), "unreadable book moved to failed/")
note = Path("watch/failed/scan - what went wrong.txt")
ok(note.exists() and "no readable text" in note.read_text(), f"failure note explains why: {note.read_text()[:70] if note.exists() else None!r}")
outs = sorted(p.name for p in Path("books_out").glob("*.m4b"))
ok(len(outs) >= 2, f"audiobooks saved: {outs}")
ok(Path("watch/notes.png").exists() and not Path("watch/done/notes.png").exists(), "non-book files are left alone")
t0 = time.time()
while time.time() - t0 < 60 and not Path("watch/done/growing.md").exists(): time.sleep(1)
ok(not picked_up_while_growing, "a file that is still being copied is not converted early")
ok(Path("watch/done/growing.md").exists(), "the growing file is converted after it stops changing")
# GUI busy flag makes the watcher wait
Path("gui_busy.flag").write_text("x")
shutil.copy("notes.md", "watch/second.md")
time.sleep(16)
st = watcher.read_status()
ok(Path("watch/second.md").exists() and st and st["state"] == "waiting", f"waits while the window is converting: {st and st['state']}")
Path("gui_busy.flag").unlink()
t0 = time.time()
while time.time() - t0 < 40 and not Path("watch/done/second.md").exists(): time.sleep(1)
ok(Path("watch/done/second.md").exists(), "continues once the window is finished")
# stop file ends it cleanly
Path("watcher.stop").write_text("x")
try: w.wait(timeout=30)
except subprocess.TimeoutExpired: w.kill()
st = json.loads(Path("watcher_status.json").read_text())
ok(w.returncode == 0 and st["state"] == "stopped" and watcher.read_status() is None, f"stop file ends the watcher (exit {w.returncode}, state {st['state']})")
log = Path("watcher.log").read_text()
ok("finished story.txt" in log and "FAILED scan.pdf" in log, "log records what happened")
# turning the setting off also ends a running watcher
w = subprocess.Popen([PY, "watcher.py"], env=env, stdout=subprocess.PIPE, text=True); time.sleep(2)
s = json.loads(Path("settings.json").read_text()); s["watch_enabled"] = False; Path("settings.json").write_text(json.dumps(s))
try: w.wait(timeout=20)
except subprocess.TimeoutExpired: w.kill()
ok(w.returncode == 0, "disabling the setting ends the watcher")
