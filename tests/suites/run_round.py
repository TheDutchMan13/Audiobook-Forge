import json, os, signal, subprocess, sys, time
from pathlib import Path
import numpy as np, soundfile as sf
PY = sys.executable
results = []
def ok(c, m):
    results.append(bool(c)); print(("PASS " if c else "FAIL ") + m, flush=True)
def run(args, opts=None, env=None, timeout=300):
    cmd = [PY, "convert.py", *args]
    if opts is not None: cmd += ["--options", json.dumps(opts)]
    return subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, **(env or {})), timeout=timeout)
def spoken():
    return [l.split("\t", 1) for l in Path("spoken.log").read_text(encoding="utf-8").splitlines()] if Path("spoken.log").exists() else []
def fresh():
    Path("spoken.log").unlink(missing_ok=True); subprocess.run(["rm", "-rf", "work", "out_r"])
def lufs(path):
    r = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-af", "ebur128=framelog=quiet", "-f", "null", "-"], capture_output=True, text=True)
    import re; return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])
def chapters_in(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", str(path)], capture_output=True, text=True)
    return [c["tags"]["title"] for c in json.loads(r.stdout)["chapters"]]
Path("rules.json").write_text(json.dumps({"rules": []}))
base = ["round.txt", "--out", "out_r", "--rules", "rules.json"]
D = {"dialogue_voice": ""}

# --- shouting + sanitizer reach the speech
fresh(); r = run(base + ["--keep-work"], D)
ok(r.returncode == 0, f"converts (exit {r.returncode}) {r.stdout[-300:] if r.returncode else ''}")
texts = " | ".join(t for _, t in spoken())
ok("stop right there" in texts and "FBI" in texts, "shouted words read as words, acronym kept")
# --- paragraph cache: a fix touching one paragraph re-speaks only that paragraph
n_first = len(spoken())
Path("spoken.log").unlink()
Path("rules.json").write_text(json.dumps({"rules": [{"find": "ferry", "say": "boat"}]}))
r = run(base + ["--keep-work"], D)
sp = spoken()
ok(r.returncode == 0 and len(sp) == 1 and "boat was late" in sp[0][1], f"changed fix re-speaks only the changed paragraph ({len(sp)} of {n_first} spoken)")
ok("paragraphs reused" in r.stdout, "the log says paragraphs were reused")
Path("rules.json").write_text(json.dumps({"rules": []}))
# --- cleanup without keep-work removes the paragraph audio too
fresh(); r = run(base, D)
ok(r.returncode == 0 and not Path("work").exists() or not any(Path("work").rglob("*.npy")), f"finished book leaves no paragraph audio behind: {list(Path('work').rglob('*'))[:5] if Path('work').exists() else []}")
# --- mid-chapter resume after a crash
fresh()
p = subprocess.Popen([PY, "convert.py", *base, "--chapters", "1", "--options", json.dumps(D)], stdout=subprocess.PIPE, text=True,
                     env=dict(os.environ, STUB_DELAY="0.3"))
seen = 0
for line in p.stdout:
    if line.startswith("@progress 1 ") and int(line.split()[2]) >= 4:
        p.kill(); break
p.wait()
done_before = len(list(Path("work").rglob("*.npy")))
Path("spoken.log").unlink(missing_ok=True)
r = run(base + ["--chapters", "1"], D)
ok(done_before >= 4 and r.returncode == 0 and f"{done_before} paragraphs reused" in r.stdout, f"resumes mid-chapter: {done_before} paragraphs kept after the crash -> {r.stdout.splitlines()[-3] if r.stdout else ''}")
# --- self-check
fresh(); r = run(base + ["--chapters", "1", "--keep-work"], D, {"STUB_TRUNCATE": "1"})
sp = [t for _, t in spoken()]
ok(r.returncode == 0 and "re-spoke 1" in r.stdout, f"self-check notices the swallowed paragraph: {[l for l in r.stdout.splitlines() if 'chapter 1' in l]}")
ok(any(t.strip() == "The radio hissed and went quiet." for t in sp), "it re-spoke that paragraph sentence by sentence")
fresh(); r = run(base + ["--chapters", "1"], dict(D, self_check=False), {"STUB_TRUNCATE": "1"})
ok("re-spoke" not in r.stdout, "self-check can be turned off")
# --- half precision
fresh(); r = run(base + ["--chapters", "2"], dict(D, half=True), {"STUB_CUDA": "1"})
ok(r.returncode == 0 and "half precision" in r.stdout and any(t == "[half]" for _, t in spoken()), "half precision runs the speech under autocast")
fresh(); r = run(base + ["--chapters", "2"], dict(D, half=True), {"STUB_CUDA": "1", "STUB_HALF_NAN": "1"})
ok(r.returncode == 0 and "continuing in full precision" in r.stdout and "Done in" in r.stdout, "broken half precision falls back to full precision")
fresh(); r = run(base + ["--chapters", "2"], dict(D, half=True))
ok(r.returncode == 0 and "needs the graphics card" in r.stdout, "half precision is skipped without a GPU")
# --- two chapters at once
fresh(); t = time.time(); r = run(base, D, {"STUB_DELAY": "0.15"}); t1 = time.time() - t
fresh(); t = time.time(); r = run(base, dict(D, parallel=True), {"STUB_DELAY": "0.15"}); t2 = time.time() - t
m4b = list(Path("out_r").glob("*.m4b"))
titles = [t for _, t in spoken() if t.startswith(("Chapter", "chapter"))]
ok(r.returncode == 0 and m4b and len(chapters_in(m4b[0])) == 4, f"two at once makes a complete book ({len(chapters_in(m4b[0])) if m4b else 0} chapters)")
ok(sorted(titles) == ["Chapter 1.", "Chapter 2.", "Chapter 3.", "Chapter 4."], f"every chapter spoken exactly once: {titles}")
ok(t2 < t1 * 0.85, f"two at once is faster when the GPU part is slow ({t1:.1f}s -> {t2:.1f}s)")
ok(not Path("work").exists() or not any(Path("work").rglob("*.*")), "two at once cleans up after itself")
# cancelling the main process also stops the second one
fresh()
p = subprocess.Popen([PY, "convert.py", *base, "--options", json.dumps(dict(D, parallel=True))], stdout=subprocess.PIPE, text=True,
                     env=dict(os.environ, STUB_DELAY="0.4"))
time.sleep(4); p.kill(); p.wait(); time.sleep(2)
left = subprocess.run(["pgrep", "-f", "convert.py round.txt"], capture_output=True, text=True).stdout.split()
ok(not left, f"cancel stops the second process too (left: {left})")
for pid in left: os.kill(int(pid), signal.SIGKILL)
# --- book loudness
fresh(); r = run(base, D)
m4b = list(Path("out_r").glob("*.m4b"))[0]; l_on = lufs(m4b)
ok("adjusted to -19 LUFS" in r.stdout and abs(l_on + 19) < 1.0, f"whole book matched to -19 LUFS ({l_on:.1f})")
fresh(); r = run(base, dict(D, book_loudness=False))
l_off = lufs(list(Path("out_r").glob("*.m4b"))[0])
ok("LUFS" not in r.stdout and abs(l_off - l_on) > 0.5, f"book loudness can be turned off ({l_off:.1f} LUFS)")
# --- room tone
fresh(); run(base + ["--chapters", "3", "--keep-work"], dict(D, room_tone=True))
a, _ = sf.read(str(next(Path("work").rglob("03.wav"))))
quiet = a[int(0.05 * 24000):int(0.25 * 24000)]
rms = 20 * np.log10(np.sqrt((quiet ** 2).mean()) + 1e-12)
ok(np.abs(quiet).max() > 0 and -72 < rms < -60, f"room tone fills the pauses at {rms:.1f} dB (under -60)")
fresh(); run(base + ["--chapters", "3", "--keep-work"], D)
a, _ = sf.read(str(next(Path("work").rglob("03.wav"))))
ok(np.abs(a[int(0.05 * 24000):int(0.25 * 24000)]).max() == 0, "without room tone pauses stay silent")
# --- he/she voices
fresh(); r = run(base + ["--chapters", "1"], {"dialogue_voice": "auto", "dialogue_female": "af_bella", "dialogue_male": "am_adam"})
sp = spoken()
fem = [t for v, t in sp if v == "af_bella"]; mal = [t for v, t in sp if v == "am_adam"]
ok(any("We need to leave" in t for t in fem) and any("Not yet" in t for t in mal), f"quotes go to the matching voice")
ok(any("take the small boat" in t for t in fem) and any("get ourselves killed" in t for t in mal), "untagged back-and-forth alternates speakers")
ok(any("she said" in t for v, t in sp if v == "af_heart"), "the narration stays with the narrator")
ok("(women)" in r.stdout, "the log says which voices read the speech")
# --- EPUB front/back matter and table of contents titles
fresh(); r = run(["matter.epub", "--out", "out_r", "--rules", "rules.json"], D)
m4b = list(Path("out_r").glob("*.m4b"))
ok(r.returncode == 0 and m4b and chapters_in(m4b[0]) == ["Chapter 1: The Storm", "Chapter 2: Second Wind", "Chapter 3: Three Bells"],
   f"EPUB: contents titles used, front/back matter skipped: {chapters_in(m4b[0]) if m4b else r.stdout[-300:]}")
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
