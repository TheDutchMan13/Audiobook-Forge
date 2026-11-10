"""Audiobook Forge watch folder: converts any book that appears in a folder, with no window needed.

Started and stopped from the window's Settings (or run by hand: python watcher.py). It reads
settings.json for the folder, voice, speed and save location, converts one book at a time with
convert.py, then moves the original into a 'done' folder (or 'failed' with a note on what went wrong).
"""
import json
import queue
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "settings.json"
STATUS = HERE / "watcher_status.json"
STOP_FILE = HERE / "watcher.stop"
GUI_BUSY = HERE / "gui_busy.flag"
LOG = HERE / "watcher.log"
SCAN_EVERY = 5          # seconds between looks at the folder
SETTLE = 10             # a file must be this old, and unchanged, before it is converted
STALE_STATUS = 30       # seconds without a status update = the watcher is not running
GUI_BUSY_MAX = 12 * 3600
STALL_MINUTES = 20      # a converter silent this long has hung
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
EXTENSIONS = {".epub", ".txt", ".md", ".markdown", ".docx", ".html", ".htm", ".pdf"}

PROG_RE = re.compile(r"^@progress (\d+) (\d+) (\d+)$")
OUT_RE = re.compile(r"^Done in .* -> (.+?)\s*$")


def log(message):
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.replace(LOG.with_suffix(".log.old"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_status(state, **extra):
    data = {"pid": os.getpid(), "updated": time.time(), "state": state, **extra}
    try:
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(STATUS)
    except OSError:
        pass


def read_status():
    """Used by the window: the watcher's last status, or None when it is not running."""
    try:
        data = json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if time.time() - data.get("updated", 0) > STALE_STATUS or data.get("state") == "stopped":
        return None
    return data


def unique(path):
    """path, or path with a time stamp when something with that name already exists."""
    if not path.exists():
        return path
    return path.with_name(f"{path.stem} {time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")


def candidates(folder):
    out = []
    try:
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(("~", ".")):
                out.append(p)
    except OSError:
        pass
    return out


def gui_is_converting():
    try:
        return GUI_BUSY.exists() and time.time() - GUI_BUSY.stat().st_mtime < GUI_BUSY_MAX
    except OSError:
        return False


def convert_one(book, settings, queued):
    """Run convert.py on one file. Returns (ok, output_path_or_None, last_lines)."""
    out_dir = settings.get("out_dir") or str(HERE / "output")
    cmd = [sys.executable, "-u", str(HERE / "convert.py"), str(book), "--voice", settings.get("voice", "af_heart"),
           "--speed", f"{float(settings.get('speed', 1.0)):.2f}", "--out", out_dir,
           "--pause-file", str(HERE / "pause-watcher.flag")]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    tail, output, last_write, pct = [], None, 0.0, 0
    lines = queue.Queue()

    def read():
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)
    threading.Thread(target=read, daemon=True).start()
    last_seen = time.time()
    while True:
        try:
            line = lines.get(timeout=5)
        except queue.Empty:
            if STOP_FILE.exists():
                proc.terminate()
            if time.time() - last_seen > STALL_MINUTES * 60 and not (HERE / "pause-watcher.flag").exists():
                tail.append(f"No progress for {STALL_MINUTES} minutes, so the conversion was stopped "
                            "(the graphics card or ffmpeg stopped responding).")
                proc.kill()
                last_seen = time.time()
            continue
        if line is None:
            break
        last_seen = time.time()
        line = line.rstrip()
        m = PROG_RE.match(line)
        if m:
            pct = int(int(m.group(2)) / max(1, int(m.group(3))) * 100)
        elif line:
            tail.append(line)
            tail = tail[-12:]
            mo = OUT_RE.search(line)
            if mo:
                output = mo.group(1)
        if time.time() - last_write > 2:
            write_status("converting", current=book.name, chapter_percent=pct, queued=queued)
            last_write = time.time()
        if STOP_FILE.exists():
            proc.terminate()
    proc.wait()
    return proc.returncode == 0 and output is not None and not STOP_FILE.exists(), output, tail


def chime(success):
    try:
        import winsound
        winsound.PlaySound("SystemAsterisk" if success else "SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:  # noqa: BLE001
        pass


def main():
    # only one watcher at a time
    existing = read_status()    # a fresh status means another watcher is alive (it updates every few seconds)
    if existing and existing.get("pid") != os.getpid():
        print("The watcher is already running.")
        return
    STOP_FILE.unlink(missing_ok=True)
    log("watcher started")
    seen = {}            # path -> (size, mtime) from the previous look, to know when a copy has finished
    done_count = 0
    last = {}
    while not STOP_FILE.exists():
        settings = load_settings()
        folder = Path(settings.get("watch_dir") or "")
        if not settings.get("watch_enabled") or not folder.is_dir():
            write_status("idle", reason="watching is turned off or the folder is missing", folder=str(folder), done=done_count, last=last)
            time.sleep(SCAN_EVERY)
            if not settings.get("watch_enabled"):
                break
            continue
        ready = []
        for p in candidates(folder):
            try:
                st = p.stat()
            except OSError:
                continue
            key = (st.st_size, st.st_mtime)
            if seen.get(p) == key and time.time() - st.st_mtime >= SETTLE:
                ready.append(p)
            seen[p] = key
        if not ready:
            write_status("idle", folder=str(folder), done=done_count, last=last)
            time.sleep(SCAN_EVERY)
            continue
        if gui_is_converting():
            write_status("waiting", reason="the window is converting a book", folder=str(folder), queued=len(ready), done=done_count, last=last)
            time.sleep(SCAN_EVERY)
            continue
        book = ready[0]
        write_status("converting", current=book.name, chapter_percent=0, queued=len(ready) - 1, folder=str(folder), done=done_count, last=last)
        log(f"converting {book.name}")
        try:
            ok, output, tail = convert_one(book, settings, len(ready) - 1)
        except Exception as e:  # noqa: BLE001
            ok, output, tail = False, None, [f"{type(e).__name__}: {e}"]
        if STOP_FILE.exists():
            log("stopped while converting; the book stays in the folder")
            break
        target_dir = folder / ("done" if ok else "failed")
        try:
            target_dir.mkdir(exist_ok=True)
            moved = unique(target_dir / book.name)
            shutil.move(str(book), str(moved))
            if not ok:
                (target_dir / (moved.stem + " - what went wrong.txt")).write_text("\n".join(tail), encoding="utf-8")
        except OSError as e:
            log(f"could not move {book.name}: {e}")
        seen.pop(book, None)
        done_count += 1 if ok else 0
        last = {"name": book.name, "ok": ok, "output": output}
        log(("finished " if ok else "FAILED ") + book.name + (f" -> {output}" if output else ""))
        if settings.get("sound", True):
            chime(ok)
    write_status("stopped", done=done_count)
    STOP_FILE.unlink(missing_ok=True)
    log("watcher stopped")


if __name__ == "__main__":
    main()
