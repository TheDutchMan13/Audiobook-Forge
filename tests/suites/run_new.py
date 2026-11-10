import json, os, subprocess, sys, time
from pathlib import Path
import numpy as np, soundfile as sf
PY = sys.executable
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
def run(args, opts=None, env=None):
    cmd = [PY, "convert.py", *args]
    if opts is not None: cmd += ["--options", json.dumps(opts)]
    return subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, **(env or {})), timeout=300)
def spoken():
    return [l.split("\t", 1) for l in Path("spoken.log").read_text(encoding="utf-8").splitlines()] if Path("spoken.log").exists() else []
def fresh():
    for p in ["spoken.log"]: Path(p).unlink(missing_ok=True)
    subprocess.run(["rm", "-rf", "work", "out_n"])
def silent_runs(a, thresh=1e-4, min_len=0.25):
    q = np.abs(a) < thresh; runs = []; start = None
    for i, v in enumerate(np.append(q, False)):
        if v and start is None: start = i
        elif not v and start is not None:
            if (i - start) / 24000 >= min_len: runs.append(round((i - start) / 24000, 2))
            start = None
    return runs
def level_db(a):
    f = a[: len(a) // 1200 * 1200].reshape(-1, 1200); r = np.sqrt((f ** 2).mean(1) + 1e-12); v = r[20 * np.log10(r) > -45]
    return 20 * np.log10(np.sqrt((v ** 2).mean()))
Path("rules.json").write_text(json.dumps({"rules": []}))
base = ["story.txt", "--out", "out_n", "--rules", "rules.json", "--keep-work"]

# 1. everything on (defaults), no dialogue voice
fresh(); r = run(base, {"dialogue_voice": ""})
ok(r.returncode == 0, f"converts with all improvements on (exit {r.returncode}) {r.stdout[-200:] if r.returncode else ''}")
texts = " | ".join(t for _, t in spoken())
ok("Doctor Smith" in texts and "3 30 PM" in texts and "June 5th, 1990" in texts and "2.5 million dollars" in texts, "text normalization reached the speech")
ok("[lead](/l" in texts, "heteronym fix reached the speech (lead case)")
ok("[4]" not in texts and "Figure 3" not in texts, "footnote marker and caption are not spoken")
ok("* * *" not in texts, "scene break is not spoken")
ok("5 kilometers" in texts, "units expanded")
wav1 = sorted(Path("work/story").glob("audio-*/01.wav"))[0]
a, sr = sf.read(str(wav1)); runs = silent_runs(a)
ok(any(1.5 <= x <= 1.8 for x in runs), f"scene break leaves a long pause: silences {runs}")
ok(max(runs) < 2.0 and min(runs) >= 0.25, "trimmed: no gap carries the model's extra 0.6 s of padding")
# 2. trimming off -> the model's padding (0.3 s each side) adds up between paragraphs
fresh(); run(base, {"trim": False, "dialogue_voice": ""})
a2, _ = sf.read(str(sorted(Path("work/story").glob("audio-*/01.wav"))[0]))
ok(len(a2) > len(a) + 24000 * 2, f"without trimming the chapter is longer ({len(a2)/24000:.1f}s vs {len(a)/24000:.1f}s)")
# 3. loudness: a quiet voice (af_heart 0.08) and a loud one (am_adam 0.6) end up at the same level
fresh(); run(base, {"dialogue_voice": ""}); qa, _ = sf.read(str(sorted(Path("work/story").glob("audio-*/01.wav"))[0]))
fresh(); run(["story.txt", "--out", "out_n", "--rules", "rules.json", "--keep-work", "--voice", "am_adam"], {"dialogue_voice": ""})
la, _ = sf.read(str(sorted(Path("work/story").glob("audio-*/01.wav"))[0]))
ok(abs(level_db(qa) - level_db(la)) < 1.0 and abs(level_db(qa) + 20) < 1.5, f"loudness evened: quiet voice {level_db(qa):.1f} dB, loud voice {level_db(la):.1f} dB")
fresh(); run(base, {"loudness": False, "dialogue_voice": ""}); ua, _ = sf.read(str(sorted(Path("work/story").glob("audio-*/01.wav"))[0]))
ok(level_db(ua) < -23, f"with loudness off the quiet voice stays quiet ({level_db(ua):.1f} dB)")
# 4. dialogue voice
fresh(); run(base, {"dialogue_voice": "am_adam"})
sp = spoken()
quotes = [t for v, t in sp if v == "am_adam"]; narr = [t for v, t in sp if v == "af_heart"]
ok(any("We should leave" in t for t in quotes) and any("I knew you would" in t for t in quotes), "quoted speech is read by the dialogue voice")
ok(any("said Mara" in t for t in narr) and any("He nodded." in t for t in narr), "narration around quotes stays with the narrator")
ok(any("so-called" in t for t in narr) and not any("so-called" in t for t in quotes), "short scare quotes stay with the narrator")
# 5. fast mode: same audio, less time, when text work is slow
env = {"STUB_G2P": "0.12", "STUB_DELAY": "0.12"}
fresh(); t = time.time(); run(base, {"fast": False, "dialogue_voice": ""}, env); t_norm = time.time() - t
na = [sf.read(str(p))[0] for p in sorted(Path("work/story").glob("audio-*/*.wav"))]
fresh(); t = time.time(); r = run(base, {"fast": True, "dialogue_voice": ""}, env); t_fast = time.time() - t
fa = [sf.read(str(p))[0] for p in sorted(Path("work/story").glob("audio-*/*.wav"))]
ok(r.returncode == 0 and "faster mode" in r.stdout, "faster mode runs")
ok(len(na) == len(fa) and all(len(x) == len(y) and np.allclose(x, y, atol=1e-5) for x, y in zip(na, fa)), "faster mode produces identical audio")
ok(t_fast < t_norm * 0.85, f"faster mode is faster when text work is slow ({t_norm:.1f}s -> {t_fast:.1f}s)")
# 6. fast mode falls back cleanly if Kokoro's internals are different than expected
Path("kokoro_backup.py").write_text(Path("kokoro.py").read_text())
Path("kokoro.py").write_text(Path("kokoro.py").read_text().replace("def g2p(self, text):", "def _g2p(self, text):").replace("_, tokens = self.g2p(text)", "_, tokens = self._g2p(text)"))
fresh(); r = run(base, {"fast": True, "dialogue_voice": ""})
Path("kokoro.py").write_text(Path("kokoro_backup.py").read_text()); Path("kokoro_backup.py").unlink()
ok(r.returncode == 0 and "continuing in normal mode" in r.stdout, "faster mode falls back to normal mode if it fails")
# 7. cache: options that change the sound re-render, faster mode does not
fresh(); run(base, {"dialogue_voice": ""})
r = run(base, {"dialogue_voice": "", "fast": True}); ok(r.stdout.count("already done") == 2, "turning faster mode on reuses cached chapters")
r = run(base, {"dialogue_voice": "", "natural_pauses": False}); ok(r.stdout.count("already done") == 0, "changing pauses re-renders")
# 8. everything off still works (plain behavior)
fresh(); r = run(base, {"normalize": False, "clutter": False, "heteronyms": False, "natural_pauses": False, "trim": False, "loudness": False, "dialogue_voice": ""})
texts = " | ".join(t for _, t in spoken())
ok(r.returncode == 0 and "Dr. Smith" in texts and "[4]" in texts, "with every option off the text is read as written")
