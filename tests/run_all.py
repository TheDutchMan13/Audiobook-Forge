"""Run every automated test against the app files in the folder above.

    python tests/run_all.py            all suites (the window tour needs a display, or Xvfb on Linux)
    python tests/run_all.py convert    only suites whose name contains "convert"

Each suite runs in a fresh temporary folder holding a copy of the app, small sample books, and stand-ins
for Kokoro, torch and transformers (tests/stubs), so no graphics card, model download or real book is
needed. The stand-in voice "speaks" each word as a short tone and logs what it was asked to say.
Exit code 0 when everything passed. Needs ffmpeg, numpy, soundfile, mutagen, Pillow, pypdf, misaki.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
APP = TESTS.parent
SUITES = [  # (file, needs a display, timeout seconds)
    ("run_tests.py", False, 900), ("run_new.py", False, 900), ("run_round.py", False, 900),
    ("run_more.py", False, 900), ("run_final.py", False, 900), ("t_check.py", False, 600),
    ("test_ai.py", False, 300), ("test_norm.py", False, 300), ("test_watcher.py", False, 600), ("t_aiconv.py", False, 900), ("test_fixes.py", False, 900),
    ("tour.py", True, 1500),
]


def workspace():
    d = Path(tempfile.mkdtemp(prefix="forge-test-"))
    for f in APP.glob("*.py"):
        shutil.copy(f, d)
    for f in (TESTS / "stubs").iterdir():
        (shutil.copytree if f.is_dir() else shutil.copy)(f, d / f.name)
    for f in (TESTS / "fixtures").iterdir():
        (shutil.copytree if f.is_dir() else shutil.copy)(f, d / f.name)
    for f in (TESTS / "suites").glob("*.py"):
        shutil.copy(f, d)
    return d


def display_env():
    if os.name == "nt" or os.environ.get("DISPLAY"):
        return os.environ.copy(), None
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return None, None
    proc = subprocess.Popen([xvfb, ":77", "-screen", "0", "1400x1000x24"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    return dict(os.environ, DISPLAY=":77"), proc


def main():
    pick = sys.argv[1] if len(sys.argv) > 1 else ""
    total_pass = total_fail = 0
    failed_suites = []
    xproc = None
    for name, gui, timeout in SUITES:
        if pick and pick not in name:
            continue
        if not (TESTS / "suites" / name).exists():
            continue
        env = os.environ.copy()
        if gui:
            env, xproc = display_env()
            if env is None:
                print(f"--  {name}: skipped (no display; install Xvfb)")
                continue
        d = workspace()
        t = time.time()
        try:
            r = subprocess.run([sys.executable, name], cwd=d, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            text = r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            text = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            text += "\nTIMEOUT"
        passed = len(re.findall(r"^PASS", text, re.M))
        failed = len(re.findall(r"^FAIL", text, re.M))
        m = re.search(r"(\d+) passed, (\d+) failed", text)
        if m and not passed and not failed:
            passed, failed = int(m.group(1)), int(m.group(2))
        broken = "Traceback" in text and not passed or "TIMEOUT" in text or re.search(r"ERRORS: [1-9]", text)
        total_pass += passed
        total_fail += failed + (1 if broken else 0)
        mark = "ok" if not failed and not broken else "FAIL"
        print(f"{mark:4} {name}: {passed} passed, {failed} failed{' (crashed or timed out)' if broken else ''} "
              f"in {time.time() - t:.0f} s")
        if mark != "ok":
            failed_suites.append(name)
            for line in text.splitlines():
                if line.startswith("FAIL") or "Error" in line or "TIMEOUT" in line:
                    print("       " + line[:300])
            (TESTS / f"last-{name}.log").write_text(text, encoding="utf-8")
        shutil.rmtree(d, ignore_errors=True)
    if xproc:
        xproc.terminate()
    print(f"\n{total_pass} passed, {total_fail} failed" + (f"  (see tests/last-*.log for {', '.join(failed_suites)})"
                                                           if failed_suites else ""))
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
