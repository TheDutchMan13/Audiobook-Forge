"""Check my setup: is everything Audiobook Forge needs present and working?

    runtime\\python\\python.exe selfcheck.py            # print the results
    runtime\\python\\python.exe selfcheck.py --speak    # also say a test sentence on the graphics card
    ... --json                                         # for the window
    ... --idle                                         # the window is not converting (leftover flag files are stale)

Each result: status ok / warn / fail / info, a title, what was found, and what to do about it.
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

import checks  # noqa: E402

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HOME", str(HERE / "models"))
warnings.filterwarnings("ignore")
if os.name == "nt":      # a program that cannot start must fail, not wait behind a hidden dialog
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:  # noqa: BLE001
        pass
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INSTALL = f'uv pip install --python "{Path(sys.executable).with_name("python.exe")}" --break-system-packages'
GB = 1024 ** 3


def result(status, title, found, fix="", action=None):
    return {"status": status, "title": title, "found": found, "fix": fix, "action": action}


def settings():
    try:
        return json.loads((HERE / "settings.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------------------------------- checks
def check_folder():
    if " " in str(HERE):
        return result("fail", "Program folder", f"{HERE} has a space in its path.",
                      "Move the program to a folder without spaces, such as C:\\Audiobook-Forge. The graphics card "
                      "libraries cannot start from a path with a space.")
    return result("ok", "Program folder", str(HERE))


def check_python():
    bundled = (HERE / "runtime" / "python") in Path(sys.executable).resolve().parents
    return result("ok" if bundled else "info", "Python",
                  f"{sys.version.split()[0]} ({'the copy inside the program' if bundled else sys.executable})")


def check_packages():
    out = []
    need = [("torch", "the graphics card library"), ("kokoro", "the voice"), ("misaki", "the voice's dictionary"),
            ("numpy", ""), ("soundfile", ""), ("PIL", "covers and pictures"), ("sv_ttk", "the window's look"),
            ("tkinterdnd2", "drag and drop")]
    optional = [("pypdf", "reading PDF files", "pypdf"), ("mutagen", "the Library tab and chapter art in MP3s", "mutagen")]
    missing = []
    for mod, what in need:
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{mod} ({what}): {type(e).__name__}")
    if missing:
        out.append(result("fail", "Required parts", "Missing or broken: " + "; ".join(missing),
                          "Reinstall the program, or install the missing parts into runtime\\python."))
    else:
        out.append(result("ok", "Required parts", "torch, kokoro, misaki, numpy, soundfile, Pillow, theme, drag and drop"))
    for mod, what, pip_name in optional:
        try:
            importlib.import_module(mod)
            out.append(result("ok", f"Optional: {mod}", f"installed (used for {what})"))
        except Exception:  # noqa: BLE001
            out.append(result("warn", f"Optional: {mod}", f"not installed, so {what} will not work.",
                              f"In PowerShell, in the program folder, run:  {INSTALL} {pip_name}"))
    try:
        import spacy
        spacy.load("en_core_web_sm")
        out.append(result("ok", "Grammar model", "en_core_web_sm (helps with words like read / read)"))
    except Exception:  # noqa: BLE001
        out.append(result("warn", "Grammar model", "en_core_web_sm is not installed; words spelled alike but said "
                          "differently may be read wrong.", "Install the en_core_web_sm wheel into runtime\\python."))
    return out


def check_gpu():
    try:
        import torch
    except Exception as e:  # noqa: BLE001
        return [result("fail", "Graphics card", f"torch could not load: {e}", "Reinstall the program.")]
    if not torch.cuda.is_available():
        return [result("fail", "Graphics card", "No supported graphics card was found, so the voice runs on the "
                       "processor (many times slower).",
                       "Audiobook Forge needs an AMD Radeon RX 9000 series card with a recent AMD Software: Adrenalin "
                       "driver. Update the driver and restart the PC.")]
    name = torch.cuda.get_device_name(0)
    hip = getattr(torch.version, "hip", None)
    found = f"{name}" + (f", ROCm/HIP {hip}" if hip else "")
    try:
        free, total = torch.cuda.mem_get_info()
        found += f", {free / GB:.1f} of {total / GB:.1f} GB graphics memory free"
        if free < 1.5 * GB:
            return [result("warn", "Graphics card", found, "Close games or video programs before converting.")]
    except Exception:  # noqa: BLE001
        pass
    try:
        checks.fast_gpu(torch)     # cuDNN off: the card's real speed (see checks.py)
        x = torch.randn(512, 512, device="cuda")
        (x @ x).sum().item()
    except Exception as e:  # noqa: BLE001
        return [result("fail", "Graphics card", f"{found}, but a test calculation failed: {e}",
                       "Update the AMD driver and restart the PC. Save diagnostics and send them if it keeps failing.")]
    return [result("ok", "Graphics card", found)]


def check_model():
    hub = HERE / "models" / "hub"
    snap = list(hub.glob("models--hexgrad--Kokoro-82M/snapshots/*")) if hub.exists() else []
    if not snap:
        return [result("warn", "Voice model", "not downloaded yet",
                       "It downloads by itself (about 330 MB) the first time you convert or press Hear it; this needs "
                       "an internet connection once.")]
    voices = sorted({p.stem for s in snap for p in (s / "voices").glob("*.pt")})
    return [result("ok", "Voice model", f"Kokoro-82M, {len(voices)} voices downloaded"),
            result("info", "Downloaded voices", ", ".join(voices) or "none yet",
                   "Other voices download by themselves the first time they are used.")]


def _ffmpeg_report(path):
    try:
        r = subprocess.run([path, "-hide_banner", "-version"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        if r.returncode != 0 or b"ffmpeg version" not in r.stdout[:300]:
            return None, f"exit code {r.returncode & 0xFFFFFFFF:#x}"
        version = r.stdout.decode("utf-8", "replace").split("\n")[0].replace("ffmpeg version ", "").split(" Copyright")[0][:60]
        enc = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        flt = subprocess.run([path, "-hide_banner", "-filters"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        missing = [n for n, txt in (("aac", enc.stdout), ("libmp3lame", enc.stdout), ("ebur128", flt.stdout),
                                    ("alimiter", flt.stdout)) if n.encode() not in txt]
        return version, missing
    except FileNotFoundError:
        return None, "not found"
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def check_ffmpeg():
    out = []
    bundled = HERE / "bin" / "ffmpeg.exe"
    use_bundled = False
    if bundled.exists():
        version, info = _ffmpeg_report(str(bundled))
        if version:
            use_bundled = True
            out.append(result("ok", "ffmpeg (program's copy)", f"{version} in {bundled.parent}"))
        else:
            out.append(result("warn", "ffmpeg (program's copy)", f"bin\\ffmpeg.exe does not start ({info}). Usually its "
                              "DLL files are missing. The ffmpeg installed on this PC is used instead.",
                              "Run installer\\Build installer.bat again (it now copies the DLLs), or set the broken copy "
                              "aside with the button below.", action="set_aside_bin"))
    if not use_bundled:
        version, info = _ffmpeg_report("ffmpeg")
        if not version:
            out.append(result("fail", "ffmpeg", f"no working ffmpeg ({info}), so audiobooks cannot be assembled.",
                              "Install it:  winget install Gyan.FFmpeg   then restart Audiobook Forge."))
            return out
        out.append(result("ok", "ffmpeg (installed on this PC)", version))
        missing = info
    else:
        missing = _ffmpeg_report(str(bundled))[1]
    if missing:
        out.append(result("warn", "ffmpeg features", "missing: " + ", ".join(missing) +
                          " (needed for M4B/MP3 files and the loudness standard)",
                          "Install a full ffmpeg build:  winget install Gyan.FFmpeg"))
    return out


def check_dictionary():
    try:
        import normalize
        ok = normalize.known_word("the")
        if ok:
            return result("ok", "Dictionary", "loaded (numbers, names and SHOUTED words are handled)")
        return result("warn", "Dictionary", "could not be loaded; the name check and some text clean-up are off",
                      "Reinstall the program.")
    except Exception as e:  # noqa: BLE001
        return result("warn", "Dictionary", f"error: {e}", "Reinstall the program.")


def _size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def check_disk(cfg):
    out = []
    out_dir = Path(cfg.get("out_dir") or HERE / "output")
    for label, where in (("Program drive", HERE), ("Save folder drive", out_dir)):
        free = checks.free_space(where)
        if free is None:
            continue
        status = "fail" if free < GB else "warn" if free < 5 * GB else "ok"
        out.append(result(status, label, f"{free / GB:.1f} GB free on {checks.drive_of(where)}",
                          "" if status == "ok" else "A book needs about 0.4 GB per hour of audio while converting."))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".forge_write_test"
        probe.write_text("x")
        probe.unlink()
        out.append(result("ok", "Save folder", str(out_dir)))
    except OSError as e:
        out.append(result("fail", "Save folder", f"{out_dir} cannot be written to ({e})",
                          "Choose another save folder in the main window."))
    work = HERE / "work"
    if work.exists():
        size = _size(work)
        out.append(result("warn" if size > 10 * GB else "info", "Work files", f"{size / GB:.1f} GB in the work folder",
                          "Settings > Sound > Clean up work files... frees this space." if size > GB else "",
                          action="cleanup" if size > GB else None))
    return out


def check_flags(idle):
    out = []
    if not idle:
        return out
    stale = [f for f in ("pause-gui.flag", "gui_busy.flag") if (HERE / f).exists()]
    if stale:
        out.append(result("warn", "Leftover files", ", ".join(stale) + " left over from an earlier run. A leftover "
                          "pause file makes the next conversion wait; a leftover busy file stops the watch folder.",
                          "Remove them with the button below.", action="remove_flags"))
    return out


def check_settings(cfg):
    out = []
    voices = [cfg.get("voice", "af_heart"), cfg.get("dialogue_voice") or ""]
    import voicemix
    gone = [v for v in voices if voicemix.is_custom(v) and not voicemix.path_of(v).exists()]
    if gone:
        out.append(result("warn", "Voices", f"{', '.join(gone)} no longer exists; Heart is used instead.",
                          "Pick another voice in the main window."))
    if cfg.get("watch_enabled") and cfg.get("watch_dir") and not Path(cfg["watch_dir"]).exists():
        out.append(result("warn", "Watch folder", f"{cfg['watch_dir']} does not exist.", "Choose it again in Settings > Watch folder."))
    return out


def check_speech():
    """Say a test sentence on the graphics card and measure the speed (after a warm-up)."""
    try:
        import torch
        checks.fast_gpu(torch)     # cuDNN off: the card's real speed (see checks.py)
        from kokoro import KPipeline
        device = "cuda" if torch.cuda.is_available() else "cpu"
        pipe = KPipeline(lang_code="a", device=device)
        text = ("This is a short test of the voice. If you can read this, Audiobook Forge can turn your books into "
                "audiobooks on this computer.")
        for _ in pipe("Warming up.", voice="af_heart"):
            pass
        t = time.time()
        n = sum(len(a) for _, _, a in pipe(text, voice="af_heart") if a is not None)
        took = max(0.001, time.time() - t)
        speed = n / 24000 / took
        status = "ok" if (device == "cuda" and speed >= 8) or (device == "cpu" and speed >= 1) else "warn"
        return result(status, "Test sentence", f"{n / 24000:.1f} s of speech in {took:.1f} s ({speed:.0f}x realtime, {device})",
                      "" if status == "ok" else "Slower than expected. Close programs that use the processor or "
                      "graphics card (video encoding, games) and check again.")
    except Exception as e:  # noqa: BLE001
        return result("fail", "Test sentence", f"the voice could not speak: {type(e).__name__}: {e}",
                      "Save diagnostics and send them.")


def run(speak=False, idle=True, progress=None):
    say = progress or (lambda t: None)
    cfg = settings()
    results = []
    say("Checking the program folder and parts...")
    results.append(check_folder())
    results.append(check_python())
    results += check_packages()
    say("Checking the graphics card...")
    results += check_gpu()
    results += check_model()
    say("Checking ffmpeg...")
    results += check_ffmpeg()
    results.append(check_dictionary())
    say("Checking disk space...")
    results += check_disk(cfg)
    results += check_flags(idle)
    results += check_settings(cfg)
    if speak:
        say("Saying a test sentence...")
        results.append(check_speech())
    return results


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    res = run(speak="--speak" in args, idle="--idle" in args,
              progress=None if as_json else (lambda t: print(t, flush=True)))
    if as_json:
        print("@results " + json.dumps(res))
        return
    icons = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "    "}
    print()
    for r in res:
        print(f"{icons[r['status']]}  {r['title']}: {r['found']}")
        if r["fix"]:
            print(f"        -> {r['fix']}")
    bad = sum(r["status"] == "fail" for r in res)
    print(f"\n{bad} problem{'s' if bad != 1 else ''} found." if bad else "\nEverything needed is working.")


if __name__ == "__main__":
    main()
