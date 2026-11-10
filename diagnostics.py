"""Save diagnostics: one zip with what is needed to work out why something went wrong.

    runtime\\python\\python.exe diagnostics.py ["where.zip"]

Contains: the newest conversion logs, the window's log, the watch folder log, settings, pronunciation
fixes, versions of Windows / Python / the libraries, the graphics card and its memory, free disk
space, and a list of the program's files. It never contains books, audio or covers.
"""
import json
import os
import platform
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if os.name == "nt":      # a program that cannot start must fail, not wait behind a hidden dialog
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:  # noqa: BLE001
        pass
PACKAGES = ["torch", "kokoro", "misaki", "numpy", "soundfile", "spacy", "pypdf", "mutagen", "Pillow", "sv-ttk",
            "tkinterdnd2", "huggingface-hub", "rocm"]
GPU_PROBE = ("import json, torch\n"
             "d = {'torch': torch.__version__, 'hip': getattr(torch.version, 'hip', None),"
             " 'available': torch.cuda.is_available()}\n"
             "if d['available']:\n"
             "    d['devices'] = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]\n"
             "    f, t = torch.cuda.mem_get_info(); d['memory_free_gb'] = round(f / 2**30, 2); d['memory_total_gb'] = round(t / 2**30, 2)\n"
             "print(json.dumps(d))\n")


def _console_python():
    exe = Path(sys.executable)
    alt = exe.with_name("python.exe")
    return str(alt) if exe.name.lower() == "pythonw.exe" and alt.exists() else str(exe)


def _versions():
    from importlib import metadata
    out = {}
    for name in PACKAGES:
        try:
            out[name] = metadata.version(name)
        except Exception:  # noqa: BLE001
            out[name] = "not installed"
    return out


def _gpu():
    try:
        r = subprocess.run([_console_python(), "-c", GPU_PROBE], capture_output=True, text=True, timeout=180,
                           cwd=HERE, creationflags=0x08000000 if os.name == "nt" else 0)
        line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("{")), None)
        return json.loads(line) if line else {"error": (r.stderr or r.stdout)[-1500:]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _ffmpeg():
    import checks
    cmd = checks.ffmpeg_path(HERE)          # the bundled copy only if it really starts, as the converter decides
    try:
        r = subprocess.run([cmd, "-hide_banner", "-version"], capture_output=True, text=True, timeout=30,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return f"{cmd}: {(r.stdout.splitlines() or ['?'])[0]}"
    except Exception as e:  # noqa: BLE001
        return f"{cmd}: not found ({e})"


def _folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _listing():
    lines = []
    for p in sorted(HERE.iterdir(), key=lambda x: x.name.lower()):
        if p.is_dir():
            size = _folder_size(p) if p.name in ("work", "logs", "models", "bench", "output") else None
            lines.append(f"[folder] {p.name}" + (f"  {size / 2**20:,.0f} MB" if size is not None else ""))
        else:
            st = p.stat()
            lines.append(f"{p.name}  {st.st_size:,} bytes  {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}")
    return "\n".join(lines)


def _tail(path, limit=200_000):
    data = Path(path).read_bytes()
    return data[-limit:]


def make_zip(dest=None, window_log=None, progress=None):
    """Write the diagnostics zip and return its path. progress(text) is told what is happening."""
    say = progress or (lambda t: None)
    import checks
    settings = {}
    try:
        settings = json.loads((HERE / "settings.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    if dest is None:
        desktop = Path.home() / "Desktop"
        dest = (desktop if desktop.exists() else HERE) / f"Audiobook Forge diagnostics {time.strftime('%Y-%m-%d %H-%M')}.zip"
    dest = Path(dest)
    say("Checking the graphics card...")
    gpu = _gpu()
    say("Running the setup check...")
    setup = None
    try:
        r = subprocess.run([_console_python(), str(HERE / "selfcheck.py"), "--json"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=HERE,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        line = next((l for l in r.stdout.splitlines() if l.startswith("@results ")), None)
        setup = json.loads(line[len("@results "):]) if line else {"error": (r.stdout + r.stderr)[-1500:]}
    except Exception as e:  # noqa: BLE001
        setup = {"error": str(e)}
    say("Collecting versions and logs...")
    out_dir = settings.get("out_dir") or str(HERE / "output")
    ram = checks.ram_free()
    info = {
        "made": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_folder": str(HERE),
        "windows": platform.platform(),
        "python": sys.version,
        "cpu_threads": os.cpu_count(),
        "memory_free_gb": round(ram / 2**30, 2) if ram else None,
        "disk_free_gb": {"app": round((checks.free_space(HERE) or 0) / 2**30, 1),
                         "output": round((checks.free_space(out_dir) or 0) / 2**30, 1)},
        "graphics": gpu,
        "ffmpeg": _ffmpeg(),
        "packages": _versions(),
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(info, indent=2))
        z.writestr("files.txt", _listing())
        z.writestr("setup check.json", json.dumps(setup, indent=2))
        for name in ("settings.json", "pronunciations.json", "watcher_status.json"):
            if (HERE / name).exists():
                z.write(HERE / name, name)
        if (HERE / "watcher.log").exists():
            z.writestr("watcher.log", _tail(HERE / "watcher.log"))
        logs = sorted((HERE / "logs").glob("*.txt"))[-5:] if (HERE / "logs").exists() else []
        for f in logs:
            z.writestr(f"logs/{f.name}", _tail(f))
        if window_log:
            z.writestr("window log.txt", window_log[-200_000:])
    say("")
    return dest


if __name__ == "__main__":
    path = make_zip(sys.argv[1] if len(sys.argv) > 1 else None, progress=lambda t: t and print(t, flush=True))
    print(f"Saved: {path}")
