import json, sys, subprocess, os
from pathlib import Path
import numpy as np
import bookcheck
results = []
def ok(c, m):
    results.append(bool(c)); print(("PASS " if c else "FAIL ") + m, flush=True)

calls = {"clips": [], "durs": []}
def fake(clips, items):
    out = []
    for clip, it in zip(clips, items):
        calls["clips"].append(len(clip) / 16000); calls["durs"].append(it["dur"])
        text = it["said"]
        w = text.split()
        if "glitch" in text:
            text = " ".join(w[:-7])                         # the voice swallowed the end
        elif "keeper" in text:
            text = " ".join(w[:5] + w[11:])                 # a phrase in the middle is gone
        elif "Thanos" in text:
            text = text.replace("Thanos", "Tanos").replace("Wakanda", "Wakonda")   # names spelled differently
        out.append(text)
    return out

subprocess.run(["rm", "-rf", "work", "logs", "out_c"])
subprocess.run([sys.executable, "convert.py", "report.txt", "--out", "out_c", "--rules", "rules.json", "--options",
                json.dumps({"dialogue_voice": ""})], capture_output=True)
rep = Path("logs/reports/report - listening report.json")
res = bookcheck.check_book(rep, transcriber=fake)
kinds = {(e["kind"], e["said"][:20]) for e in res["issues"]}
print(kinds)
ok(("ending", "The lamp flickered t") in kinds, "a swallowed ending is found")
ok(any(k == "missing" and s.startswith("The keeper") for k, s in kinds), "words missing in the middle are found")
ok(not any(s.startswith("Natasha and Bruce") for _, s in kinds), "names spelled differently by Whisper are not flagged")
ok(len(res["issues"]) == 2, f"nothing else is flagged ({len(res['issues'])} issues)")
ok(all(abs(c - (d + 0.4)) < 0.1 for c, d in zip(calls["clips"], calls["durs"])), f"each clip is the paragraph's length plus a small margin: {list(zip(calls['clips'], calls['durs']))[:3]}")
ok(res["checked"] == 5 and res["skipped"] == 2, f"titles and very short items are skipped: checked {res['checked']}, skipped {res['skipped']}")
data = json.loads(rep.read_text())
ok("check" in data and data["events"] is not None, "results are saved in the report")
txt = rep.with_suffix(".txt").read_text()
ok("=== Checked by listening" in txt and "heard:" in txt, "the text report gets a Checked by listening section")
# a second check uses the saved transcripts
calls["clips"].clear()
res2 = bookcheck.check_book(rep, transcriber=fake)
ok(not calls["clips"] and len(res2["issues"]) == 2, "checking again reuses the saved transcripts")
txt2 = rep.with_suffix(".txt").read_text()
ok(txt2.count("=== Checked by listening") == 1, "the section is replaced, not added twice")
# timing-test mode: one chapter
calls["clips"].clear()
Path("logs/reports/report - transcripts.json").unlink()
res3 = bookcheck.check_book(rep, chapters={2}, transcriber=fake)
ok(res3["checked"] == 2 and all(e["chapter"] == 2 for e in res3["issues"]), "--chapters limits the check")
# long paragraphs are split under 30 s at quiet points
a = np.concatenate([np.sin(np.arange(16000 * 20) / 5) * 0.3, np.zeros(16000), np.sin(np.arange(16000 * 25) / 5) * 0.3]).astype(np.float32)
parts = bookcheck.split_long(a)
ok(len(parts) == 2 and all(len(p) <= 28 * 16000 for p in parts) and abs(len(parts[0]) / 16000 - 20.5) < 0.6, f"a 46 s paragraph is cut in the silence: {[round(len(p)/16000, 1) for p in parts]}")
# MP3 folder output
subprocess.run(["rm", "-rf", "work", "logs", "out_c"])
subprocess.run([sys.executable, "convert.py", "report.txt", "--out", "out_c", "--rules", "rules.json", "--options",
                json.dumps({"dialogue_voice": "", "format": "mp3_chapters"})], capture_output=True)
res4 = bookcheck.check_book(Path("logs/reports/report - listening report.json"), transcriber=fake)
ok(len(res4["issues"]) == 2, f"works on a folder of MP3s too ({len(res4['issues'])} issues)")
# comparison details
cmp = bookcheck.Comparer([{"find": "Okoye", "say": "oh-koy-ay"}])
ok(bookcheck.verdict(cmp.compare("Okoye drew her spear and smiled at the king today.", "Okoya drew her spear and smiled at the king today.")) is None,
   "words changed by a pronunciation fix are forgiven")
ok(bookcheck.verdict(cmp.compare("At 3 30 PM on June 5th, 2021 the ship finally sailed away.", "At three thirty p.m. on June fifth, twenty twenty-one, the ship finally sailed away.")) is None,
   "numbers written differently are forgiven")
ok(bookcheck.verdict(cmp.compare("Yes, she said.", "")) is None, "very short lines are not judged")
ok(bookcheck.verdict(cmp.compare("She walked slowly to the door and waited.", "")) is not None, "a line where nothing was heard is flagged")
ok(bookcheck.verdict(cmp.compare("The quiet harbor lay under a thin moon while the boats came in slowly.",
                                 "The quiet harbor lay under a thin moon while the boats came in slowly. Thank you for watching, please subscribe and like the video."))[0] == "extra",
   "invented extra words are noticed")
print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
