"""Checks before a long conversion: disk space and memory. Used by convert.py and the window.

Disk use while converting (24 kHz, 16-bit mono):
  chapter audio      about 173 MB per hour of audio (kept until the audiobook is assembled)
  paragraph audio    about 173 MB per hour (kept for resuming; deleted at the end unless kept on purpose)
  finished book      kbps * 450 KB per hour (64 kbps: about 29 MB per hour)
"""
import ctypes
import os
import shutil
from pathlib import Path

WORDS_PER_HOUR = 150 * 60
WORK_BYTES_PER_HOUR = 2 * 24000 * 2 * 3600       # chapter wav + paragraph pieces, 16-bit
MARGIN = 300 * 1024 ** 2
GB = 1024 ** 3


def free_space(path):
    """Free bytes on the drive that holds path (or would hold it once created). None if unknown."""
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free
    except OSError:
        return None


def drive_of(path):
    p = Path(path).resolve()
    return (p.drive or p.anchor or "/").upper()


def gb(n):
    return f"{n / GB:.1f} GB"


def disk_problem(work_dir, out_dir, words_todo, words_total, kbps=64):
    """A sentence explaining the shortage, or None when there is room."""
    hours_todo = words_todo / WORDS_PER_HOUR
    hours_total = words_total / WORDS_PER_HOUR
    need_work = int(hours_todo * WORK_BYTES_PER_HOUR) + MARGIN
    need_out = int(hours_total * kbps * 1000 / 8 * 3600) + 50 * 1024 ** 2
    needs = {}
    needs[drive_of(work_dir)] = needs.get(drive_of(work_dir), 0) + need_work
    needs[drive_of(out_dir)] = needs.get(drive_of(out_dir), 0) + need_out
    for drive, need in needs.items():
        where = work_dir if drive == drive_of(work_dir) else out_dir
        free = free_space(where)
        if free is not None and free < need:
            return (f"Not enough disk space on {drive.rstrip(chr(92)) or drive}: this book needs about {gb(need)} "
                    f"while converting, but only {gb(free)} is free. Free up some space or save to another drive.")
    return None


def ram_free():
    """Available memory in bytes, or None if it cannot be read."""
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return int(st.ullAvailPhys)
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001
        pass
    return None


def gpu_free(torch):
    """(free, total) graphics memory in bytes, or None."""
    try:
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return int(free), int(total)
    except Exception:  # noqa: BLE001
        pass
    return None


def memory_plan(torch, parallel):
    """(parallel still allowed, [notes for the log]) given free system and graphics memory.
    One converter needs roughly 2 GB of memory and 1 GB of graphics memory; two need twice that."""
    notes = []
    ram = ram_free()
    gpu = gpu_free(torch)
    if ram is not None and ram < 1.5 * GB:
        notes.append(f"Your PC is low on memory ({gb(ram)} free). Close other programs if the conversion stops.")
    if gpu is not None and gpu[0] < 1.2 * GB:
        notes.append(f"The graphics card is low on memory ({gb(gpu[0])} free). Close games or video programs if "
                     "the conversion stops.")
    if parallel:
        if ram is not None and ram < 4 * GB:
            notes.append(f"Two chapters at once is off for this run: only {gb(ram)} of memory is free.")
            parallel = False
        elif gpu is not None and gpu[0] < 3 * GB:
            notes.append(f"Two chapters at once is off for this run: only {gb(gpu[0])} of graphics memory is free.")
            parallel = False
    return parallel, notes


def is_disk_full(exc):
    text = str(exc).lower()
    return getattr(exc, "errno", None) == 28 or getattr(exc, "winerror", None) in (112, 39) or \
        "no space left" in text or "disk full" in text or "not enough space" in text


def is_out_of_memory(exc):
    text = f"{type(exc).__name__} {exc}".lower()
    return "out of memory" in text or "outofmemory" in text or "hiperrorouto" in text


_FFMPEG = {}


def ffmpeg_path(app_dir):
    """The ffmpeg in the app's bin folder if it really runs (a copy missing its DLLs is skipped), else 'ffmpeg'."""
    if "path" not in _FFMPEG:
        import subprocess
        bundled = Path(app_dir) / "bin" / "ffmpeg.exe"
        path = "ffmpeg"
        if bundled.exists():
            try:
                r = subprocess.run([str(bundled), "-hide_banner", "-version"], capture_output=True, timeout=30,
                                   creationflags=0x08000000 if os.name == "nt" else 0)
                if r.returncode == 0 and b"ffmpeg version" in r.stdout[:300]:
                    path = str(bundled)
            except Exception:  # noqa: BLE001
                pass
        _FFMPEG["path"] = path
    return _FFMPEG["path"]
