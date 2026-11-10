"""Audiobook Forge - the window.

Convert tab: drop one EPUB (pick chapters) or several (a queue), choose voice / speed / folder,
watch progress, play the result. Library tab: every audiobook in the output folder.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
    from tkinterdnd2 import DND_FILES, TkinterDnD
    import sv_ttk
    from PIL import Image, ImageOps, ImageTk
except ImportError:  # lets the helper functions below be tested without a display
    tk = None

from books import BookError, SUPPORTED, describe_unsupported, is_supported, read_book, read_book_cover
from epub_reader import safe_name
import checks
import normalize
import pronunciations as pron
import themes
import voicemix
import watcher as watchmod

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "settings.json"
PREVIEW_DIR = APP_DIR / "models" / "previews"
CUSTOM_COVERS = APP_DIR / "custom_covers"
THEME_CACHE = APP_DIR / "themes"
VOICES = {
    "af_heart": "Heart (American, female)", "af_bella": "Bella (American, female)",
    "af_nicole": "Nicole (American, female)", "af_sarah": "Sarah (American, female)",
    "af_sky": "Sky (American, female)", "af_aoede": "Aoede (American, female)",
    "af_kore": "Kore (American, female)", "am_adam": "Adam (American, male)",
    "am_michael": "Michael (American, male)", "am_fenrir": "Fenrir (American, male)",
    "am_puck": "Puck (American, male)",
    "af_alloy": "Alloy (American, female)", "af_nova": "Nova (American, female)",
    "af_jessica": "Jessica (American, female)", "af_river": "River (American, female)",
    "am_echo": "Echo (American, male)", "am_eric": "Eric (American, male)", "am_liam": "Liam (American, male)",
    "am_onyx": "Onyx (American, male)",
}
ACCENT_LABELS = {"blue": "Blue (Windows)", "teal": "Teal", "purple": "Purple", "orange": "Orange",
                 "pink": "Pink", "green": "Green"}
TEXT_SIZES = {"smaller": ("Smaller", 0.9), "normal": ("Normal", 1.0), "larger": ("Larger", 1.15),
              "largest": ("Largest", 1.3)}
DONE_RE = re.compile(r"^\[(\d+)/(\d+)\] chapter (\d+):")
MIN_RE = re.compile(r"- ([\d.]+) min of audio|already done \(([\d.]+) min\)")
PROG_RE = re.compile(r"^@progress (\d+) (\d+) (\d+)$")
SUM_RE = re.compile(r"^@summary ([\d.]+)$")
OUT_RE = re.compile(r"^Done in .* -> (.+?)\s*$")
FORMATS = {"m4b": ("M4B audiobook (recommended)", "One file with chapters. Works in Apple Books, Audiobookshelf, "
                                                    "Smart AudioBook Player, BookPlayer and most audiobook apps."),
           "mp3": ("One MP3 file with chapters", "For players that do not open M4B. Chapter marks work in many, not all, players."),
           "mp3_chapters": ("A folder of MP3s, one per chapter", "Numbered files plus a playlist. Works on almost anything, "
                                                                 "including old MP3 players and car stereos.")}
QUALITIES = {"small": (48, "Small"), "standard": (64, "Standard (recommended)"), "high": (96, "High"), "best": (128, "Best")}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # no console window for helpers (Windows only)
DETACHED = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # the watcher keeps running after the window closes
PAUSE_FILE = APP_DIR / "pause-gui.flag"
QUEUE_FILE = APP_DIR / "queue.json"       # the running queue, so it can be resumed after a crash or restart
GUI_BUSY = APP_DIR / "gui_busy.flag"
PASTED_DIR = APP_DIR / "pasted"
BOOK_PATTERNS = " ".join(f"*{e}" for e in sorted(SUPPORTED))
FOLDER_LIMIT = 300            # more books than this from folders at once is almost certainly the wrong folder
FORMAT_RANK = {".epub": 0, ".docx": 1, ".pdf": 2, ".md": 3, ".markdown": 3, ".html": 4, ".htm": 4, ".txt": 5}


def natural_key(path):
    """'Book 2' before 'Book 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]


def expand_books(paths, limit=FOLDER_LIMIT):
    """The books in a list of dropped or chosen paths, looking inside folders (and their subfolders).
    The same book in several formats in one folder (Book.epub + Book.pdf) is taken once, the best format.
    Returns (files, info): info has "folders", "unsupported" (names of other files seen), "duplicates", "capped"."""
    info = {"folders": 0, "unsupported": [], "duplicates": 0, "capped": False}
    found = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            info["folders"] += 1
            inside = []
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted((d for d in dirs if not d.startswith(".") and d.lower() not in ("__macosx", "$recycle.bin")),
                                 key=natural_key)
                for name in names:
                    if name.startswith((".", "~$")):
                        continue
                    f = Path(root) / name
                    if is_supported(f):
                        inside.append(f)
                    elif f.suffix.lower() in (".mobi", ".azw", ".azw3", ".kfx", ".lit", ".fb2", ".cbz", ".cbr", ".djvu", ".rtf", ".doc"):
                        info["unsupported"].append(name)
            best = {}
            for f in inside:
                key = (f.parent, f.stem.lower())
                if key in best:
                    info["duplicates"] += 1
                    if FORMAT_RANK.get(f.suffix.lower(), 9) >= FORMAT_RANK.get(best[key].suffix.lower(), 9):
                        continue
                best[key] = f
            found += sorted(best.values(), key=natural_key)
        elif is_supported(path):
            found.append(path)
        else:
            info["unsupported"].append(path.name)
    seen, files = set(), []
    for f in found:
        k = str(f.resolve()).lower() if f.exists() else str(f).lower()
        if k not in seen:
            seen.add(k)
            files.append(str(f))
    if len(files) > limit:
        files, info["capped"] = files[:limit], True
    return files, info


def nothing_found_message(dropped, info):
    if info["folders"]:
        where = "that folder" if info["folders"] == 1 else "those folders"
        msg = f"No books were found in {where}"
        if info["unsupported"]:
            kinds = sorted({Path(n).suffix.lower() for n in info["unsupported"]})
            msg += f" (only {', '.join(kinds)} files, which need converting to EPUB first; the free Calibre app does this)"
        return msg + "."
    return describe_unsupported(dropped[0]) if dropped else ""
FORMATS_TEXT = "EPUB, PDF, Word, text, Markdown or web page"
AFTER_ACTIONS = {"nothing": "Do nothing", "close": "Close Audiobook Forge", "sleep": "Sleep",
                 "hibernate": "Hibernate", "shutdown": "Shut down"}
AFTER_VERBS = {"close": "Closing Audiobook Forge", "sleep": "Going to sleep", "hibernate": "Hibernating",
               "shutdown": "Shutting down"}
STARTUP_NAME = "Audiobook Forge watch folder.vbs"
SAME_VOICE = "Same as narrator"
BUILTIN_VOICES = list(VOICES)


def load_custom_voices():
    """Add the saved mixes to VOICES (and drop deleted ones)."""
    for vid in [v for v in VOICES if voicemix.is_custom(v)]:
        del VOICES[vid]
    for vid, name, desc in voicemix.list_custom():
        VOICES[vid] = f"\u2605 {name} (your mix)"


load_custom_voices()
AUTO_VOICES = "Match the speaker (he/she)"
CAST_VOICES = "Each character their own voice"
CASTING_DIR = APP_DIR / "casting"


def voice_label(vid):
    """Readable name for a voice or a blend of voices ("af_bella,af_sarah")."""
    if vid in VOICES:
        return VOICES[vid]
    parts = [VOICES.get(v, v).split(" (")[0] for v in vid.split(",")]
    kind = "female" if all(v.startswith("af_") for v in vid.split(",")) else \
        "male" if all(v.startswith("am_") for v in vid.split(",")) else "mixed"
    return " + ".join(parts) + f" (blend, {kind})"
FEMALE_VOICES = [v for v in VOICES if v.startswith("af_")]
MALE_VOICES = [v for v in VOICES if v.startswith("am_")]
# (key, label, help, default)
TEXT_OPTIONS = [
    ("normalize", "Read numbers, dates and abbreviations naturally",
     "\"June 5\" becomes \"June fifth\", \"Dr. Smith\" becomes \"Doctor Smith\", \"$2.5M\" becomes \"2.5 million dollars\".", True),
    ("clutter", "Skip footnote markers, web links and captions",
     "Raised footnote numbers, [12] style references, URLs and \"Figure 3:\" captions are not read.", True),
    ("heteronyms", "Fix words with two pronunciations",
     "For example \"lead\" the metal and \"bass\" the instrument. Most others are already handled.", True),
    ("shouting", "Read SHOUTED words as words",
     "\"STOP RIGHT THERE\" is read normally instead of letter by letter. Real acronyms like FBI are still spelled.", True),
    ("natural_pauses", "Natural pauses", "Longer pauses at scene breaks and headings, shorter ones between lines of dialogue.", True),
]
SOUND_OPTIONS = [
    ("trim", "Trim stray silences", "Removes the uneven gaps the voice sometimes leaves around sentences.", True),
    ("self_check", "Re-check paragraphs that sound cut off",
     "If a paragraph comes out far shorter (or longer) than its text should take, it is read again sentence by sentence.", True),
    ("loudness", "Even out loudness", "Every chapter plays at the same volume.", True),
    ("book_loudness", "Audiobook loudness standard",
     "Measures the whole finished book and sets it to the level audiobook stores use, with peaks kept under -3 dB.", True),
    ("room_tone", "Soft room tone",
     "A faint, natural background under the voice instead of dead digital silence in pauses. Some listeners prefer it.", False),
]
# "fast" (faster mode) is no longer shown: measured on the RX 9070 XT it gave no gain. It still works if set.
SPEED_OPTIONS = [
    ("half", "Half-precision math (about 1.8x faster)",
     "Lighter 16-bit math on the graphics card. Measured on an RX 9070 XT: 37-39x realtime instead of 18-21x. It can "
     "change the sound very slightly, so listen with Compare by ear below. Falls back to full precision if anything "
     "goes wrong.", False),
    ("parallel", "Two chapters at once",
     "Runs a second converter beside the first: about a third faster with full precision on an RX 9070 XT, and more "
     "graphics memory. Turned off by itself when memory is short.", False),
]
QUALITY_OPTIONS = [(k, l, h) for k, l, h, _ in TEXT_OPTIONS + SOUND_OPTIONS]
CHECKED, UNCHECKED = "☑", "☐"
SPEED_X = 20     # measured realtime factor, used for estimates before real progress exists
STALL_MINUTES = 20   # a converter that prints nothing for this long (not paused) has hung: it is stopped
ASM_RE = re.compile(r"^@assemble (\w+) (\d+) (\d+)$")
EXIT_CODES = {
    0xC0000005: "the converter crashed (an access violation, usually inside the graphics driver)",
    0xC0000409: "the converter crashed (a library stopped it on purpose after an internal error)",
    0xC00000FD: "the converter crashed (it ran out of stack memory)",
    0xC0000017: "the PC ran out of memory",
    0xC000013A: "the converter was closed from outside (the PC went to sleep or shut down, or it was ended in Task Manager)",
    0xC0000135: "a program file (DLL) is missing; run Check my setup",
}


def describe_exit(code):
    """A plain reason for a converter that ended with this exit code."""
    if code is None:
        return "the converter stopped"
    c = code & 0xFFFFFFFF
    if c in EXIT_CODES:
        return EXIT_CODES[c]
    if c == 1:
        return "the converter stopped with an error"
    return f"the converter stopped unexpectedly (code {c:#x})" if c > 255 else f"the converter stopped (code {c})"
WORDS_PER_MIN = 150


def compress_ranges(nums):
    """[1,2,3,5,7,8] -> '1-3,5,7-8'"""
    nums = sorted(nums)
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_length(minutes):
    if minutes < 0.5:
        return "<1 min"
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_settings(data):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_mp3_info(path):
    """(title, author, length, cover) from an MP3 book or a folder of chapter MP3s (album = book title)."""
    from mutagen.mp3 import MP3
    path = Path(path)
    files = sorted(path.glob("*.mp3")) if path.is_dir() else [path]
    title = author = cover = None
    length = 0.0
    for i, f in enumerate(files):
        m = MP3(str(f))
        length += m.info.length
        if i == 0 and m.tags:
            tag = m.tags.get("TALB") if path.is_dir() else (m.tags.get("TIT2") or m.tags.get("TALB"))
            title = str(tag.text[0]) if tag else None
            art = m.tags.get("TPE1")
            author = str(art.text[0]) if art else None
            pics = m.tags.getall("APIC")
            cover = bytes(pics[0].data) if pics else None
    return title, author, length or None, cover


def read_m4b_info(path):
    """(title, author, length_seconds, cover_bytes) for an audiobook; missing parts are None."""
    title = author = length = cover = None
    if Path(path).is_dir() or Path(path).suffix.lower() == ".mp3":
        try:
            title, author, length, cover = read_mp3_info(path)
        except Exception:  # noqa: BLE001
            pass
    try:
        if Path(path).is_dir() or Path(path).suffix.lower() == ".mp3":
            raise ValueError("not an M4B")
        from mutagen.mp4 import MP4
        m = MP4(str(path))
        tags = m.tags or {}
        title = (tags.get("\xa9nam") or [None])[0]
        author = (tags.get("\xa9ART") or [None])[0]
        covers = tags.get("covr") or []
        cover = bytes(covers[0]) if covers else None
        length = m.info.length
    except Exception:  # noqa: BLE001  (mutagen missing or an odd file: show what we can)
        pass
    if not title:
        stem = Path(path).stem
        if " - " in stem:
            author_guess, title = stem.split(" - ", 1)
            author = author or author_guess
        else:
            title = stem
    return title, author, length, cover


class WinTaskbar:
    """Progress on the taskbar button and a flash when done. Quietly does nothing off Windows."""
    NOPROGRESS, INDETERMINATE, NORMAL, ERROR, PAUSED = 0, 1, 2, 4, 8

    def __init__(self, root):
        self.root = root
        self.ok = False
        try:
            import ctypes
            from ctypes import wintypes
            self.ctypes = ctypes
            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

            def guid(text):
                g = GUID()
                ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
                return g

            clsid = guid("{56FDF344-FD6D-11d0-958A-006097C9A090}")   # TaskbarList
            iid = guid("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")     # ITaskbarList3
            ptr = ctypes.c_void_p()
            if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr)) != 0 or not ptr:
                return
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            fn = ctypes.WINFUNCTYPE
            self._init = fn(ctypes.c_long, ctypes.c_void_p)(vtbl[3])
            self._value = fn(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong)(vtbl[9])
            self._state = fn(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_int)(vtbl[10])
            self.ptr = ptr
            self._init(ptr)
            self.ok = True
        except Exception:  # noqa: BLE001
            self.ok = False

    def _hwnd(self):
        return int(self.root.wm_frame(), 16)

    def progress(self, pct, state=None):
        if self.ok:
            try:
                self._state(self.ptr, self._hwnd(), state or self.NORMAL)
                self._value(self.ptr, self._hwnd(), int(max(0, min(100, pct)) * 10), 1000)
            except Exception:  # noqa: BLE001
                pass

    def set_state(self, state):
        if self.ok:
            try:
                self._state(self.ptr, self._hwnd(), state)
            except Exception:  # noqa: BLE001
                pass

    def flash(self):
        try:
            ctypes = self.ctypes if self.ok else __import__("ctypes")
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                            ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), self._hwnd(), 3 | 12, 0, 0)  # flash until focused
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:  # noqa: BLE001
            pass


class App:
    def __init__(self, root, first_book=None):
        self.root = root
        self.settings = load_settings()
        self.book = None
        self.meta = {}
        self.chapters = []
        self.selected = set()
        self.overrides = {}
        self.proc = None
        self.loading_batch = False      # books from a folder are being read in the background
        self.ai_busy = False            # the language model is using the graphics card (speakers or pronunciations)
        self.q = queue.Queue()
        self.ui_q = queue.Queue()      # work handed back to the window by background threads
        self.running = False
        self.cancelled = False
        self.cover_img = None
        self.preview_busy = False
        self.batch = []            # [(path, meta, chapters)] when several books are dropped
        self.batch_i = 0
        self.batch_results = []    # [(title, output path or None, why it failed, index in the queue)]
        self.batch_active = False
        self.batch_start = 0.0
        self.library_dirty = True
        self.library_items = []
        self.library_images = []
        self.paused = False
        self.sample_busy = False
        self.after_var = None
        self.reset_run_state()

        root.title("Audiobook Forge")
        try:
            root.iconbitmap(str(APP_DIR / "forge.ico"))
        except Exception:  # noqa: BLE001
            pass
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.text_factor = TEXT_SIZES.get(self.settings.get("text_size", "normal"), TEXT_SIZES["normal"])[1]
        geo = self.settings.get("geometry")
        root.geometry(geo if geo else f"{int(1100 * self.scale)}x{int(800 * self.scale)}")
        root.minsize(int(960 * self.scale), int(680 * self.scale))

        self.accent = self.settings.get("accent", "blue")
        try:
            tcl = themes.prepare(self.accent, THEME_CACHE)
            if tcl:
                root.tk.call("source", str(tcl))
                root._sv_ttk_loaded = True   # tell sv_ttk the (recolored) theme is already loaded
        except Exception:  # noqa: BLE001  (any problem: fall back to the standard blue theme)
            self.accent = "blue"
        self.theme = self.settings.get("theme", "light")
        sv_ttk.set_theme(self.theme)
        self.make_fonts()
        self.build_ui()
        self.apply_style()
        self.show_view("ready")
        # load the voice's dictionary in the background, so "as it will be read" and name checks are quick
        threading.Thread(target=normalize._lexicon, daemon=True).start()
        self.taskbar = WinTaskbar(root)
        for leftover in (PAUSE_FILE, GUI_BUSY):        # from a window that was closed unexpectedly
            try:
                leftover.unlink()
            except OSError:
                pass
        if self.settings.get("watch_enabled") and self.settings.get("watch_dir") and not watchmod.read_status():
            self.start_watcher()
        self.watch_checked = 0.0
        root.bind("<Control-v>", self.on_paste_key)
        root.bind("<Control-V>", self.on_paste_key)

        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<DropEnter>>", self.on_drag_enter)
        root.dnd_bind("<<DropPosition>>", lambda e: e.action)
        root.dnd_bind("<<DropLeave>>", self.on_drag_leave)
        root.dnd_bind("<<Drop>>", self.on_drop)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(250, self.poll)
        self.resuming = False
        if first_book:
            self.open_paths([first_book]) if Path(first_book).is_dir() else self.load_book(first_book)
        else:
            root.after(800, self.offer_resume)

    # ------------------------------------------------------------------ fonts & style
    def make_fonts(self):
        have = set(tkfont.families(self.root))

        def pick(*names):
            return next((n for n in names if n in have), names[-1])

        display = pick("Segoe UI Variable Display Semibold", "Segoe UI Semibold", "DejaVu Sans")
        text = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "DejaVu Sans")
        self.f_app = tkfont.Font(root=self.root, name="ForgeApp", family=display, size=-22)
        self.f_book = tkfont.Font(root=self.root, name="ForgeBook", family=display, size=-19)
        self.f_big = tkfont.Font(root=self.root, name="ForgeBig", family=display, size=-32)
        self.f_done = tkfont.Font(root=self.root, name="ForgeDone", family=display, size=-26)
        self.f_strong = tkfont.Font(root=self.root, name="ForgeStrong", family=text, size=-15)
        self.f_mono = tkfont.Font(root=self.root, name="ForgeMono", family=pick("Consolas", "Cascadia Mono", "DejaVu Sans Mono"), size=-12)
        self.f_symbol = tkfont.Font(root=self.root, name="ForgeSymbol", family=pick("Segoe UI Symbol", "DejaVu Sans"), size=-34)
        self.f_drop = tkfont.Font(root=self.root, name="ForgeDrop", family=display, size=-30)
        for f in (self.f_app, self.f_book, self.f_big, self.f_done, self.f_strong, self.f_drop):
            f.configure(weight="bold" if display == "DejaVu Sans" else "normal")

    def scale_fonts(self):
        """Pixel-sized fonts (the theme's and ours) follow display scaling and the text-size setting."""
        if not hasattr(self, "_font_base"):
            self._font_base = {}
        for name in tkfont.names(self.root):
            f = tkfont.nametofont(name, root=self.root)
            base = self._font_base.setdefault(name, int(f.cget("size")))
            if base < 0:
                f.configure(size=int(round(base * self.scale * self.text_factor)))
            elif base > 0:
                f.configure(size=max(6, int(round(base * self.text_factor))))

    def theme_bg(self):
        return ttk.Style().lookup(".", "background") or ("#1c1c1c" if self.theme == "dark" else "#fafafa")

    def apply_style(self):
        style = ttk.Style()
        self.scale_fonts()
        try:
            body = tkfont.nametofont(style.lookup("Treeview", "font") or "TkDefaultFont", root=self.root)
        except tk.TclError:
            body = tkfont.nametofont("TkDefaultFont", root=self.root)
        style.configure("Treeview", rowheight=int(body.metrics("linespace") * 1.7))
        dark = sv_ttk.get_theme() == "dark"
        self.theme = "dark" if dark else "light"
        bg = self.theme_bg()
        accent = themes.accent_color(self.accent, self.theme)
        self.theme_btn.config(text="Light mode" if dark else "Dark mode")
        log_bg, log_fg = ("#101317", "#A3ACB8") if dark else ("#F1F2F4", "#3A424C")
        self.log.config(background=log_bg, foreground=log_fg, insertbackground=log_fg)
        self.done_icon.config(background=bg, foreground=accent)
        self.cover_label.config(background=bg)
        self.overlay.config(background=bg, highlightbackground=accent, highlightcolor=accent,
                            highlightthickness=max(3, int(3 * self.scale)))
        for w in (self.overlay_title, self.overlay_sub):
            w.config(background=bg)
        self.overlay_title.config(foreground=accent)
        self.overlay_sub.config(foreground="#A3ACB8" if dark else "#5B6472")
        self.lib_canvas.config(background=bg)
        for lbl in self.library_images_labels():
            lbl.config(background=bg)
        cover_wrap = int(170 * self.scale * self.text_factor)
        for w in (self.title_lbl, self.author_lbl, self.chips_lbl):
            w.config(wraplength=cover_wrap)

    def toggle_theme(self):
        self.set_theme("light" if self.theme == "dark" else "dark")

    def set_theme(self, mode):
        sv_ttk.set_theme(mode)
        self.theme = mode
        self.settings["theme"] = mode
        save_settings(self.settings)
        self.apply_style()

    # ------------------------------------------------------------------ layout
    def build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 12, 18, 0))
        header.pack(fill="x")
        ttk.Label(header, text="Audiobook Forge", font=self.f_app).pack(side="left")
        ttk.Button(header, text="Settings", command=self.open_settings).pack(side="right")
        self.theme_btn = ttk.Button(header, text="", command=self.toggle_theme)
        self.theme_btn.pack(side="right", padx=8)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(8, 16))
        convert_tab = ttk.Frame(self.nb, padding=12)
        self.library_tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(convert_tab, text="   Convert   ")
        self.nb.add(self.library_tab, text="   Library   ")
        self.nb.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        convert_tab.columnconfigure(1, weight=1)
        convert_tab.rowconfigure(0, weight=1)
        self.left = ttk.Frame(convert_tab, width=int(340 * self.scale))
        self.left.grid(row=0, column=0, sticky="ns", padx=(0, 16))
        self.left.pack_propagate(False)
        self.right = ttk.Frame(convert_tab)
        self.right.grid(row=0, column=1, sticky="nsew")

        # --- left: book card, settings / run card, drop hint
        self.book_card = ttk.LabelFrame(self.left, text="Book", padding=12)
        self.book_card.pack(fill="x")
        row = ttk.Frame(self.book_card)
        row.pack(fill="x")
        self.cover_label = tk.Label(row, bd=0)
        self.cover_label.pack(side="left", anchor="n")
        info = ttk.Frame(row)
        info.pack(side="left", fill="x", expand=True, padx=(12, 0), anchor="n")
        self.title_lbl = ttk.Label(info, text="No book yet", font=self.f_book)
        self.title_lbl.pack(anchor="w")
        self.author_lbl = ttk.Label(info, text="Drop a book in this window")
        self.author_lbl.pack(anchor="w", pady=(2, 8))
        self.chips_lbl = ttk.Label(info, text="", justify="left")
        self.chips_lbl.pack(anchor="w")
        brow = ttk.Frame(self.book_card)
        brow.pack(fill="x", pady=(12, 0))
        self.browse_btn = ttk.Button(brow, text="Choose books...", command=self.browse)
        self.browse_btn.pack(side="left", fill="x", expand=True)
        self.edit_btn = ttk.Button(brow, text="Edit details...", command=self.edit_details, state="disabled")
        self.edit_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
        prow = ttk.Frame(self.book_card)
        prow.pack(fill="x", pady=(8, 0))
        self.paste_btn = ttk.Button(prow, text="Paste text...", command=self.paste_text)
        self.paste_btn.pack(side="left", fill="x", expand=True)
        self.sample_btn = ttk.Button(prow, text="\u25B6 Hear a sample", command=self.book_sample, state="disabled")
        self.sample_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.set_cover(None)

        self.settings_card = ttk.LabelFrame(self.left, text="Narration", padding=12)
        ttk.Label(self.settings_card, text="Voice").pack(anchor="w")
        vrow = ttk.Frame(self.settings_card)
        vrow.pack(fill="x", pady=(2, 4))
        self.voice_var = tk.StringVar(value=VOICES.get(self.settings.get("voice", "af_heart"), VOICES["af_heart"]))
        self.voice_box = ttk.Combobox(vrow, textvariable=self.voice_var, values=list(VOICES.values()), state="readonly")
        self.voice_box.pack(side="left", fill="x", expand=True)
        self.voice_box.bind("<<ComboboxSelected>>", lambda e: self.persist())
        self.preview_btn = ttk.Button(vrow, text="▶ Hear it", command=self.preview_voice)
        self.preview_btn.pack(side="left", padx=(8, 0))
        self.preview_lbl = ttk.Label(self.settings_card, text="", foreground="gray")
        self.preview_lbl.pack(anchor="w", pady=(0, 6))
        ttk.Label(self.settings_card, text="Voice for quoted speech").pack(anchor="w")
        current = self.settings.get("dialogue_voice") or ""
        self.dialogue_var = tk.StringVar(value=AUTO_VOICES if current == "auto" else CAST_VOICES if current == "cast" else
                                         (VOICES.get(current, SAME_VOICE) if current else SAME_VOICE))
        drow = ttk.Frame(self.settings_card)
        drow.pack(fill="x", pady=(2, 10))
        dbox = ttk.Combobox(drow, textvariable=self.dialogue_var,
                            values=[SAME_VOICE, AUTO_VOICES, CAST_VOICES] + list(VOICES.values()), state="readonly")
        self.dialogue_box = dbox
        dbox.pack(side="left", fill="x", expand=True)
        dbox.bind("<<ComboboxSelected>>", lambda e: (self.persist(), self.show_cast_button()))
        self.cast_btn = ttk.Button(drow, text="Cast...", command=self.cast_characters)
        self.show_cast_button()
        srow = ttk.Frame(self.settings_card)
        srow.pack(fill="x")
        ttk.Label(srow, text="Speed").pack(side="left")
        self.speed_val = ttk.Label(srow, text="1.00x")
        self.speed_val.pack(side="right")
        self.speed_var = tk.DoubleVar(value=float(self.settings.get("speed", 1.0)))
        ttk.Scale(self.settings_card, from_=0.7, to=1.4, variable=self.speed_var, command=self.on_speed).pack(fill="x", pady=(2, 10))
        ttk.Label(self.settings_card, text="Save audiobooks to").pack(anchor="w")
        orow = ttk.Frame(self.settings_card)
        orow.pack(fill="x", pady=(2, 0))
        self.out_var = tk.StringVar(value=self.settings.get("out_dir", str(APP_DIR / "output")))
        ttk.Entry(orow, textvariable=self.out_var, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(orow, text="Change...", command=self.choose_out).pack(side="left", padx=(8, 0))
        pfrow = ttk.Frame(self.settings_card)
        pfrow.pack(fill="x", pady=(10, 0))
        # the less used tools sit in one menu, so the card stays short and nothing is cut off on a scaled screen
        self.more_btn = ttk.Menubutton(pfrow, text="More \u25BE")
        more = tk.Menu(self.more_btn, tearoff=0)
        more.add_command(label="Pronunciation fixes...", command=self.open_pronunciations)
        more.add_command(label="Check names in this book...", command=self.check_names)
        more.add_separator()
        more.add_command(label="Cast characters...", command=self.cast_characters)
        more.add_command(label="Mix a voice...", command=self.open_mixer)
        self.more_btn["menu"] = more
        self.more_btn.pack(side="left")
        ttk.Label(pfrow, text="Pronunciation fixes:", foreground="gray").pack(side="left", padx=(10, 0))
        self.pron_lbl = ttk.Label(pfrow, text="", foreground="gray")
        self.pron_lbl.pack(side="left", padx=(4, 0))
        self.update_pron_label()
        self.on_speed()

        self.run_card = ttk.LabelFrame(self.left, text="This run", padding=12)
        self.run_labels = {}
        for key, text in (("chapter", "Chapter"), ("elapsed", "Time so far"), ("left", "About left"),
                          ("speed", "Speed"), ("voice", "Voice"), ("took", "Took")):
            r = ttk.Frame(self.run_card)
            ttk.Label(r, text=text, foreground="gray").pack(side="left")
            lbl = ttk.Label(r, text="")
            lbl.pack(side="right")
            self.run_labels[key] = (r, lbl)

        self.hint = ttk.Label(self.left, text=f"Tip: drop a book ({FORMATS_TEXT}) to pick its chapters, "
                                              "or several books or a whole folder to queue them. Ctrl+V pastes text.",
                              wraplength=int(310 * self.scale), foreground="gray")
        self.hint.pack(side="bottom", anchor="w")
        self.watch_lbl = ttk.Label(self.left, text="", wraplength=int(310 * self.scale))
        self.watch_lbl.pack(side="bottom", anchor="w", pady=(0, 8))

        # --- right: views
        self.views = {}
        self.build_ready_view()
        self.build_batch_view()
        self.build_run_view()
        self.build_done_view()

        # --- library tab
        self.build_library()

        # --- drag-over overlay (shown while a file is dragged over the window)
        self.overlay = tk.Frame(self.root, bd=0)
        inner = tk.Frame(self.overlay, bd=0)
        inner.place(relx=0.5, rely=0.5, anchor="center")
        self.overlay_title = tk.Label(inner, text="Drop to load", font=self.f_drop)
        self.overlay_title.pack()
        self.overlay_sub = tk.Label(inner, text="One book opens it. Several books make a queue.")
        self.overlay_sub.pack(pady=(8, 0))
        self.overlay_inner = inner

    def show_cast_button(self):
        """Cast... next to the quoted-speech choice only when each character gets their own voice."""
        if self.dialogue_var.get() == CAST_VOICES:
            self.cast_btn.pack(side="left", padx=(8, 0))
        else:
            self.cast_btn.pack_forget()

    def build_ready_view(self):
        v = ttk.Frame(self.right)
        self.views["ready"] = v
        card = ttk.LabelFrame(v, text="Chapters", padding=10)
        card.pack(fill="both", expand=True)
        bar = ttk.Frame(card)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Search").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.apply_filter())
        ttk.Entry(bar, textvariable=self.search_var, width=24).pack(side="left", padx=(6, 12))
        self.count_lbl = ttk.Label(bar, text="")
        self.count_lbl.pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.set_all(False)).pack(side="right")
        ttk.Button(bar, text="Select all", command=lambda: self.set_all(True)).pack(side="right", padx=6)
        wrap = ttk.Frame(card)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("sel", "n", "title", "words", "len"), show="headings", selectmode="none")
        for col, text, width, anchor, stretch in (("sel", "", 40, "center", False), ("n", "#", 44, "e", False),
                                                  ("title", "  Chapter", 220, "w", True),
                                                  ("words", "Words", 80, "e", False), ("len", "Length", 90, "e", False)):
            self.tree.heading(col, text=text, anchor="w" if col == "title" else anchor)
            self.tree.column(col, width=int(width * self.scale), anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.on_tree_double)
        ttk.Label(card, text="Click a box to include or skip a chapter. Double-click a chapter to read it.",
                  foreground="gray").pack(anchor="w", pady=(6, 0))
        self.estimate_lbl = ttk.Label(v, text="", foreground="gray")
        self.estimate_lbl.pack(anchor="e", pady=(10, 0))
        foot = ttk.Frame(v)
        foot.pack(fill="x", pady=(6, 0))
        self.start_btn = ttk.Button(foot, text="Convert to audiobook  \u2192", style="Accent.TButton",
                                    command=self.start, state="disabled")
        self.start_btn.pack(side="right")
        self.make_after_box(foot)

    def build_batch_view(self):
        v = ttk.Frame(self.right)
        self.views["batch"] = v
        card = ttk.LabelFrame(v, text="Book queue", padding=10)
        card.pack(fill="both", expand=True)
        ttk.Label(card, text="These books will be converted one after another, each with all of its chapters.",
                  foreground="gray").pack(anchor="w", pady=(0, 8))
        wrap = ttk.Frame(card)
        wrap.pack(fill="both", expand=True)
        self.batch_tree = ttk.Treeview(wrap, columns=("n", "title", "author", "chapters", "len"), show="headings", selectmode="none")
        for col, text, width, anchor, stretch in (("n", "#", 40, "e", False), ("title", "  Book", 200, "w", True),
                                                  ("author", "Author", 140, "w", True), ("chapters", "Chapters", 80, "e", False),
                                                  ("len", "Length", 90, "e", False)):
            self.batch_tree.heading(col, text=text, anchor="w" if anchor == "w" else "e")
            self.batch_tree.column(col, width=int(width * self.scale), anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.batch_tree.yview)
        self.batch_tree.configure(yscrollcommand=sb.set)
        self.batch_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.batch_est = ttk.Label(v, text="", foreground="gray")
        self.batch_est.pack(anchor="e", pady=(10, 0))
        foot = ttk.Frame(v)
        foot.pack(fill="x", pady=(6, 0))
        self.batch_btn = ttk.Button(foot, text="Convert books  \u2192", style="Accent.TButton", command=self.start_batch)
        self.batch_btn.pack(side="right")
        self.make_after_box(foot)

    def build_run_view(self):
        v = ttk.Frame(self.right)
        self.views["run"] = v
        top = ttk.LabelFrame(v, text="Converting your audiobook", padding=12)
        top.pack(fill="x")
        self.run_top = top
        prow = ttk.Frame(top)
        prow.pack(fill="x")
        self.pct_lbl = ttk.Label(prow, text="0%", font=self.f_big)
        self.pct_lbl.pack(side="right")
        self.run_status = ttk.Label(prow, text="Starting... the first chapter includes about a minute of GPU warm-up.")
        self.run_status.pack(side="left", anchor="s")
        self.bar = ttk.Progressbar(top, maximum=100)
        self.bar.pack(fill="x", pady=(8, 0))

        card = ttk.LabelFrame(v, text="Chapters", padding=10)
        card.pack(fill="both", expand=True, pady=(12, 0))
        wrap = ttk.Frame(card)
        wrap.pack(fill="both", expand=True)
        self.run_tree = ttk.Treeview(wrap, columns=("n", "title", "state"), show="headings", selectmode="none")
        for col, text, width, anchor, stretch in (("n", "#", 44, "e", False), ("title", "  Chapter", 220, "w", True),
                                                  ("state", "Status", 170, "w", False)):
            self.run_tree.heading(col, text=text, anchor="w" if col != "n" else "e")
            self.run_tree.column(col, width=int(width * self.scale), anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.run_tree.yview)
        self.run_tree.configure(yscrollcommand=sb.set)
        self.run_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        det = ttk.LabelFrame(v, text="Details", padding=6)
        det.pack(fill="x", pady=(12, 0))
        self.log = ScrolledText(det, height=4, state="disabled", font=self.f_mono, relief="flat", wrap="none", bd=0)
        self.log.pack(fill="x")
        foot = ttk.Frame(v)
        foot.pack(fill="x", pady=(12, 0))
        self.cancel_btn = ttk.Button(foot, text="Cancel", command=self.cancel)
        self.cancel_btn.pack(side="right")
        self.pause_btn = ttk.Button(foot, text="\u23F8  Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=(0, 8))
        self.make_after_box(foot, side="left")
        self.back_btn = ttk.Button(foot, text="Back", command=self.back_to_ready)
        self.diag_btn = ttk.Button(foot, text="Save diagnostics...", command=lambda: self.save_diagnostics())
        self.check_btn = ttk.Button(foot, text="Check my setup...", command=lambda: self.check_setup())

    def build_done_view(self):
        v = ttk.Frame(self.right)
        self.views["done"] = v
        card = ttk.LabelFrame(v, text="Finished", padding=18)
        card.pack(fill="both", expand=True)
        head = ttk.Frame(card)
        head.pack(fill="x")
        self.done_icon = tk.Label(head, text="✔", font=self.f_symbol, width=2)
        self.done_icon.pack(side="left")
        text = ttk.Frame(head)
        text.pack(side="left", padx=(8, 0))
        self.done_title = ttk.Label(text, text="Your audiobook is ready", font=self.f_done)
        self.done_title.pack(anchor="w")
        self.done_sub = ttk.Label(text, text="")
        self.done_sub.pack(anchor="w")
        filebox = ttk.LabelFrame(card, text="File", padding=10)
        filebox.pack(fill="x", pady=(16, 0))
        self.done_name = ttk.Label(filebox, text="", font=self.f_strong)
        self.done_name.pack(anchor="w")
        self.done_path = ttk.Label(filebox, text="", foreground="gray")
        self.done_path.pack(anchor="w")
        btns = ttk.Frame(card)
        btns.pack(fill="x", pady=(16, 0))
        self.play_btn = ttk.Button(btns, text="▶  Play audiobook", style="Accent.TButton", command=self.play_output)
        self.play_btn.pack(side="left")
        ttk.Button(btns, text="Show in folder", command=self.show_output).pack(side="left", padx=8)
        ttk.Button(btns, text="See it in the Library", command=lambda: self.nb.select(self.library_tab)).pack(side="left")
        ttk.Button(btns, text="Convert another book", command=self.convert_another).pack(side="left", padx=8)
        self.markers_lbl = ttk.Label(card, text="Chapters inside the file (your player shows these)", font=self.f_strong)
        self.markers_lbl.pack(anchor="w", pady=(20, 4))
        self.marker_tree = ttk.Treeview(card, columns=("at", "title"), show="headings", selectmode="none", height=6)
        self.marker_tree.heading("at", text="Starts at", anchor="w")
        self.marker_tree.heading("title", text="Chapter", anchor="w")
        self.marker_tree.column("at", width=int(100 * self.scale), stretch=False)
        self.marker_tree.column("title", width=int(260 * self.scale), stretch=True)
        self.marker_tree.pack(fill="x")
        self.more_lbl = ttk.Label(card, text="", foreground="gray", wraplength=int(560 * self.scale))
        self.more_lbl.pack(anchor="w", pady=(4, 0))
        self.done_extra = ttk.Frame(card)
        self.done_extra.pack(fill="x", pady=(8, 0))
        self.retry_btn = ttk.Button(self.done_extra, text="Try the failed books again", command=self.retry_failed)
        self.report_btn = ttk.Button(self.done_extra, text="Listening report", command=self.open_report)

    def show_view(self, name):
        for frame in self.views.values():
            frame.pack_forget()
        self.views[name].pack(fill="both", expand=True)
        self.settings_card.pack_forget()
        self.run_card.pack_forget()
        if name in ("ready", "batch"):
            self.settings_card.pack(fill="x", pady=(14, 0), after=self.book_card)
        else:
            self.run_card.pack(fill="x", pady=(14, 0), after=self.book_card)
            only_in = {"took": "done", "elapsed": "run", "left": "run"}
            for key, (row, lbl) in self.run_labels.items():
                row.pack_forget()
                if only_in.get(key, name) == name:
                    row.pack(fill="x", pady=2)
        self.edit_btn.config(state="normal" if name == "ready" and self.book and not self.running else "disabled")
        if hasattr(self, "sample_btn"):
            self.sample_btn.config(state="normal" if name == "ready" and self.book and not self.running
                                   and not self.sample_busy else "disabled")
        self.view = name

    # ------------------------------------------------------------------ settings
    def persist(self):
        dialogue = ""
        if hasattr(self, "dialogue_var") and self.dialogue_var.get() == AUTO_VOICES:
            dialogue = "auto"
        elif hasattr(self, "dialogue_var") and self.dialogue_var.get() == CAST_VOICES:
            dialogue = "cast"
        elif hasattr(self, "dialogue_var") and self.dialogue_var.get() != SAME_VOICE:
            dialogue = next((vid for vid, label in VOICES.items() if label == self.dialogue_var.get()), "")
        self.settings.update(voice=self.voice_id(), speed=round(self.speed_var.get(), 2), out_dir=self.out_var.get(),
                             theme=self.theme, dialogue_voice=dialogue)
        save_settings(self.settings)

    def voice_id(self):
        for vid, label in VOICES.items():
            if label == self.voice_var.get():
                return vid
        return "af_heart"

    def on_speed(self, *_):
        self.speed_val.config(text=f"{self.speed_var.get():.2f}x")
        if hasattr(self, "out_var"):
            self.persist()

    def choose_out(self):
        path = filedialog.askdirectory(title="Where should finished audiobooks go?", initialdir=self.out_var.get())
        if path:
            self.out_var.set(str(Path(path)))
            self.persist()
            self.library_dirty = True

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        try:
            win.iconbitmap(str(APP_DIR / "forge.ico"))
        except Exception:  # noqa: BLE001
            pass
        outer = ttk.Frame(win, padding=16)
        outer.pack(fill="both", expand=True)
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        body = ttk.Frame(tabs, padding=14)
        narration_tab = ttk.Frame(tabs, padding=14)
        sound_tab = ttk.Frame(tabs, padding=14)
        speed_tab = ttk.Frame(tabs, padding=14)
        output_tab = ttk.Frame(tabs, padding=14)
        watch_tab = ttk.Frame(tabs, padding=14)
        tabs.add(body, text="  Appearance  ")
        tabs.add(narration_tab, text="  Narration  ")
        tabs.add(sound_tab, text="  Sound  ")
        tabs.add(speed_tab, text="  Speed  ")
        tabs.add(output_tab, text="  Output  ")
        tabs.add(watch_tab, text="  Watch folder  ")

        box = ttk.LabelFrame(body, text="Theme", padding=12)
        box.pack(fill="x")
        theme_var = tk.StringVar(value=self.theme)
        for value, label in (("light", "Light"), ("dark", "Dark")):
            ttk.Radiobutton(box, text=label, value=value, variable=theme_var,
                            command=lambda: self.set_theme(theme_var.get())).pack(side="left", padx=(0, 16))

        box = ttk.LabelFrame(body, text="Accent color", padding=12)
        box.pack(fill="x", pady=(12, 0))
        accent_var = tk.StringVar(value=self.settings.get("accent", self.accent))
        grid = ttk.Frame(box)
        grid.pack(fill="x")
        sw = int(16 * self.scale)
        for i, (key, label) in enumerate(ACCENT_LABELS.items()):
            cell = ttk.Frame(grid)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=3)
            ttk.Radiobutton(cell, text="", value=key, variable=accent_var,
                            command=lambda: self.choose_accent(accent_var.get(), restart_row)).pack(side="left")
            c = tk.Canvas(cell, width=sw, height=sw, highlightthickness=0, bd=0, background=self.theme_bg())
            c.create_rectangle(0, 0, sw, sw, fill=themes.accent_color(key, self.theme), outline="")
            c.pack(side="left", padx=(0, 6))
            ttk.Label(cell, text=label).pack(side="left")
        restart_row = ttk.Frame(box)
        restart_row.pack(fill="x", pady=(10, 0))
        self.restart_lbl = ttk.Label(restart_row, text="", foreground="gray")
        self.restart_lbl.pack(side="left")
        self.restart_btn = ttk.Button(restart_row, text="Restart now", command=self.restart_app)
        if accent_var.get() != self.accent:
            self.choose_accent(accent_var.get(), restart_row)

        box = ttk.LabelFrame(body, text="Text size", padding=12)
        box.pack(fill="x", pady=(12, 0))
        size_var = tk.StringVar(value=self.settings.get("text_size", "normal"))
        for key, (label, _) in TEXT_SIZES.items():
            ttk.Radiobutton(box, text=label, value=key, variable=size_var,
                            command=lambda: self.set_text_size(size_var.get())).pack(side="left", padx=(0, 16))

        box = ttk.LabelFrame(body, text="While converting", padding=12)
        box.pack(fill="x", pady=(12, 0))
        awake_var = tk.BooleanVar(value=self.settings.get("keep_awake", True))
        ttk.Checkbutton(box, text="Keep the PC awake (the screen can still turn off)", variable=awake_var,
                        style="Switch.TCheckbutton", command=lambda: self.set_setting("keep_awake", awake_var.get())).pack(anchor="w")
        ttk.Label(box, text="Stops Windows from going to sleep in the middle of a long book. Not while paused, and "
                            "never after the conversion ends.", foreground="gray",
                  wraplength=int(440 * self.scale)).pack(anchor="w", padx=(46, 0))

        box = ttk.LabelFrame(body, text="When a book is finished", padding=12)
        box.pack(fill="x", pady=(12, 0))
        sound_var = tk.BooleanVar(value=self.settings.get("sound", True))
        ttk.Checkbutton(box, text="Play a sound and flash the taskbar button", variable=sound_var, style="Switch.TCheckbutton",
                        command=lambda: self.set_setting("sound", sound_var.get())).pack(anchor="w")

        box = ttk.LabelFrame(body, text="Help", padding=12)
        box.pack(fill="x", pady=(12, 0))
        hrow = ttk.Frame(box)
        hrow.pack(anchor="w")
        ttk.Button(hrow, text="Check my setup...", command=lambda: self.check_setup(win)).pack(side="left")
        ttk.Button(hrow, text="Save diagnostics...", command=lambda: self.save_diagnostics(win)).pack(side="left", padx=(8, 0))
        ttk.Label(box, text="Check my setup tests the graphics card, ffmpeg, disk space and the program's parts, and says "
                            "how to fix anything wrong. Save diagnostics makes one zip with logs and system details to "
                            "send when something goes wrong; it contains no books or audio.", foreground="gray",
                  wraplength=int(440 * self.scale)).pack(anchor="w", pady=(6, 0))

        self.build_quality_settings(narration_tab, sound_tab, speed_tab)
        self.build_output_settings(output_tab)
        self.build_watch_settings(watch_tab, first=True)

        ttk.Button(outer, text="Close", command=win.destroy).pack(anchor="e", pady=(12, 0))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.grab_set()

    def set_setting(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)

    def choose_accent(self, key, row):
        self.set_setting("accent", key)
        if key == self.accent:
            self.restart_lbl.config(text="")
            self.restart_btn.pack_forget()
        else:
            self.restart_lbl.config(text="The new color appears after a restart.")
            if not self.running:
                self.restart_btn.pack(side="right")

    def set_text_size(self, key):
        self.set_setting("text_size", key)
        self.text_factor = TEXT_SIZES.get(key, TEXT_SIZES["normal"])[1]
        self.apply_style()
        if self.book and not self.batch:
            self.set_cover_from_book()

    def restart_app(self):
        if self.running:
            return
        self.persist()
        self.settings["geometry"] = self.root.geometry()
        save_settings(self.settings)
        args = [sys.executable, str(Path(__file__).resolve())]
        if self.book and not self.batch:
            args.append(str(self.book))
        try:
            subprocess.Popen(args, cwd=APP_DIR)
        except OSError:
            return
        self.root.destroy()

    # ---------------------------------------------------------- voice preview
    def preview_voice(self):
        if self.preview_busy or self.running:
            return
        voice = self.voice_id()
        wav = PREVIEW_DIR / (f"custom-{voicemix.safe(voice[7:])}-{voicemix.file_hash(voice)}.wav"
                             if voicemix.is_custom(voice) else f"{voice}.wav")
        if wav.exists():
            self.play_wav(wav)
            return
        self.preview_busy = True
        self.preview_btn.config(state="disabled")
        self.preview_lbl.config(text="Making a sample... the first one takes up to a minute.")

        def work():
            try:
                subprocess.run([sys.executable, str(APP_DIR / "preview.py"), voice, str(wav)], cwd=APP_DIR,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=600)
            except Exception:  # noqa: BLE001
                pass
            self.ui_q.put(lambda: self.preview_done(wav))
        threading.Thread(target=work, daemon=True).start()

    def preview_done(self, wav):
        self.preview_busy = False
        self.preview_btn.config(state="normal")
        if wav.exists():
            self.preview_lbl.config(text="")
            self.play_wav(wav)
        else:
            self.preview_lbl.config(text="Could not make a sample for that voice.")

    def play_wav(self, wav):
        try:
            import winsound
            winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:  # noqa: BLE001
            try:
                os.startfile(wav)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- drag and drop
    def on_drag_enter(self, event):
        if not self.running:
            self.overlay.place(relx=0.5, rely=0.5, relwidth=0.96, relheight=0.94, anchor="center")
            self.overlay.lift()
        return event.action

    def on_drag_leave(self, event):
        self.overlay.place_forget()
        return event.action

    def on_drop(self, event):
        self.overlay.place_forget()
        if self.running or self.loading_batch:
            return event.action
        self.open_paths(list(self.root.tk.splitlist(event.data)))
        return event.action

    def open_paths(self, paths):
        """Books, folders of books, or a mix: one book is loaded, several go into the queue."""
        files, info = expand_books(paths)
        if not files:
            hint = nothing_found_message(paths, info)
            messagebox.showinfo("Audiobook Forge", f"{hint}\n\nAudiobook Forge reads {FORMATS_TEXT} files.".strip())
            return
        notes = []
        if info["capped"]:
            notes.append(f"Only the first {FOLDER_LIMIT} books were taken. Drop a smaller folder for the rest.")
        if info["duplicates"]:
            notes.append(f"{info['duplicates']} book{'s were' if info['duplicates'] != 1 else ' was'} in the folder in more than "
                         "one format; the EPUB (or best other format) was taken.")
        if info["folders"] and info["unsupported"]:
            notes.append(f"{len(info['unsupported'])} Kindle or other file{'s' if len(info['unsupported']) != 1 else ''} "
                         "were left out (convert them to EPUB first).")
        self.nb.select(0)
        if len(files) > 1 or info["folders"]:
            self.set_batch(files, notes)
        else:
            self.load_book(files[0])

    def browse(self):
        if self.running or self.loading_batch:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Books...", command=self.browse_files)
        menu.add_command(label="A folder of books...", command=self.browse_folder)
        b = self.browse_btn
        try:
            menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            menu.grab_release()

    def browse_files(self):
        paths = filedialog.askopenfilenames(title="Choose one or more books",
                                            filetypes=[("Books", BOOK_PATTERNS), ("All files", "*.*")])
        if paths:
            self.open_paths(list(paths))

    def browse_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder of books (subfolders are included)", mustexist=True,
                                         initialdir=self.settings.get("last_book_folder") or None)
        if folder:
            self.set_setting("last_book_folder", folder)
            self.open_paths([folder])

    # ---------------------------------------------------------- loading a book
    def cover_size(self):
        return int(100 * self.scale * self.text_factor), int(134 * self.scale * self.text_factor)

    def set_cover(self, data):
        size = self.cover_size()
        try:
            if not data:
                raise ValueError
            import io
            img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), size)
        except Exception:  # noqa: BLE001
            img = Image.new("RGB", size, "#1B2A3A")
        self.cover_img = ImageTk.PhotoImage(img)
        self.cover_label.config(image=self.cover_img)

    def book_cover_bytes(self, path, meta, overrides=None):
        overrides = overrides or {}
        if overrides.get("cover"):
            try:
                return Path(overrides["cover"]).read_bytes()
            except OSError:
                pass
        cover = read_book_cover(path)
        if cover:
            return cover[0]
        try:
            from covers import make_cover
            return make_cover(overrides.get("title") or meta.get("title") or Path(path).stem,
                              overrides.get("author") or meta.get("creator", ""))
        except Exception:  # noqa: BLE001
            return None

    def show_book_card(self, path, meta, overrides=None):
        overrides = overrides or {}
        self.cover_bytes = self.book_cover_bytes(path, meta, overrides)
        self.set_cover(self.cover_bytes)
        self.title_lbl.config(text=overrides.get("title") or meta.get("title") or Path(path).stem)
        self.author_lbl.config(text=overrides.get("author") or meta.get("creator") or "Unknown author")

    def set_cover_from_book(self):
        if self.book:
            self.show_book_card(self.book, self.meta, self.overrides)

    def load_book(self, path):
        if self.running:
            return
        try:
            meta, chapters = read_book(path)
        except BookError as e:
            messagebox.showerror("Audiobook Forge", f"Could not read that book.\n\n{e}")
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Audiobook Forge", f"Could not read that book:\n{e}")
            return
        if not chapters:
            messagebox.showerror("Audiobook Forge", "No readable chapters were found in that book.")
            return
        self.batch = []
        self.overrides = {}
        self.book, self.chapters, self.meta = Path(path), chapters, meta
        self.selected = set(range(1, len(chapters) + 1))
        self.show_book_card(path, meta)
        self.tree.delete(*self.tree.get_children())
        for i, ch in enumerate(chapters, 1):
            self.tree.insert("", "end", iid=str(i), values=(CHECKED, i, ch.title, f"{ch.words:,}", fmt_length(ch.words / WORDS_PER_MIN)))
        self.search_var.set("")
        self.start_btn.config(state="normal")
        self.show_view("ready")
        self.update_summary()

    # ---------------------------------------------------------- editing details
    def edit_details(self):
        if not self.book or self.running or self.batch:
            return
        win = tk.Toplevel(self.root)
        win.title("Edit book details")
        win.transient(self.root)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=20)
        body.pack(fill="both", expand=True)
        cur = dict(self.overrides)
        ttk.Label(body, text="These change the audiobook's title, author and cover. The book file itself is not changed.",
                  foreground="gray", wraplength=int(440 * self.scale)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(body, text="Title").grid(row=1, column=0, sticky="w")
        title_var = tk.StringVar(value=cur.get("title") or self.meta.get("title") or self.book.stem)
        ttk.Entry(body, textvariable=title_var, width=44).grid(row=1, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Label(body, text="Author").grid(row=2, column=0, sticky="w", padx=(0, 12))
        author_var = tk.StringVar(value=cur.get("author") or self.meta.get("creator", ""))
        ttk.Entry(body, textvariable=author_var, width=44).grid(row=2, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Label(body, text="Cover").grid(row=3, column=0, sticky="nw", pady=(10, 0))
        preview = tk.Label(body, bd=0, background=self.theme_bg())
        preview.grid(row=3, column=1, sticky="w", pady=(10, 0))
        state = {"cover": cur.get("cover"), "img": None}

        def refresh_preview():
            data = self.book_cover_bytes(self.book, self.meta, {"cover": state["cover"], "title": title_var.get(),
                                                                "author": author_var.get()})
            size = self.cover_size()
            try:
                import io
                img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), size)
            except Exception:  # noqa: BLE001
                img = Image.new("RGB", size, "#1B2A3A")
            state["img"] = ImageTk.PhotoImage(img)
            preview.config(image=state["img"])

        def choose_image():
            path = filedialog.askopenfilename(parent=win, title="Choose a cover image",
                                              filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp *.gif")])
            if not path:
                return
            try:
                CUSTOM_COVERS.mkdir(parents=True, exist_ok=True)
                out = CUSTOM_COVERS / f"{safe_name(self.book.stem)}.jpg"
                img = Image.open(path).convert("RGB")
                img.thumbnail((1400, 1400))
                img.save(out, format="JPEG", quality=92)
                state["cover"] = str(out)
                refresh_preview()
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Audiobook Forge", f"That image could not be used:\n{e}", parent=win)

        def reset_cover():
            state["cover"] = None
            refresh_preview()

        cbtns = ttk.Frame(body)
        cbtns.grid(row=3, column=2, sticky="nw", pady=(10, 0), padx=(12, 0))
        ttk.Button(cbtns, text="Choose image...", command=choose_image).pack(fill="x")
        ttk.Button(cbtns, text="Use the book's cover", command=reset_cover).pack(fill="x", pady=(8, 0))
        refresh_preview()

        def save():
            new = {}
            if title_var.get().strip() and title_var.get().strip() != (self.meta.get("title") or ""):
                new["title"] = title_var.get().strip()
            if author_var.get().strip() != (self.meta.get("creator") or ""):
                new["author"] = author_var.get().strip()
            if state["cover"]:
                new["cover"] = state["cover"]
            self.overrides = new
            self.set_cover_from_book()
            win.destroy()

        foot = ttk.Frame(body)
        foot.grid(row=4, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(foot, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(foot, text="Save", style="Accent.TButton", command=save).pack(side="right", padx=8)
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.grab_set()

    # ------------------------------------------------------- chapter selection
    def apply_filter(self):
        query = self.search_var.get().strip().lower()
        pos = 0
        for i, ch in enumerate(self.chapters, 1):
            iid = str(i)
            if not query or query in ch.title.lower():
                self.tree.move(iid, "", pos)
                pos += 1
            else:
                self.tree.detach(iid)
        self.update_summary()

    def on_tree_click(self, event):
        if self.running or self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            idx = int(iid)
            self.selected.symmetric_difference_update({idx})
            self.tree.set(iid, "sel", CHECKED if idx in self.selected else UNCHECKED)
            self.update_summary()

    def on_tree_double(self, event):
        if self.tree.identify_column(event.x) == "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.show_chapter_text(int(iid))

    def show_chapter_text(self, idx):
        ch = self.chapters[idx - 1]
        win = tk.Toplevel(self.root)
        win.title(ch.title)
        win.transient(self.root)
        win.geometry(f"{int(720 * self.scale)}x{int(620 * self.scale)}")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body)
        head.pack(fill="x", pady=(0, 10))
        card_lbl = tk.Label(head, bd=0, background=self.theme_bg())
        card_lbl.pack(side="right", padx=(12, 0))
        self.chapter_card_into(card_lbl, idx, ch.title)
        titles = ttk.Frame(head)
        titles.pack(side="left", fill="x", expand=True)
        ttk.Label(titles, text=ch.title, font=self.f_book, wraplength=int(520 * self.scale)).pack(anchor="w")
        ttk.Label(titles, text=f"Chapter {idx} - {ch.words:,} words - about {fmt_length(ch.words / WORDS_PER_MIN)} of audio",
                  foreground="gray").pack(anchor="w", pady=(2, 0))
        dark = self.theme == "dark"
        txt = ScrolledText(body, wrap="word", relief="flat", bd=0, padx=12, pady=10,
                           font=tkfont.nametofont("SunValleyBodyFont", root=self.root),
                           background="#202020" if dark else "#ffffff", foreground="#ECEFF3" if dark else "#1c1c1c")
        txt.pack(fill="both", expand=True)
        txt.tag_configure("hl", background="#3a3a1e" if dark else "#fff3b0")
        spoken_var = tk.BooleanVar(value=False)
        shown = []                          # paragraph numbers in the order they are shown
        clicked = {"index": None}

        def fill():
            txt.config(state="normal")
            txt.delete("1.0", "end")
            shown.clear()
            texts = []
            for i, p in enumerate(ch.paragraphs):
                if spoken_var.get():
                    if self.narration_options()["clutter"] and normalize.is_caption(p):
                        continue
                    texts.append(self.as_spoken(p))
                else:
                    texts.append(p)
                shown.append(i)
            txt.insert("1.0", "\n\n".join(texts))
            txt.config(state="disabled")
        fill()

        def paragraph_at(index):
            line = int(str(index).split(".")[0])
            k = max(0, min((line - 1) // 2, len(shown) - 1))
            return k

        def remember(event):
            clicked["index"] = txt.index(f"@{event.x},{event.y}")

        def hear():
            if not shown:
                return
            k = paragraph_at(clicked["index"] or txt.index("@0,0"))
            txt.tag_remove("hl", "1.0", "end")
            txt.tag_add("hl", f"{2 * k + 1}.0", f"{2 * k + 1}.end")
            hear_btn.config(state="disabled")

            def status(text):
                if win.winfo_exists():
                    hear_lbl.config(text=text)

            def done():
                if win.winfo_exists():
                    hear_btn.config(state="normal")
            if self.running or self.sample_busy:
                status("Wait for the current conversion or sample to finish first.")
                done()
                return
            self.book_sample(at=f"{idx}:{shown[k] + 1}", status=status, done_cb=done)

        menu = tk.Menu(win, tearoff=0)
        menu.add_command(label="Hear this paragraph", command=hear)

        def context(event):
            remember(event)
            menu.tk_popup(event.x_root, event.y_root)
        txt.bind("<Button-1>", remember, add="+")
        txt.bind("<Button-3>", context)
        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(foot, text="Show it as it will be read (after fixes and clean-up)", variable=spoken_var,
                        style="Switch.TCheckbutton", command=fill).pack(side="left")
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")
        hear_btn = ttk.Button(foot, text="\u25B6 Hear paragraph", command=hear)
        hear_btn.pack(side="right", padx=(0, 8))
        hear_lbl = ttk.Label(body, text="Click a paragraph, then Hear paragraph (or right-click it). It is read with "
                                        "all your current settings.", foreground="gray", wraplength=int(660 * self.scale))
        hear_lbl.pack(anchor="w", pady=(6, 0))

    def chapter_card_into(self, label, idx, title):
        """Show this chapter's title card (the chapter picture) in a label, made in the background."""
        book = self.overrides.get("title") or self.meta.get("title") or ""
        cover = self.cover_bytes if hasattr(self, "cover_bytes") else None
        total = len(self.chapters)
        size = int(110 * self.scale)

        def work():
            try:
                import io
                from covers import make_chapter_card
                data = make_chapter_card(idx, title, book, cover, size=500, total=total)
                img = Image.open(io.BytesIO(data)).convert("RGB").resize((size, size), Image.LANCZOS)
            except Exception:  # noqa: BLE001
                img = None
            self.ui_q.put(lambda: show(img))

        def show(img):
            if img is None or not label.winfo_exists():
                return
            photo = ImageTk.PhotoImage(img)
            label.configure(image=photo)
            label.image = photo
        threading.Thread(target=work, daemon=True).start()

    def narration_options(self):
        n = self.settings.get("narration") or {}
        return {k: n.get(k, True) for k in ("clutter", "normalize", "heteronyms", "shouting")}

    def as_spoken(self, text, rules=None, markup=False):
        """What the narrator will actually say for this text."""
        rules = pron.load_rules() if rules is None else rules
        out = normalize.prepare(text, self.narration_options(), lambda t: pron.apply_rules(t, rules))
        return out if markup else normalize.strip_markup(out)

    def visible_rows(self):
        return [int(i) for i in self.tree.get_children()]

    def set_all(self, on):
        if self.running or not self.chapters:
            return
        for i in self.visible_rows():
            (self.selected.add if on else self.selected.discard)(i)
            self.tree.set(str(i), "sel", CHECKED if on else UNCHECKED)
        self.update_summary()

    def update_summary(self):
        if not self.chapters:
            return
        words = sum(self.chapters[i - 1].words for i in self.selected)
        audio_min = words / WORDS_PER_MIN
        self.chips_lbl.config(text=f"{len(self.chapters)} chapters\nabout {audio_min / 60:.1f} h of audio selected")
        shown = len(self.tree.get_children())
        extra = f"  (showing {shown})" if shown != len(self.chapters) else ""
        self.count_lbl.config(text=f"{len(self.selected)} of {len(self.chapters)} selected{extra}")
        kbps = QUALITIES.get((self.settings.get("output") or {}).get("quality", "standard"), (64, ""))[0]
        size_mb = audio_min * 60 * kbps * 1000 / 8 / 1e6
        self.estimate_lbl.config(text=f"About {fmt_length(audio_min / self.speed_x())} to convert, about {size_mb:.0f} MB when finished")
        self.start_btn.config(state="normal" if self.selected and not self.running else "disabled")

    # ------------------------------------------------------------ queue
    def set_batch(self, files, notes=()):
        """Read the books in the background (a folder can hold dozens), then show the queue."""
        if self.running or self.loading_batch:
            return
        self.loading_batch = True
        self.browse_btn.config(state="disabled")
        self.title_lbl.config(text="Reading books...")
        self.author_lbl.config(text=f"0 of {len(files)}")
        self.chips_lbl.config(text="")

        def work():
            books, skipped = [], []
            for n, f in enumerate(files, 1):
                try:
                    meta, chapters = read_book(f)
                    if chapters:
                        books.append((f, meta, chapters))
                    else:
                        skipped.append(f"{Path(f).name}: no readable text")
                except BookError as e:
                    skipped.append(f"{Path(f).name}: {str(e).splitlines()[0]}")
                except Exception:  # noqa: BLE001
                    skipped.append(Path(f).name)
                self.ui_q.put(lambda n=n, name=Path(f).name: self.author_lbl.config(text=f"{n} of {len(files)}: {name}"))
            self.ui_q.put(lambda: self.batch_read(books, skipped, list(notes)))
        threading.Thread(target=work, daemon=True).start()

    def batch_read(self, books, skipped, notes):
        self.loading_batch = False
        self.browse_btn.config(state="normal")
        if skipped:
            shown = skipped[:25] + ([f"...and {len(skipped) - 25} more"] if len(skipped) > 25 else [])
            notes.append("These could not be read and were skipped:\n\n" + "\n".join(shown))
        if notes:
            messagebox.showwarning("Audiobook Forge", "\n\n".join(notes))
        if not books:
            if self.book:
                self.show_book_card(self.book, self.meta, self.overrides)
            else:
                self.title_lbl.config(text="No book yet")
                self.author_lbl.config(text="Drop a book in this window")
            return
        if len(books) == 1:
            self.load_book(books[0][0])
            return
        if not getattr(self, "resuming", False):
            done_before = [b for b in books if checks.already_converted(b[0])]
            if done_before and len(done_before) < len(books):
                books = [b for b in books if b not in done_before]
                messagebox.showinfo("Audiobook Forge", f"{len(done_before)} of these books were converted before and are "
                                    "left out of the queue. To convert one again, drop it on its own.")
            elif done_before and messagebox.askyesno(
                    "Audiobook Forge", f"All {len(books)} books were converted before. Convert them all again?"):
                pass
            elif done_before:
                return
        self.resuming = False
        self.batch = books
        self.overrides = {}
        self.book, self.meta, self.chapters = Path(books[0][0]), books[0][1], books[0][2]
        self.show_book_card(books[0][0], books[0][1])
        self.batch_tree.delete(*self.batch_tree.get_children())
        for i, (f, meta, chapters) in enumerate(books, 1):
            words = sum(c.words for c in chapters)
            self.batch_tree.insert("", "end", iid=str(i), values=(i, meta.get("title") or Path(f).stem, meta.get("creator", ""),
                                                                  len(chapters), fmt_length(words / WORDS_PER_MIN)))
        self.show_view("batch")
        self.update_batch_summary()

    def update_batch_summary(self):
        n = len(self.batch)
        words = sum(c.words for _, _, chs in self.batch for c in chs)
        audio_min = words / WORDS_PER_MIN
        self.chips_lbl.config(text=f"{n} books in the queue\nabout {audio_min / 60:.1f} h of audio in total")
        self.batch_est.config(text=f"About {fmt_length(audio_min / self.speed_x())} to convert everything")
        self.batch_btn.config(text=f"Convert {n} books  →", state="normal")

    # ------------------------------------------------------------ running
    def reset_run_state(self):
        self.done = set()
        self.durations = {}
        self.order = []
        self.done_words = 0
        self.current_idx = None
        self.partial = {}           # chapter -> fraction done (two at once: more than one)
        self.total_words = 1
        self.total_audio_sec = None
        self.last_output = None
        self.problem = None
        self.report_path = None
        self.last_error = None
        self.log_path = None
        self.assembling = None       # (step, seconds done, seconds total) while the audiobook file is made
        self.err_tail = []           # the converter's last error-output lines (shown when it crashes)
        self.last_line_time = time.time()
        self.stalled = False
        self.start_time = 0.0
        self.end_time = 0.0
        self.paused_total = 0.0
        self.pause_started = time.time() if getattr(self, "paused", False) else 0.0

    def start(self):
        self.batch_active = False
        self.launch()

    def start_batch(self):
        self.batch_active = True
        self.batch_i = 0
        self.batch_results = []
        self.batch_start = time.time()
        self.batch_audio = 0.0
        self.save_queue()
        self.prepare_batch_book()
        self.launch()

    def save_queue(self):
        """Write the queue to queue.json after every book, so a crash, a restart or closing the window loses nothing:
        the next start offers to carry on."""
        data = {"books": [str(b[0]) for b in self.batch], "next": self.batch_i,
                "failed": [str(self.batch[r[3]][0]) for r in self.batch_results if not r[1] and r[3] is not None
                           and r[3] < len(self.batch)],
                "saved": time.strftime("%Y-%m-%d %H:%M")}
        try:
            tmp = QUEUE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, QUEUE_FILE)
        except OSError:
            pass

    @staticmethod
    def clear_queue():
        try:
            QUEUE_FILE.unlink()
        except OSError:
            pass

    @staticmethod
    def unfinished_queue():
        """(books still to convert, saved time) from a queue that did not finish, or None."""
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        books = data.get("books") or []
        left = books[int(data.get("next", 0)):]
        left += [b for b in data.get("failed") or [] if b not in left]
        left = [b for b in left if Path(b).exists()]
        return (left, data.get("saved", "")) if left else None

    def offer_resume(self):
        found = self.unfinished_queue()
        if not found or self.running or self.batch:
            return
        left, saved = found
        if messagebox.askyesno("Audiobook Forge", f"The queue from {saved} did not finish: {len(left)} "
                               f"book{'s' if len(left) != 1 else ''} still to convert. Put {'them' if len(left) != 1 else 'it'} "
                               "back in the queue? (Finished chapters are kept, so each carries on where it stopped.)"):
            self.resuming = True
            self.open_paths(left)
        else:
            self.clear_queue()

    def prepare_batch_book(self):
        path, meta, chapters = self.batch[self.batch_i]
        self.book, self.meta, self.chapters = Path(path), meta, chapters
        self.selected = set(range(1, len(chapters) + 1))
        self.overrides = {}
        self.show_book_card(path, meta)

    def launch(self):
        if self.ai_busy:
            messagebox.showinfo("Audiobook Forge", "The AI is still reading a book on the graphics card. Wait for it "
                                                   "to finish, or stop it, then convert.")
            self.batch_active = False
            return
        cmd = [sys.executable, "-u", str(APP_DIR / "convert.py"), str(self.book), "--voice", self.voice_id(),
               "--speed", f"{self.speed_var.get():.2f}", "--out", self.out_var.get()]
        if len(self.selected) != len(self.chapters):
            cmd += ["--chapters", compress_ranges(self.selected)]
        if not self.batch_active:
            for key in ("title", "author", "cover"):
                if self.overrides.get(key):
                    cmd += [f"--{key}", self.overrides[key]]
        cmd += ["--pause-file", str(PAUSE_FILE)]
        if self.settings.get("keep_work"):
            cmd += ["--keep-work"]
        if not self.batch_active:
            words = sum(self.chapters[i - 1].words for i in self.selected)
            kbps = QUALITIES.get((self.settings.get("output") or {}).get("quality", "standard"), (64, ""))[0]
            problem = checks.disk_problem(APP_DIR / "work", self.out_var.get(), words, words, kbps)
            if problem:
                if not messagebox.askyesno("Audiobook Forge", problem + "\n\nConvert anyway?", icon="warning"):
                    return
                cmd += ["--skip-checks"]
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            GUI_BUSY.write_text(str(os.getpid()))        # the watch folder waits while the window converts
        except OSError:
            pass
        try:
            self.proc = subprocess.Popen(cmd, cwd=APP_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            if self.batch_active:          # one book that cannot start must not stop the queue
                title = self.meta.get("title") or self.book.stem
                self.batch_results.append((title, None, f"the converter could not start ({e})", self.batch_i))
                self.next_in_batch()
                return
            messagebox.showerror("Audiobook Forge", f"Could not start the converter:\n{e}")
            return
        self.persist()
        self.reset_run_state()
        self.running, self.cancelled = True, False
        self.order = sorted(self.selected)
        self.total_words = max(1, sum(self.chapters[i - 1].words for i in self.order))
        self.start_time = time.time()
        self.run_tree.delete(*self.run_tree.get_children())
        for i in self.order:
            self.run_tree.insert("", "end", iid=str(i), values=(i, self.chapters[i - 1].title, "Queued"))
        if not self.batch_active or self.batch_i == 0:
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.config(state="disabled")
        self.bar["value"] = 0
        self.pct_lbl.config(text="0%")
        self.run_status.config(text="Starting... the first chapter includes about a minute of GPU warm-up.")
        self.cancel_btn.config(state="normal")
        self.pause_btn.config(state="normal", text="\u25B6  Resume" if self.paused else "\u23F8  Pause")
        self.run_labels["voice"][1].config(text=VOICES.get(self.voice_id(), self.voice_id()))
        self.back_btn.pack_forget()
        self.diag_btn.pack_forget()
        self.check_btn.pack_forget()
        title = self.overrides.get("title") or self.meta.get("title") or self.book.stem
        if self.batch_active:
            self.run_top.config(text=f"Converting book {self.batch_i + 1} of {len(self.batch)}: {title}")
        else:
            self.run_top.config(text=f"Converting: {title}")
        self.nb.select(0)
        self.show_view("run")
        self.taskbar.progress(0)
        self.refresh_run()
        threading.Thread(target=self.err_reader, args=(self.proc, self.err_tail), daemon=True).start()
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()

    def reader(self, proc):
        for line in proc.stdout:
            self.q.put(line.rstrip())
        proc.wait()
        self.q.put(None)

    @staticmethod
    def err_reader(proc, tail):
        """Keep the last lines the converter wrote to its error output (library warnings, or why it crashed)."""
        for line in proc.stderr:
            line = line.rstrip()
            if line and "Defaulting repo_id" not in line:
                tail.append(line)
                if len(tail) > 60:
                    del tail[:20]

    def check_stalled(self):
        """A converter silent for STALL_MINUTES while not paused has hung (a stuck graphics driver, a hidden
        dialog). Stop it so the queue moves on; finished chapters are kept for Retry."""
        if not (self.running and self.proc) or self.paused or self.cancelled or self.stalled:
            return
        if time.time() - self.last_line_time > STALL_MINUTES * 60:
            self.stalled = True
            self.problem = (f"No progress for {STALL_MINUTES} minutes, so the conversion was stopped (the graphics card "
                            "or ffmpeg stopped responding). Finished chapters are kept; Retry carries on from there.")
            self.log_line(self.problem)
            try:
                self.proc.kill()
            except OSError:
                pass

    def note_crash(self, code):
        """Put the reason and the converter's last error lines into its log file (Save diagnostics includes it)."""
        lines = [f"--- {describe_exit(code)} (exit code {(code or 0) & 0xFFFFFFFF:#x}) ---"]
        if self.problem:
            lines.append(self.problem)
        lines += self.err_tail[-30:]
        for line in lines[1:][-8:]:
            self.log_line(line)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%H:%M:%S ") + "\n".join(lines) + "\n")
            except OSError:
                pass

    def poll(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        try:
            while True:
                line = self.q.get_nowait()
                if line is None:
                    self.finished()
                else:
                    self.handle_line(line)
        except queue.Empty:
            pass
        if self.running:
            self.refresh_run()
        self.check_stalled()
        if time.time() - self.watch_checked > 2:
            self.watch_checked = time.time()
            self.update_watch_label()
        self.root.after(150, self.poll)

    def log_line(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def handle_line(self, line):
        self.last_line_time = time.time()
        if line.startswith("WARNING: Defaulting repo_id"):
            return
        if line.startswith("@log "):
            self.log_path = line[5:].strip()
            return
        m = ASM_RE.match(line)
        if m:
            self.assembling = (m.group(1), int(m.group(2)), max(1, int(m.group(3))))
            return
        m = PROG_RE.match(line)
        if m:
            idx, k, n = (int(x) for x in m.groups())
            if idx != self.current_idx:
                self.current_idx = idx
                if str(idx) in self.run_tree.get_children():
                    self.run_tree.see(str(idx))
            self.partial[idx] = k / max(1, n)
            return
        m = SUM_RE.match(line)
        if m:
            self.total_audio_sec = float(m.group(1))
            return
        if line.startswith("@paused"):
            return
        if line.startswith("@problem "):
            self.problem = line[len("@problem "):]
            return
        if line.startswith("@report "):
            self.report_path = line[len("@report "):].strip()
            return
        self.log_line(line)
        if line.strip() and not line.startswith(("  ", "Traceback", "[", "Done in", "Assembling", "Using ", "Device")):
            self.last_error = line.strip()[:200]
        m = DONE_RE.match(line)
        if m:
            idx = int(m.group(3))
            if idx not in self.done:
                self.done.add(idx)
                self.done_words += self.chapters[idx - 1].words
            self.partial.pop(idx, None)
            mm = MIN_RE.search(line)
            if mm:
                self.durations[idx] = float(mm.group(1) or mm.group(2))
        m = OUT_RE.search(line)
        if m:
            self.last_output = m.group(1)

    def refresh_run(self):
        cur_words = sum(self.chapters[i - 1].words * f for i, f in self.partial.items()
                        if i not in self.done and 0 < i <= len(self.chapters))
        got = min(self.total_words, self.done_words + cur_words)
        pct = got / self.total_words * 100
        self.bar["value"] = pct
        self.pct_lbl.config(text=f"{int(pct)}%")
        tb_state = WinTaskbar.PAUSED if self.paused else None
        if self.batch_active and self.batch:
            self.taskbar.progress((self.batch_i + pct / 100) / len(self.batch) * 100, tb_state)
        else:
            self.taskbar.progress(pct, tb_state)
        elapsed = self.active_seconds()
        for i in self.order:
            iid = str(i)
            if i in self.done:
                dur = self.durations.get(i)
                text = f"Done, {fmt_length(dur)}" if dur else "Done"
            elif i in self.partial:
                text = f"Converting... {int(self.partial[i] * 100)}%"
            else:
                text = "Queued"
            if self.run_tree.set(iid, "state") != text:
                self.run_tree.set(iid, "state", text)
        pos = (self.order.index(self.current_idx) + 1) if self.current_idx in self.order else len(self.done) + 1
        self.run_labels["chapter"][1].config(text=f"{min(pos, len(self.order))} of {len(self.order)}")
        self.run_labels["elapsed"][1].config(text=fmt_time(elapsed))
        remaining_words = self.total_words - got
        if got > self.total_words * 0.03 and elapsed > 5:
            rate = (got / WORDS_PER_MIN * 60) / elapsed          # seconds of audio per second
            left = remaining_words / WORDS_PER_MIN * 60 / max(rate, 0.1)
            self.run_labels["speed"][1].config(text=f"{rate:.1f}x realtime")
        else:
            left = remaining_words / WORDS_PER_MIN * 60 / self.speed_x()
            self.run_labels["speed"][1].config(text="measuring...")
        self.run_labels["left"][1].config(text=fmt_length(left / 60) if left >= 60 else "under a minute")
        if self.running and self.assembling and not self.paused:
            step, done_s, total_s = self.assembling
            frac = min(1.0, done_s / total_s)
            what = "Measuring the book's loudness" if step == "measuring" else "Putting the audiobook file together"
            self.run_status.config(text=f"{what}: {int(frac * 100)}%")
            self.run_labels["left"][1].config(text="a few minutes" if frac < 0.8 else "under a minute")
        elif self.paused:
            self.run_status.config(text="Paused. Click Resume to carry on where it stopped.")
        elif self.running and self.done:
            self.run_status.config(text=f"{len(self.done)} of {len(self.order)} chapters done, "
                                        f"about {fmt_length(left / 60) if left >= 60 else 'a minute'} left")

    def finished(self):
        code = self.proc.returncode if self.proc else None
        self.running = False
        self.end_time = time.time()
        self.cancel_btn.config(state="disabled")
        self.pause_btn.config(state="disabled")
        more_books = self.batch_active and not self.cancelled and self.batch_i + 1 < len(self.batch)
        if not more_books:
            self.set_paused(False)
            try:
                GUI_BUSY.unlink()
            except OSError:
                pass
        took = fmt_time(self.end_time - self.start_time)
        ok = code == 0 and self.last_output is not None and not self.cancelled
        if not ok and not self.cancelled:
            if code not in (0, 1) and not self.problem:
                self.problem = describe_exit(code)[0].upper() + describe_exit(code)[1:] + \
                    ". Finished chapters are kept; converting again carries on from there."
            self.note_crash(code)
        if ok:
            self.library_dirty = True
            self.remember_speed()
        if self.batch_active and not self.cancelled:
            title = self.meta.get("title") or self.book.stem
            reason = None if ok else (self.problem or self.last_error or describe_exit(code))
            self.batch_results.append((title, self.last_output if ok else None, reason, self.batch_i))
            if ok:
                self.batch_audio += self.total_audio_sec or sum(self.durations.values()) * 60
            if self.batch_i + 1 < len(self.batch):
                self.batch_i += 1
                self.save_queue()
                self.prepare_batch_book()
                self.launch()
                return
            self.clear_queue()
            self.show_batch_done()
            self.notify_done(any(r[1] for r in self.batch_results))
            return
        if not self.batch_active:
            self.update_summary()
        if self.cancelled:
            note = "Books finished so far are kept." if self.batch_active else "Finished chapters are kept - convert the same book again to resume."
            self.run_status.config(text=f"Cancelled. {note}")
            self.back_btn.pack(side="right", padx=8)
            self.taskbar.set_state(WinTaskbar.NOPROGRESS)
        elif ok:
            self.show_done(took)
            self.notify_done(True)
        else:
            self.run_status.config(text=self.problem or f"Stopped: {describe_exit(code)}. The details below may say why.")
            self.back_btn.pack(side="right", padx=8)
            self.diag_btn.pack(side="right")
            self.check_btn.pack(side="right", padx=(0, 8))
            self.notify_done(False)

    def notify_done(self, success):
        action = self.after_action()
        if success and action != "nothing":
            self.root.after(1500, lambda: self.begin_after_action(action))
        if success:
            self.taskbar.set_state(WinTaskbar.NOPROGRESS)
        else:
            self.taskbar.progress(100)
            self.taskbar.set_state(WinTaskbar.ERROR)
        if self.settings.get("sound", True):
            try:
                import winsound
                winsound.PlaySound("SystemAsterisk" if success else "SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
            except Exception:  # noqa: BLE001
                self.root.bell()
            if self.root.focus_displayof() is None:
                self.taskbar.flash()

    def cancel(self):
        if self.proc and self.running:
            self.cancelled = True
            self.set_paused(False)
            self.proc.terminate()

    # ------------------------------------------------------------- finished
    def show_done(self, took):
        out = Path(self.last_output)
        try:
            size_mb = (sum(f.stat().st_size for f in out.iterdir() if f.is_file()) if out.is_dir()
                       else out.stat().st_size) / 1e6
        except OSError:
            size_mb = 0
        total_sec = self.total_audio_sec or sum(self.durations.values()) * 60
        self.done_title.config(text="Your audiobook is ready")
        self.done_sub.config(text=f"{len(self.order)} chapters, {fmt_length(total_sec / 60)}, {size_mb:.0f} MB, converted in {took}")
        self.done_name.config(text=out.name)
        self.done_path.config(text=str(out.parent))
        self.retry_btn.pack_forget()
        self.report_btn.pack_forget()
        if self.report_path and Path(self.report_path).exists():
            try:
                places = len(json.loads(Path(self.report_path).read_text(encoding="utf-8")).get("events", []))
            except (OSError, ValueError):
                places = 0
            self.report_btn.config(text=f"Listening report ({places} place{'s' if places != 1 else ''} worth a listen)"
                                   if places else "Listening report (nothing in particular)")
            self.report_btn.pack(side="left")
        self.run_labels["took"][1].config(text=took)
        self.run_labels["chapter"][1].config(text=f"{len(self.order)} chapters")
        rate = total_sec / max(1, self.end_time - self.start_time)
        self.run_labels["speed"][1].config(text=f"{rate:.1f}x realtime")
        self.markers_lbl.config(text="Chapters inside the file (your player shows these)")
        self.marker_tree.heading("at", text="Starts at")
        self.marker_tree.heading("title", text="Chapter")
        self.marker_tree.delete(*self.marker_tree.get_children())
        start, shown = 0.0, 0
        for i in self.order:
            if shown < 6:
                self.marker_tree.insert("", "end", values=(fmt_time(start * 60), self.chapters[i - 1].title))
                shown += 1
            start += self.durations.get(i, self.chapters[i - 1].words / WORDS_PER_MIN)
        more = len(self.order) - shown
        self.more_lbl.config(text=f"and {more} more chapters" if more > 0 else "")
        self.show_view("done")

    def next_in_batch(self):
        """Go on with the next book of the queue, or show the summary after the last one."""
        if self.batch_i + 1 < len(self.batch):
            self.batch_i += 1
            self.save_queue()
            self.prepare_batch_book()
            self.root.after(200, self.launch)
        else:
            self.running = False
            self.clear_queue()
            self.show_batch_done()
            self.notify_done(any(r[1] for r in self.batch_results))

    def retry_failed(self):
        failed = [r[3] for r in self.batch_results if not r[1] and r[3] is not None and r[3] < len(self.batch)]
        if not failed or self.running:
            return
        self.batch = [self.batch[i] for i in failed]
        self.retry_btn.pack_forget()
        self.start_batch()

    def show_batch_done(self):
        ok = [(r[0], r[1]) for r in self.batch_results if r[1]]
        took = fmt_time(time.time() - self.batch_start)
        self.last_output = ok[-1][1] if ok else None
        self.done_title.config(text="Your audiobooks are ready" if ok else "Nothing was converted")
        self.done_sub.config(text=f"{len(ok)} of {len(self.batch_results)} books converted in {took}")
        self.done_name.config(text=f"{len(ok)} audiobooks saved")
        self.done_path.config(text=self.out_var.get())
        self.markers_lbl.config(text="Books in this run")
        self.report_btn.pack_forget()
        self.marker_tree.heading("at", text="Result")
        self.marker_tree.heading("title", text="Book")
        self.marker_tree.delete(*self.marker_tree.get_children())
        rows = sorted(self.batch_results, key=lambda r: bool(r[1]))          # failed books first
        for title, path, reason, _ in rows[:12]:
            self.marker_tree.insert("", "end", values=("Done" if path else "Failed", title if path else f"{title}: {reason}"))
        extra = len(rows) - 12
        failed = len(self.batch_results) - len(ok)
        self.more_lbl.config(text=(f"and {extra} more. " if extra > 0 else "") +
                             (f"{failed} failed. Their finished chapters are kept, so trying again picks up where they "
                              "stopped. The logs folder has the details." if failed else ""))
        self.retry_btn.pack_forget()
        if failed:
            self.retry_btn.pack(side="left")
        self.run_labels["took"][1].config(text=took)
        self.run_labels["chapter"][1].config(text=f"{len(self.batch_results)} books")
        spent = max(1, time.time() - self.batch_start)
        self.run_labels["speed"][1].config(text=f"{self.batch_audio / spent:.1f}x realtime" if self.batch_audio else "-")
        self.show_view("done")

    def back_to_ready(self):
        self.reset_run_state()
        self.taskbar.set_state(WinTaskbar.NOPROGRESS)
        if self.batch and self.batch_active:
            self.batch_active = False
            self.show_book_card(self.batch[0][0], self.batch[0][1])
            self.show_view("batch")
            self.update_batch_summary()
        else:
            self.batch_active = False
            self.show_view("ready")
            self.update_summary()

    def play_output(self):
        if self.last_output and Path(self.last_output).exists():
            os.startfile(self.last_output)

    def show_output(self):
        if self.last_output and Path(self.last_output).exists():
            subprocess.Popen(["explorer", "/select,", str(Path(self.last_output))])
        else:
            self.open_out_folder()

    def open_out_folder(self):
        out = Path(self.out_var.get())
        out.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(out)
        except Exception:  # noqa: BLE001
            pass

    def convert_another(self):
        self.batch, self.batch_active = [], False
        self.reset_run_state()
        self.show_view("ready")
        self.update_summary()

    # ------------------------------------------------------------- library
    def build_library(self):
        tab = self.library_tab
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 10))
        self.lib_count = ttk.Label(bar, text="", font=self.f_strong)
        self.lib_count.pack(side="left")
        ttk.Button(bar, text="Open folder", command=self.open_out_folder).pack(side="right")
        ttk.Button(bar, text="Refresh", command=self.refresh_library).pack(side="right", padx=8)
        self.lib_search = tk.StringVar()
        self.lib_search.trace_add("write", lambda *a: self.layout_library())
        ttk.Entry(bar, textvariable=self.lib_search, width=26).pack(side="right", padx=(6, 8))
        ttk.Label(bar, text="Search").pack(side="right")
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        self.lib_canvas = tk.Canvas(holder, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.lib_canvas.yview)
        self.lib_canvas.configure(yscrollcommand=sb.set)
        self.lib_canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.lib_inner = ttk.Frame(self.lib_canvas)
        self.lib_window = self.lib_canvas.create_window(0, 0, window=self.lib_inner, anchor="nw")
        self.lib_inner.bind("<Configure>", lambda e: self.lib_canvas.configure(scrollregion=self.lib_canvas.bbox("all")))
        self.lib_canvas.bind("<Configure>", self.on_lib_resize)
        self.lib_canvas.bind("<Enter>", lambda e: self.bind_wheel(True))
        self.lib_canvas.bind("<Leave>", lambda e: self.bind_wheel(False))
        self.lib_empty = ttk.Label(self.lib_inner, text="", foreground="gray", justify="left")
        self.lib_tiles = []
        self.lib_cols = 0

    def library_images_labels(self):
        return [t["cover"] for t in getattr(self, "lib_tiles", [])]

    def bind_wheel(self, on):
        if on:
            self.root.bind_all("<MouseWheel>", lambda e: self.lib_canvas.yview_scroll(int(-e.delta / 120), "units"))
            self.root.bind_all("<Button-4>", lambda e: self.lib_canvas.yview_scroll(-3, "units"))
            self.root.bind_all("<Button-5>", lambda e: self.lib_canvas.yview_scroll(3, "units"))
        else:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.root.unbind_all(seq)

    def on_tab_changed(self, event):
        if self.nb.index(self.nb.select()) == 1 and self.library_dirty:
            self.refresh_library()

    def refresh_library(self):
        self.library_dirty = False
        folder = Path(self.out_var.get())
        self.lib_count.config(text="Loading...")
        files = []
        if folder.exists():
            files = [f for f in folder.iterdir() if f.suffix.lower() in (".m4b", ".mp3") and f.is_file()] + \
                    [d for d in folder.iterdir() if d.is_dir() and any(d.glob("*.m3u")) and any(d.glob("*.mp3"))]
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        def work():
            reports = {}                       # finished file -> its listening report
            for r in (APP_DIR / "logs" / "reports").glob("*listening report.json") if (APP_DIR / "logs" / "reports").exists() else []:
                try:
                    out_path = json.loads(r.read_text(encoding="utf-8")).get("output", "")
                    if out_path:
                        reports[os.path.normcase(os.path.abspath(out_path))] = r
                except (OSError, ValueError):
                    pass
            items = []
            for f in files:
                title, author, length, cover = read_m4b_info(f)
                items.append({"path": f, "title": title, "author": author or "", "length": length, "cover": cover,
                              "report": reports.get(os.path.normcase(os.path.abspath(str(f))))})
            self.ui_q.put(lambda: self.library_loaded(items))
        threading.Thread(target=work, daemon=True).start()

    def library_loaded(self, items):
        for t in self.lib_tiles:
            t["frame"].destroy()
        self.lib_tiles = []
        self.library_images = []
        size = (int(120 * self.scale), int(160 * self.scale))
        for it in items:
            data = it["cover"]
            if not data:
                try:
                    from covers import make_cover
                    data = make_cover(it["title"], it["author"])
                except Exception:  # noqa: BLE001
                    data = None
            try:
                import io
                img = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), size)
            except Exception:  # noqa: BLE001
                img = Image.new("RGB", size, "#1B2A3A")
            photo = ImageTk.PhotoImage(img)
            self.library_images.append(photo)
            frame = ttk.Frame(self.lib_inner, padding=10)
            cover = tk.Label(frame, image=photo, bd=0, background=self.theme_bg())
            cover.pack(anchor="w")
            wrap = int(170 * self.scale)
            ttk.Label(frame, text=it["title"], font=self.f_strong, wraplength=wrap, justify="left").pack(anchor="w", pady=(8, 0))
            if it["author"]:
                ttk.Label(frame, text=it["author"], foreground="gray", wraplength=wrap, justify="left").pack(anchor="w")
            if it["length"]:
                ttk.Label(frame, text=fmt_length(it["length"] / 60), foreground="gray").pack(anchor="w")
            row = ttk.Frame(frame)
            row.pack(anchor="w", pady=(6, 0))
            path = it["path"]
            ttk.Button(row, text="▶ Play", style="Accent.TButton",
                       command=lambda p=path: os.startfile(p)).pack(side="left")
            ttk.Button(row, text="Folder", command=lambda p=path: subprocess.Popen(["explorer", "/select,", str(p)])).pack(side="left", padx=(6, 0))
            if it.get("report"):
                ttk.Button(row, text="Report", command=lambda r=it["report"]: self.open_report(r)).pack(side="left", padx=(6, 0))
            for w in (cover,):
                w.bind("<Double-1>", lambda e, p=path: os.startfile(p))
            self.lib_tiles.append({"frame": frame, "cover": cover, "text": f"{it['title']} {it['author']}".lower()})
        self.library_items = items
        self.lib_cols = 0
        self.layout_library(force=True)

    def on_lib_resize(self, event):
        self.lib_canvas.itemconfigure(self.lib_window, width=event.width)
        self.layout_library()

    def layout_library(self, force=False):
        width = max(1, self.lib_canvas.winfo_width())
        tile_w = int(200 * self.scale)
        cols = max(1, width // tile_w)
        query = self.lib_search.get().strip().lower() if hasattr(self, "lib_search") else ""
        visible = [t for t in self.lib_tiles if not query or query in t["text"]]
        for t in self.lib_tiles:
            t["frame"].grid_forget()
        self.lib_empty.grid_forget()
        for i, t in enumerate(visible):
            t["frame"].grid(row=i // cols, column=i % cols, sticky="nw")
        n = len(self.lib_tiles)
        folder = self.out_var.get()
        if not n:
            self.lib_count.config(text="No audiobooks yet")
            self.lib_empty.config(text=f"Audiobooks you convert will show up here.\nThey are saved in {folder}")
            self.lib_empty.grid(row=0, column=0, sticky="w", padx=10, pady=10)
        elif query:
            self.lib_count.config(text=f"{len(visible)} of {n} audiobooks")
        else:
            self.lib_count.config(text=f"{n} audiobook{'s' if n != 1 else ''}")
        self.lib_cols = cols

    # ------------------------------------------------------------- pause / resume
    def active_seconds(self):
        """Seconds spent converting, not counting time spent paused."""
        now = time.time()
        paused = self.paused_total + ((now - self.pause_started) if self.paused and self.pause_started else 0)
        return max(0.0, now - self.start_time - paused)

    def set_paused(self, on):
        if on == self.paused:
            return
        if on:
            try:
                PAUSE_FILE.write_text("paused")
            except OSError:
                return
            self.paused = True
            self.pause_started = time.time()
        else:
            try:
                PAUSE_FILE.unlink()
            except OSError:
                pass
            if self.pause_started:
                self.paused_total += time.time() - self.pause_started
            self.paused = False
            self.pause_started = 0.0
            self.last_line_time = time.time()      # the stall clock starts again after a pause
        self.pause_btn.config(text="\u25B6  Resume" if self.paused else "\u23F8  Pause")

    def toggle_pause(self):
        if not self.running:
            return
        self.set_paused(not self.paused)
        if self.paused:
            self.run_status.config(text="Pausing... the sentence being read finishes first.")
            self.root.after(1500, lambda: self.paused and self.run_status.config(
                text="Paused. Click Resume to carry on where it stopped."))
        self.refresh_run()

    # ------------------------------------------------------------- after finishing
    def make_after_box(self, parent, side="left"):
        if self.after_var is None:
            self.after_var = tk.StringVar(value=AFTER_ACTIONS["nothing"])
        box = ttk.Frame(parent)
        box.pack(side=side)
        ttk.Label(box, text="When finished:").pack(side="left")
        ttk.Combobox(box, textvariable=self.after_var, values=list(AFTER_ACTIONS.values()), state="readonly",
                     width=20).pack(side="left", padx=(6, 0))
        return box

    def after_action(self):
        label = self.after_var.get() if self.after_var else ""
        return next((k for k, v in AFTER_ACTIONS.items() if v == label), "nothing")

    def begin_after_action(self, action):
        """Count down, so it can still be stopped, then do it."""
        if action == "nothing" or self.running:
            return
        seconds = {"close": 15}.get(action, 60)
        win = tk.Toplevel(self.root)
        win.title("Audiobook Forge")
        win.transient(self.root)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        body = ttk.Frame(win, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Your audiobook is finished.", font=self.f_strong).pack(anchor="w")
        msg = ttk.Label(body, text="")
        msg.pack(anchor="w", pady=(6, 16))
        state = {"left": seconds, "cancelled": False}
        verb = AFTER_VERBS[action]

        def cancel():
            state["cancelled"] = True
            self.after_var.set(AFTER_ACTIONS["nothing"])
            win.destroy()

        def now():
            state["left"] = 0
            tick()

        def tick():
            if state["cancelled"] or not win.winfo_exists():
                return
            if state["left"] <= 0:
                win.destroy()
                self.after_var.set(AFTER_ACTIONS["nothing"])
                self.do_after_action(action)
                return
            msg.config(text=f"{verb} in {state['left']} seconds.")
            state["left"] -= 1
            win.after(1000, tick)

        row = ttk.Frame(body)
        row.pack(anchor="e")
        ttk.Button(row, text="Cancel", style="Accent.TButton", command=cancel).pack(side="right")
        ttk.Button(row, text=f"{AFTER_ACTIONS[action]} now", command=now).pack(side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.after_dialog = win
        tick()

    def do_after_action(self, action):
        commands = {
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            "hibernate": ["shutdown", "/h"],
            "shutdown": ["shutdown", "/s", "/t", "0"],
        }
        if action == "close":
            self.close_now()
        elif action in commands:
            self.run_system_command(commands[action])

    def run_system_command(self, cmd):
        try:
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            messagebox.showerror("Audiobook Forge", f"That did not work:\n{e}")

    # ------------------------------------------------------------- pasted text
    def on_paste_key(self, event):
        cls = event.widget.winfo_class() if hasattr(event.widget, "winfo_class") else ""
        if cls in ("TEntry", "Entry", "Text", "TCombobox") or self.running:
            return None
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        if text.strip():
            self.paste_text(text)
        return "break"

    def paste_text(self, initial=""):
        if self.running:
            return
        win = tk.Toplevel(self.root)
        win.title("Paste text")
        win.transient(self.root)
        win.geometry(f"{int(720 * self.scale)}x{int(560 * self.scale)}")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Paste or type the text to turn into an audiobook. Lines such as \"Chapter 2\" or "
                             "\"# Part Two\" start new chapters.", foreground="gray",
                  wraplength=int(680 * self.scale)).pack(anchor="w")
        trow = ttk.Frame(body)
        trow.pack(fill="x", pady=(10, 8))
        ttk.Label(trow, text="Title").pack(side="left")
        title_var = tk.StringVar(value=time.strftime("Pasted text %Y-%m-%d %H.%M"))
        ttk.Entry(trow, textvariable=title_var).pack(side="left", fill="x", expand=True, padx=(8, 0))
        dark = self.theme == "dark"
        txt = ScrolledText(body, wrap="word", relief="flat", bd=0, padx=10, pady=8,
                           font=tkfont.nametofont("SunValleyBodyFont", root=self.root),
                           background="#202020" if dark else "#ffffff", foreground="#ECEFF3" if dark else "#1c1c1c",
                           insertbackground="#ECEFF3" if dark else "#1c1c1c")
        txt.pack(fill="both", expand=True)
        if initial:
            txt.insert("1.0", initial)
        count = ttk.Label(body, text="", foreground="gray")
        count.pack(anchor="w", pady=(6, 0))

        def update_count(*_):
            words = len(txt.get("1.0", "end").split())
            count.config(text=f"{words:,} words, about {fmt_length(words / WORDS_PER_MIN)} of audio")
            txt.edit_modified(False)
        txt.bind("<<Modified>>", update_count)
        update_count()

        def use():
            text = txt.get("1.0", "end").strip()
            if len(text.split()) < 5:
                messagebox.showinfo("Audiobook Forge", "There is no text to convert yet.", parent=win)
                return
            title = title_var.get().strip() or "Pasted text"
            PASTED_DIR.mkdir(parents=True, exist_ok=True)
            path = PASTED_DIR / f"{safe_name(title)}.txt"
            n = 2
            while path.exists():
                path = PASTED_DIR / f"{safe_name(title)} ({n}).txt"
                n += 1
            path.write_text(text, encoding="utf-8")
            win.destroy()
            self.nb.select(0)
            self.load_book(path)
            if self.book == path:
                self.overrides = {"title": title} if title != path.stem else {}
                self.set_cover_from_book()

        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(12, 0))
        ttk.Button(foot, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(foot, text="Use this text", style="Accent.TButton", command=use).pack(side="right", padx=8)
        txt.focus_set()

    # ------------------------------------------------------------- setup check
    def check_setup(self, parent=None):
        win = tk.Toplevel(parent or self.root)
        win.title("Check my setup")
        win.transient(parent or self.root)
        win.geometry(f"{int(900 * self.scale)}x{int(620 * self.scale)}")
        try:
            win.iconbitmap(str(APP_DIR / "forge.ico"))
        except Exception:  # noqa: BLE001
            pass
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        status = ttk.Label(body, text="")
        status.pack(anchor="w")
        speak_var = tk.BooleanVar(value=not self.running)
        top = ttk.Frame(body)
        top.pack(fill="x", pady=(8, 0))
        run_btn = ttk.Button(top, text="Check again", style="Accent.TButton")
        run_btn.pack(side="left")
        ttk.Checkbutton(top, text="Also say a test sentence on the graphics card (about 30 seconds)",
                        variable=speak_var).pack(side="left", padx=(12, 0))
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        tree = ttk.Treeview(wrap, columns=("s", "title", "found"), show="headings", selectmode="browse")
        for col, text, width, stretch in (("s", "", 40, False), ("title", "Part", 190, False), ("found", "Found", 560, True)):
            tree.heading(col, text=text, anchor="w")
            tree.column(col, width=int(width * self.scale), stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("fail", "#C42B1C"), ("warn", "#9D5D00"), ("ok", "#0F7B0F")):
            tree.tag_configure(tag, foreground=color)
        detail = ttk.LabelFrame(body, text="What to do", padding=10)
        detail.pack(fill="x", pady=(10, 0))
        fix_lbl = ttk.Label(detail, text="Select a line to see more.", wraplength=int(840 * self.scale))
        fix_lbl.pack(anchor="w")
        action_btn = ttk.Button(detail, text="")
        results = {}
        icons = {"ok": "\u2714", "warn": "\u26A0", "fail": "\u2716", "info": "\u2139"}

        def show(res):
            if not win.winfo_exists():
                return
            run_btn.config(state="normal")
            tree.delete(*tree.get_children())
            results.clear()
            if res is None:
                status.config(text="The check itself could not run. Save diagnostics and send them.")
                return
            order = {"fail": 0, "warn": 1, "ok": 2, "info": 3}
            for i, r in enumerate(sorted(res, key=lambda r: order.get(r["status"], 4))):
                results[str(i)] = r
                tree.insert("", "end", iid=str(i), values=(icons.get(r["status"], ""), r["title"], r["found"]), tags=(r["status"],))
            bad = sum(r["status"] == "fail" for r in res)
            warn = sum(r["status"] == "warn" for r in res)
            status.config(text=(f"{bad} problem{'s' if bad != 1 else ''} to fix" if bad else "Everything needed is working") +
                          (f", {warn} thing{'s' if warn != 1 else ''} worth a look." if warn else "."))
            first = next((iid for iid, r in results.items() if r["status"] in ("fail", "warn")), None)
            if first:
                tree.selection_set(first)

        def run_check():
            run_btn.config(state="disabled")
            tree.delete(*tree.get_children())
            status.config(text="Checking... the graphics card part can take up to a minute.")
            args = ["--json"] + (["--speak"] if speak_var.get() and not self.running else []) + ([] if self.running else ["--idle"])

            def work():
                res = None
                try:
                    r = subprocess.run([sys.executable, str(APP_DIR / "selfcheck.py")] + args, cwd=APP_DIR, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW, timeout=600)
                    line = next((l for l in r.stdout.splitlines() if l.startswith("@results ")), None)
                    res = json.loads(line[len("@results "):]) if line else None
                except Exception:  # noqa: BLE001
                    res = None
                self.ui_q.put(lambda: show(res))
            threading.Thread(target=work, daemon=True).start()

        def do_action(kind):
            if kind == "remove_flags":
                for f in (PAUSE_FILE, GUI_BUSY):
                    if not self.running:
                        try:
                            f.unlink()
                        except OSError:
                            pass
                run_check()
            elif kind == "set_aside_bin":
                target = APP_DIR / "bin (not working)"
                try:
                    if target.exists():
                        import shutil
                        shutil.rmtree(target)
                    (APP_DIR / "bin").rename(target)
                    messagebox.showinfo("Audiobook Forge", "The broken copy was moved to \"bin (not working)\". The ffmpeg "
                                                           "installed on this PC is used from now on.", parent=win)
                except OSError as e:
                    messagebox.showerror("Audiobook Forge", f"Could not move it:\n{e}", parent=win)
                run_check()
            elif kind == "cleanup":
                self.cleanup_work(win)

        def on_select(_=None):
            sel = tree.selection()
            r = results.get(sel[0]) if sel else None
            action_btn.pack_forget()
            if not r:
                return
            fix_lbl.config(text=r["fix"] or "Nothing to do here.")
            labels = {"remove_flags": "Remove the leftover files", "set_aside_bin": "Set the broken copy aside",
                      "cleanup": "Clean up work files..."}
            if r.get("action") in labels:
                action_btn.config(text=labels[r["action"]], command=lambda k=r["action"]: do_action(k))
                action_btn.pack(anchor="w", pady=(8, 0))
        tree.bind("<<TreeviewSelect>>", on_select)
        run_btn.config(command=run_check)
        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(10, 0))
        ttk.Button(foot, text="Save diagnostics...", command=lambda: self.save_diagnostics(win)).pack(side="left")
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")
        run_check()

    # ------------------------------------------------------------- work files
    def work_folders(self):
        """[{"name", "path", "size", "modified", "state"}] for each book's folder in work."""
        out = []
        work = APP_DIR / "work"
        if not work.exists():
            return out
        for d in sorted(p for p in work.iterdir() if p.is_dir()):
            size, newest, chapters, pieces = 0, 0.0, 0, 0
            for root, _, files in os.walk(d):
                for f in files:
                    try:
                        st = os.stat(os.path.join(root, f))
                    except OSError:
                        continue
                    size += st.st_size
                    newest = max(newest, st.st_mtime)
                    chapters += f.endswith(".wav") and not f.endswith(".partial.wav")
                    pieces += f.endswith(".npy")
            parts = []
            if chapters:
                parts.append(f"{chapters} finished chapter{'s' if chapters != 1 else ''}")
            if pieces:
                parts.append(f"{pieces:,} paragraphs")
            out.append({"name": d.name, "path": d, "size": size, "modified": newest,
                        "state": ", ".join(parts) or "small leftovers"})
        return out

    def cleanup_work(self, parent=None):
        win = tk.Toplevel(parent or self.root)
        win.title("Clean up work files")
        win.transient(parent or self.root)
        win.geometry(f"{int(820 * self.scale)}x{int(520 * self.scale)}")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Audio kept for books that were stopped, failed, or converted with \"Keep paragraph audio\" on. "
                             "It lets a conversion carry on where it stopped. Deleting it is safe: finished audiobooks are "
                             "not touched, and a book converted again is simply read from the start.",
                  foreground="gray", wraplength=int(780 * self.scale)).pack(anchor="w")
        total_lbl = ttk.Label(body, text="Looking...", font=self.f_strong)
        total_lbl.pack(anchor="w", pady=(8, 0))
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        tree = ttk.Treeview(wrap, columns=("sel", "book", "size", "when", "what"), show="headings", selectmode="none")
        for col, text, width, anchor, stretch in (("sel", "", 40, "center", False), ("book", "Book", 300, "w", True),
                                                  ("size", "Size", 90, "e", False), ("when", "Last used", 120, "w", False),
                                                  ("what", "What is kept", 220, "w", False)):
            tree.heading(col, text=text, anchor="w" if anchor == "w" else anchor)
            tree.column(col, width=int(width * self.scale), anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        folders, chosen = {}, set()
        busy_name = safe_name(self.book.stem) if self.running and self.book else None

        def fill(items):
            if not win.winfo_exists():
                return
            tree.delete(*tree.get_children())
            folders.clear()
            chosen.clear()
            for i, f in enumerate(items):
                iid = str(i)
                folders[iid] = f
                in_use = f["name"] == busy_name
                when = time.strftime("%Y-%m-%d", time.localtime(f["modified"])) if f["modified"] else ""
                tree.insert("", "end", iid=iid, values=("" if in_use else UNCHECKED, f["name"] + ("  (converting now)" if in_use else ""),
                                                        f"{f['size'] / 1e9:.2f} GB", when, f["state"]))
            total = sum(f["size"] for f in items)
            total_lbl.config(text=f"{total / 1e9:.1f} GB in {len(items)} book folder{'s' if len(items) != 1 else ''}"
                             if items else "Nothing to clean up.")

        def scan():
            threading.Thread(target=lambda: self.ui_q.put(lambda items=self.work_folders(): fill(items)), daemon=True).start()

        def toggle(event):
            iid = tree.identify_row(event.y)
            if not iid or folders[iid]["name"] == busy_name:
                return
            if iid in chosen:
                chosen.discard(iid)
            else:
                chosen.add(iid)
            tree.set(iid, "sel", CHECKED if iid in chosen else UNCHECKED)

        def select_all():
            for iid, f in folders.items():
                if f["name"] != busy_name:
                    chosen.add(iid)
                    tree.set(iid, "sel", CHECKED)

        def delete():
            picks = [folders[i] for i in chosen]
            if not picks:
                return
            size = sum(f["size"] for f in picks) / 1e9
            if not messagebox.askyesno("Audiobook Forge", f"Delete the work files of {len(picks)} book{'s' if len(picks) != 1 else ''} "
                                                          f"({size:.1f} GB)?", parent=win):
                return
            total_lbl.config(text="Deleting...")

            def work():
                import shutil
                for f in picks:
                    shutil.rmtree(f["path"], ignore_errors=True)
                self.ui_q.put(scan)
            threading.Thread(target=work, daemon=True).start()

        tree.bind("<Button-1>", toggle)
        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(10, 0))
        ttk.Button(foot, text="Select all", command=select_all).pack(side="left")
        ttk.Button(foot, text="Delete selected", style="Accent.TButton", command=delete).pack(side="left", padx=8)
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")
        scan()

    # ------------------------------------------------------------- listening report
    def open_report(self, path=None):
        path = Path(path or self.report_path or "")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            messagebox.showinfo("Audiobook Forge", "There is no listening report for this book.")
            return
        win = tk.Toplevel(self.root)
        win.title(f"Listening report - {data.get('book', '')}")
        win.transient(self.root)
        win.geometry(f"{int(920 * self.scale)}x{int(700 * self.scale)}")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Places in this audiobook worth a quick listen. Select one and press Hear it to play a few "
                             "seconds from that spot. Words the voice had to guess are at the bottom: if one sounds wrong, "
                             "add a pronunciation fix and convert the book again (only those paragraphs are read again "
                             "when \"Keep paragraph audio\" is on).", foreground="gray",
                  wraplength=int(860 * self.scale)).pack(anchor="w")
        kinds = {"fixed": "Read again", "suspect": "May be cut off", "silence": "Long silence"}
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        tree = ttk.Treeview(wrap, columns=("at", "chapter", "what", "text"), show="headings", selectmode="browse")
        for col, text, width, stretch in (("at", "Time", 80, False), ("chapter", "Chapter", 170, False),
                                          ("what", "What", 130, False), ("text", "Text", 420, True)):
            tree.heading(col, text=text, anchor="w")
            tree.column(col, width=int(width * self.scale), stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        rows = {}
        clock = lambda sec: f"{int(sec) // 3600}:{int(sec) // 60 % 60:02d}:{int(sec) % 60:02d}"   # noqa: E731
        heard_kinds = {"ending": "End cut off", "missing": "Words missing", "extra": "Extra words"}

        def fill():
            tree.delete(*tree.get_children())
            rows.clear()
            check = data.get("check") or {}
            for k, e in enumerate(check.get("issues", [])):          # what listening found comes first
                iid = f"c{k}"
                rows[iid] = e
                tree.insert("", "end", iid=iid, values=(clock(e["book_at"]), e.get("chapter_title", ""),
                                                        heard_kinds.get(e["kind"], e["kind"]),
                                                        f"heard: {e.get('heard', '')}   |   text: {e.get('said', '')}"))
            for i, e in enumerate(data.get("events", [])):
                rows[str(i)] = e
                tree.insert("", "end", iid=str(i), values=(clock(e["book_at"]), e.get("chapter_title", ""),
                                                           kinds.get(e["kind"], e["kind"]), e.get("text", "")))
            for j, n in enumerate(data.get("names", [])):
                iid = f"n{j}"
                rows[iid] = {"book_at": n["book_at"], "chapter": n["chapter"], "word": n["word"]}
                tree.insert("", "end", iid=iid, values=(clock(n["book_at"]), f"{n['count']}x in the book",
                                                        "Unfamiliar word", n["word"]))
            if not rows:
                tree.insert("", "end", values=("", "", "", "Nothing in particular to check."))
            if check:
                part = f" (chapters {', '.join(map(str, check['chapters']))})" if check.get("chapters") else ""
                speed = f", {check['speed']}x realtime" if check.get("speed") else ""
                check_lbl.config(text=f"Checked by listening on {check.get('when', '')}{part}: {check.get('checked', 0)} "
                                      f"paragraphs, {len(check.get('issues', []))} worth a listen{speed}.")

        lrow = ttk.LabelFrame(body, text="Check by listening", padding=10)
        lrow.pack(fill="x", pady=(10, 0))
        check_lbl = ttk.Label(lrow, text="Whisper listens to the finished audiobook and compares every paragraph with "
                                         "the text, to find missing or wrong words. It runs on the graphics card and "
                                         "downloads its model the first time.", foreground="gray",
                              wraplength=int(860 * self.scale))
        check_lbl.pack(anchor="w")
        crow = ttk.Frame(lrow)
        crow.pack(fill="x", pady=(8, 0))
        models = {"Fast (base)": "base.en", "Accurate (small, slower)": "small.en"}
        model_var = tk.StringVar(value="Fast (base)")
        ttk.Combobox(crow, textvariable=model_var, values=list(models), state="readonly", width=24).pack(side="left")
        listen_btn = ttk.Button(crow, text="Check this book", style="Accent.TButton")
        listen_btn.pack(side="left", padx=(8, 0))
        first_btn = ttk.Button(crow, text="Time one chapter")
        first_btn.pack(side="left", padx=(8, 0))
        stop_btn = ttk.Button(crow, text="Stop", state="disabled")
        stop_btn.pack(side="left", padx=(8, 0))
        bar = ttk.Progressbar(crow, mode="determinate", maximum=100, length=int(180 * self.scale))
        bar.pack(side="left", padx=(12, 0))
        listen_state = {"proc": None}

        def listen(chapters=None):
            if self.running or self.ai_busy:
                check_lbl.config(text="Wait until the conversion has finished: both need the graphics card.")
                return
            if not data.get("paragraphs"):
                check_lbl.config(text="This book was converted before Check by listening existed. Convert it again "
                                      "(quick when its work files were kept), then check it.")
                return
            cmd = [sys.executable, "-u", str(APP_DIR / "bookcheck.py"), str(path), "--model", models[model_var.get()], "--progress"]
            if chapters:
                cmd += ["--chapters", chapters]
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            try:
                proc = subprocess.Popen(cmd, cwd=APP_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
            except OSError as e:
                check_lbl.config(text=f"Could not start the check: {e}")
                return
            listen_state["proc"] = proc
            for b in (listen_btn, first_btn):
                b.config(state="disabled")
            stop_btn.config(state="normal")
            bar["value"] = 0
            check_lbl.config(text="Starting... loading Whisper takes a little while (longer the first time, while it downloads).")

            def read():
                problem, summary = None, None
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("@check "):
                        done_n, total, chapter = line.split()[1:4]
                        self.ui_q.put(lambda d=int(done_n), t=int(total), c=chapter: progress(d, t, c))
                    elif line.startswith("@problem "):
                        problem = line[9:]
                    elif line.startswith(("Loading Whisper", "Listening on")):
                        self.ui_q.put(lambda m=line: win.winfo_exists() and check_lbl.config(text=m))
                    elif line.startswith("Checked "):
                        summary = line
                proc.wait()
                self.ui_q.put(lambda: finished(proc.returncode, problem, summary))
            threading.Thread(target=read, daemon=True).start()

        def progress(done_n, total, chapter):
            if win.winfo_exists():
                bar["value"] = done_n / max(1, total) * 100
                check_lbl.config(text=f"Listening... {done_n:,} of {total:,} paragraphs (chapter {chapter}).")

        def finished(code, problem, summary):
            listen_state["proc"] = None
            if not win.winfo_exists():
                return
            for b in (listen_btn, first_btn):
                b.config(state="normal")
            stop_btn.config(state="disabled")
            if code == 0:
                try:
                    data.update(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
                bar["value"] = 100
                fill()
            else:
                check_lbl.config(text=problem or "The check stopped." if listen_state.get("stopped") else
                                 (problem or f"The check stopped with an error (code {code}). Save diagnostics if it keeps happening."))
            listen_state["stopped"] = False

        def stop():
            proc = listen_state.get("proc")
            if proc and proc.poll() is None:
                listen_state["stopped"] = True
                proc.terminate()

        def time_one():
            chapters = data.get("paragraphs") or []
            if chapters:
                biggest = max(chapters, key=lambda c: c.get("duration", 0))
                listen(str(biggest["chapter"]))

        listen_btn.config(command=lambda: listen())
        first_btn.config(command=time_one)
        stop_btn.config(command=stop)
        win.protocol("WM_DELETE_WINDOW", lambda: (stop(), win.destroy()))
        fill()
        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(10, 0))
        status = ttk.Label(foot, text="", foreground="gray")

        def hear():
            sel = tree.selection()
            if not sel or sel[0] not in rows:
                return
            e = rows[sel[0]]
            src, at = Path(data.get("output", "")), max(0.0, e["book_at"] - 1.5)
            if src.is_dir():                     # a folder of MP3s: the chapter's own file, chapter time
                files = sorted(src.glob("*.mp3"))
                pos = e.get("chapter_pos", 0)
                if pos < len(files):
                    src, at = files[pos], max(0.0, e.get("at", 0) - 1.5)
            if not src.exists():
                status.config(text="The audiobook file was moved or deleted.")
                return
            clip = PREVIEW_DIR / "_report_clip.wav"
            status.config(text="Getting the clip...")

            def work():
                try:
                    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                    subprocess.run([checks.ffmpeg_path(APP_DIR), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}",
                                    "-t", "15", "-i", str(src), "-vn", "-ac", "1", str(clip)], timeout=120,
                                   creationflags=CREATE_NO_WINDOW, capture_output=True)
                    ok = clip.exists()
                except Exception:  # noqa: BLE001
                    ok = False
                self.ui_q.put(lambda: done(ok))

            def done(ok):
                if not win.winfo_exists():
                    return
                status.config(text="Playing 15 seconds from just before that spot." if ok else "Could not get the clip.")
                if ok:
                    self.play_wav(clip)
            threading.Thread(target=work, daemon=True).start()

        ttk.Button(foot, text="\u25B6 Hear it", style="Accent.TButton", command=hear).pack(side="left")
        ttk.Button(foot, text="Open as text", command=lambda: os.startfile(str(path.with_suffix(".txt")))
                   if path.with_suffix(".txt").exists() else None).pack(side="left", padx=8)
        ttk.Button(foot, text="Fix pronunciations...", command=lambda: (win.destroy(), self.open_pronunciations())).pack(side="left")
        status.pack(side="left", padx=12)
        ttk.Button(foot, text="Close", command=win.destroy).pack(side="right")
        tree.bind("<Double-1>", lambda e: hear())

    # ------------------------------------------------------------- diagnostics
    def save_diagnostics(self, parent=None):
        parent = parent or self.root
        desktop = Path.home() / "Desktop"
        path = filedialog.asksaveasfilename(parent=parent, title="Save diagnostics",
                                            initialdir=str(desktop if desktop.exists() else APP_DIR),
                                            initialfile=f"Audiobook Forge diagnostics {time.strftime('%Y-%m-%d %H-%M')}.zip",
                                            defaultextension=".zip", filetypes=[("Zip file", "*.zip")])
        if not path:
            return
        window_log = self.log.get("1.0", "end") if hasattr(self, "log") else ""
        self.preview_lbl.config(text="Saving diagnostics... (checking the graphics card can take a minute)")

        def work():
            try:
                import diagnostics
                result, error = diagnostics.make_zip(path, window_log), None
            except Exception as e:  # noqa: BLE001
                result, error = None, e
            self.ui_q.put(lambda: done(result, error))

        def done(result, error):
            self.preview_lbl.config(text="")
            if error:
                messagebox.showerror("Audiobook Forge", f"Could not save diagnostics:\n{error}")
                return
            if messagebox.askyesno("Audiobook Forge", f"Diagnostics saved:\n{result}\n\nShow the file in its folder?"):
                try:
                    subprocess.Popen(["explorer", "/select,", str(result)])
                except OSError:
                    pass
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- samples
    def compare_half(self, label):
        """The listening check for half precision: the same passage, full precision (A) then half (B)."""
        if self.sample_busy or self.running or not self.book or self.batch:
            label.config(text="Load a single book first (and wait until nothing is converting).")
            return
        self.persist()
        self.sample_busy = True
        label.config(text="Making both samples... about a minute.")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        base = [sys.executable, "-u", str(APP_DIR / "convert.py"), str(self.book), "--voice", self.voice_id(),
                "--speed", f"{self.speed_var.get():.2f}"]
        if self.selected and len(self.selected) != len(self.chapters):
            base += ["--chapters", compress_ranges(self.selected)]
        wavs = [PREVIEW_DIR / "_compare_full.wav", PREVIEW_DIR / "_compare_half.wav"]

        def work():
            made = []
            for wav, half in zip(wavs, (False, True)):
                try:
                    wav.unlink()
                except OSError:
                    pass
                try:
                    subprocess.run(base + ["--sample", str(wav), "--options", json.dumps({"half": half})], cwd=APP_DIR,
                                   env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   creationflags=CREATE_NO_WINDOW, timeout=900)
                except Exception:  # noqa: BLE001
                    pass
                made.append(wav.exists())
            self.ui_q.put(lambda: play(made))

        def length(wav):
            import wave
            try:
                with wave.open(str(wav)) as w:
                    return w.getnframes() / w.getframerate()
            except Exception:  # noqa: BLE001
                return 20.0

        def play(made):
            self.sample_busy = False
            if not label.winfo_exists():
                return
            if not all(made):
                label.config(text="The samples could not be made. Try Hear a sample on the main screen for details.")
                return
            label.config(text="Playing A: full precision...")
            self.play_wav(wavs[0])
            gap = int(length(wavs[0]) * 1000) + 1200
            self.root.after(gap, lambda: label.winfo_exists() and (label.config(text="Playing B: half precision..."),
                                                                     self.play_wav(wavs[1])))
            self.root.after(gap + int(length(wavs[1]) * 1000) + 500, lambda: label.winfo_exists() and label.config(
                text="Done. If B sounded the same as A, half precision is safe to keep on."))
        threading.Thread(target=work, daemon=True).start()

    def speed_x(self):
        """Realtime factor for estimates: what the last conversions really ran at, else a typical value."""
        try:
            return max(2.0, float(self.settings.get("measured_x") or SPEED_X))
        except (TypeError, ValueError):
            return SPEED_X

    def remember_speed(self):
        """After a finished book: keep a smoothed realtime factor (audio length / time taken) for estimates."""
        audio = self.total_audio_sec or 0
        took = self.active_seconds()
        if audio < 300 or took < 30:
            return
        x = audio / took
        old = self.settings.get("measured_x")
        self.settings["measured_x"] = round(x if not old else 0.6 * float(old) + 0.4 * x, 1)
        save_settings(self.settings)

    def book_sample(self, at=None, status=None, done_cb=None):
        """Read a short stretch of the loaded book with every current setting (voice, speed, fixes, dialogue
        voices, sound options) and play it. at: "chapter:paragraph" for one paragraph; otherwise about a
        minute from the middle of the selected chapters. status(text) shows progress; done_cb() at the end."""
        if self.sample_busy or self.running or not self.book or self.batch:
            return
        show = status or (lambda t: self.preview_lbl.config(text=t))
        wav = PREVIEW_DIR / "_book_sample.wav"
        cmd = [sys.executable, "-u", str(APP_DIR / "convert.py"), str(self.book), "--voice", self.voice_id(),
               "--speed", f"{self.speed_var.get():.2f}", "--sample", str(wav)]
        if at:
            cmd += ["--sample-at", at]
        elif self.selected and len(self.selected) != len(self.chapters):
            cmd += ["--chapters", compress_ranges(self.selected)]
        self.persist()
        self.sample_busy = True
        self.sample_btn.config(state="disabled")
        show("Making a sample with your current settings... about 20 seconds (longer the first time).")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")

        def work():
            try:
                wav.unlink()
            except OSError:
                pass
            lines = []
            try:
                r = subprocess.run(cmd, cwd=APP_DIR, env=env, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", creationflags=CREATE_NO_WINDOW, timeout=900)
                lines = (r.stdout or "").splitlines()
            except Exception as e:  # noqa: BLE001
                lines = [str(e)]
            self.ui_q.put(lambda: finished(lines))

        def finished(lines):
            self.sample_busy = False
            if self.view == "ready" and self.book and not self.running:
                self.sample_btn.config(state="normal")
            where = next((l.split()[1:] for l in lines if l.startswith("@sample-from ")), None)
            if wav.exists():
                if where and not at:
                    ch = int(where[0])
                    title = self.chapters[ch - 1].title if 0 < ch <= len(self.chapters) else f"chapter {ch}"
                    show(f"Playing a sample from {title}.")
                else:
                    show("Playing the sample.")
                self.play_wav(wav)
            else:
                problem = next((l for l in reversed(lines) if l.strip() and not l.startswith(("@", "WARNING"))), "")
                show("Could not make a sample." + (f" ({problem[:120]})" if problem else ""))
            if done_cb:
                done_cb()
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- voice mixer
    def refresh_voice_lists(self):
        load_custom_voices()
        self.voice_box.config(values=list(VOICES.values()))
        self.dialogue_box.config(values=[SAME_VOICE, AUTO_VOICES, CAST_VOICES] + list(VOICES.values()))
        if self.voice_var.get() not in VOICES.values():
            self.voice_var.set(VOICES["af_heart"])
        if self.dialogue_var.get() not in [SAME_VOICE, AUTO_VOICES, CAST_VOICES] + list(VOICES.values()):
            self.dialogue_var.set(SAME_VOICE)
        self.persist()

    def open_mixer(self):
        win = tk.Toplevel(self.root)
        win.title("Mix a voice")
        win.transient(self.root)
        win.resizable(False, False)
        try:
            win.iconbitmap(str(APP_DIR / "forge.ico"))
        except Exception:  # noqa: BLE001
            pass
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        wrap = int(520 * self.scale)
        ttk.Label(body, text="Blend two voices into one of your own, for example mostly Heart with a little Bella. A saved "
                             "mix can be the narrator, the voice for quoted speech, or a character's voice.",
                  foreground="gray", wraplength=wrap).pack(anchor="w")
        builtin = {VOICES[v]: v for v in BUILTIN_VOICES}

        box = ttk.LabelFrame(body, text="New mix", padding=12)
        box.pack(fill="x", pady=(12, 0))
        a_var = tk.StringVar(value=VOICES["af_heart"])
        b_var = tk.StringVar(value=VOICES["af_bella"])
        grid = ttk.Frame(box)
        grid.pack(fill="x")
        for row, (label, var) in enumerate((("First voice", a_var), ("Second voice", b_var))):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            ttk.Combobox(grid, textvariable=var, values=list(builtin), state="readonly", width=30).grid(row=row, column=1, sticky="w")
        weight = tk.DoubleVar(value=70.0)
        share = ttk.Label(box, text="")
        share.pack(anchor="w", pady=(10, 0))

        def show_share(*_):
            w = round(weight.get())
            share.config(text=f"{w}% {a_var.get().split(' (')[0]}  +  {100 - w}% {b_var.get().split(' (')[0]}")
        ttk.Scale(box, from_=100, to=0, variable=weight, command=show_share).pack(fill="x", pady=(4, 0))
        for v in (a_var, b_var):
            v.trace_add("write", show_share)
        show_share()
        nrow = ttk.Frame(box)
        nrow.pack(fill="x", pady=(10, 0))
        ttk.Label(nrow, text="Name").pack(side="left")
        name_var = tk.StringVar(value="My voice")
        ttk.Entry(nrow, textvariable=name_var, width=24).pack(side="left", padx=(8, 0))
        hear_btn = ttk.Button(nrow, text="\u25B6 Hear it")
        hear_btn.pack(side="left", padx=(10, 0))
        save_btn = ttk.Button(nrow, text="Save", style="Accent.TButton")
        save_btn.pack(side="left", padx=(8, 0))
        status = ttk.Label(body, text="", foreground="gray", wraplength=wrap)

        mine = ttk.LabelFrame(body, text="Your voices", padding=12)
        mine.pack(fill="x", pady=(12, 0))
        lst = tk.Listbox(mine, height=5, activestyle="none", exportselection=False)
        lst.pack(fill="x")
        mrow = ttk.Frame(mine)
        mrow.pack(fill="x", pady=(8, 0))
        status.pack(anchor="w", pady=(10, 0))
        items = []

        def refill():
            lst.delete(0, "end")
            items.clear()
            for vid, name, desc in voicemix.list_custom():
                items.append(vid)
                lst.insert("end", f"{name}   ({desc})" if desc else name)
            if not items:
                lst.insert("end", "No mixes yet.")

        def chosen():
            sel = lst.curselection()
            return items[sel[0]] if sel and sel[0] < len(items) else None

        def run_helper(args, done, timeout=900):
            def work():
                try:
                    r = subprocess.run([sys.executable, str(APP_DIR / "voicemix.py")] + args, cwd=APP_DIR, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW, timeout=timeout)
                    result = (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))
                except Exception as e:  # noqa: BLE001
                    result = (False, str(e))
                self.ui_q.put(lambda: done(*result))
            threading.Thread(target=work, daemon=True).start()

        def busy(on, text=""):
            for b in (hear_btn, save_btn):
                b.config(state="disabled" if on else "normal")
            status.config(text=text)

        def hear():
            if self.running:
                status.config(text="Wait for the conversion to finish first.")
                return
            wav = PREVIEW_DIR / "_mix.wav"
            try:
                wav.unlink()
            except OSError:
                pass
            busy(True, "Making a sample... the first one can take up to a minute.")

            def done(ok, output):
                if not win.winfo_exists():
                    return
                busy(False, "" if ok and wav.exists() else "Could not make a sample. " + output.strip()[-200:])
                if ok and wav.exists():
                    self.play_wav(wav)
            run_helper(["try", builtin[a_var.get()], builtin[b_var.get()], f"{weight.get() / 100:.3f}", str(wav)], done)

        def save():
            name = voicemix.safe(name_var.get())
            if not name:
                status.config(text="Give the voice a name first.")
                return
            if any(n == name for _, n, _ in voicemix.list_custom()) and not messagebox.askyesno(
                    "Audiobook Forge", f"Replace your voice \"{name}\"? Books already read with it will be read again "
                                       "the next time you convert them.", parent=win):
                return
            busy(True, "Saving...")

            def done(ok, output):
                if not win.winfo_exists():
                    return
                busy(False, f"Saved \"{name}\". It is now in every voice list." if ok else "Could not save it. " + output.strip()[-200:])
                refill()
                self.refresh_voice_lists()
            run_helper(["make", name, builtin[a_var.get()], builtin[b_var.get()], f"{weight.get() / 100:.3f}"], done)

        def use():
            vid = chosen()
            if vid:
                self.voice_var.set(VOICES.get(vid, VOICES["af_heart"]))
                self.persist()
                status.config(text=f"{VOICES.get(vid)} is now the narrator.")

        def delete():
            vid = chosen()
            if not vid or not messagebox.askyesno("Audiobook Forge", f"Delete {VOICES.get(vid, vid)}?", parent=win):
                return
            voicemix.delete(vid)
            refill()
            self.refresh_voice_lists()

        hear_btn.config(command=hear)
        save_btn.config(command=save)
        ttk.Button(mrow, text="Use as narrator", command=use).pack(side="left")
        ttk.Button(mrow, text="Delete", command=delete).pack(side="left", padx=(8, 0))
        refill()
        ttk.Button(body, text="Close", command=win.destroy).pack(anchor="e", pady=(12, 0))

    # ------------------------------------------------------------- casting
    def cast_characters(self):
        if not self.chapters or self.batch:
            messagebox.showinfo("Audiobook Forge", "Load a book first. Then this finds the characters who speak in it "
                                                   "and gives each one a voice, which you can change.")
            return
        import speakers
        book_stem = self.book.stem
        narration = self.settings.get("narration") or {}
        female, male = narration.get("dialogue_female", "af_bella"), narration.get("dialogue_male", "am_michael")
        narrator = self.voice_id()
        win = tk.Toplevel(self.root)
        win.title("Cast characters")
        win.transient(self.root)
        win.geometry(f"{int(820 * self.scale)}x{int(780 * self.scale)}")
        try:
            win.iconbitmap(str(APP_DIR / "forge.ico"))
        except Exception:  # noqa: BLE001
            pass
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Everyone who speaks at least twice, most lines first. Main characters get a voice of their "
                             "own; smaller parts share the women's or men's voice (Settings > Narration). Change any voice "
                             "and press \u25B6 to hear one of their lines. Lines whose speaker is unclear stay with the "
                             "narrator.", wraplength=int(780 * self.scale), foreground="gray").pack(anchor="w")
        status = ttk.Label(body, text="Finding who speaks...")
        status.pack(anchor="w", pady=(8, 0))
        outer = ttk.Frame(body)
        outer.pack(fill="both", expand=True, pady=(8, 0))
        canvas = tk.Canvas(outer, highlightthickness=0, bd=0, background=self.theme_bg())
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        choices = {"Narrator's voice": narrator}
        for v in list(VOICES) + [x for x in speakers.FEMALE_POOL + speakers.MALE_POOL if x not in VOICES]:
            choices[voice_label(v)] = v
        rows = {}
        state = {"busy": False, "characters": []}

        def label_of(v):
            return "Narrator's voice" if v == narrator else voice_label(v)

        def hear(name, button):
            if state["busy"] or self.running:
                return
            c = next(c for c in state["characters"] if c["name"] == name)
            text = self.as_spoken(c["example"] or f"Hello, my name is {name}.", markup=True).strip()
            state["busy"] = True
            button.config(state="disabled")
            status.config(text=f"Making a sample of {c['display']}...")

            def done(wav):
                state["busy"] = False
                if not win.winfo_exists():
                    return
                button.config(state="normal")
                status.config(text="" if wav else "Could not make a sample.")
                if wav:
                    self.play_wav(wav)
            self.speak_sample(text, done, voice=choices.get(rows[name].get(), narrator))

        def fill(characters, voices):
            for w in inner.winfo_children():
                w.destroy()
            rows.clear()
            for col, text in enumerate(("Character", "Lines", "", "Voice", "")):
                ttk.Label(inner, text=text, foreground="gray").grid(row=0, column=col, sticky="w", padx=(0, 12), pady=(0, 6))
            for r, c in enumerate(characters, 1):
                ttk.Label(inner, text=c["display"], font=self.f_strong).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=3)
                ttk.Label(inner, text=str(c["lines"])).grid(row=r, column=1, sticky="e", padx=(0, 12))
                ttk.Label(inner, text={"f": "woman", "m": "man"}.get(c["gender"], "?"), foreground="gray").grid(
                    row=r, column=2, sticky="w", padx=(0, 12))
                var = tk.StringVar(value=label_of(voices.get(c["name"], narrator)))
                ttk.Combobox(inner, textvariable=var, values=list(choices), state="readonly", width=34).grid(
                    row=r, column=3, sticky="w", padx=(0, 8))
                b = ttk.Button(inner, text="\u25B6")
                b.config(command=lambda n=c["name"], btn=b: hear(n, btn))
                b.grid(row=r, column=4, sticky="w")
                rows[c["name"]] = var

        def loaded(characters, voices):
            if not win.winfo_exists():
                return
            state["characters"] = characters
            if not characters:
                status.config(text="No characters with speech tags (\"said Anna\") were found in this book.")
                return
            status.config(text=f"{len(characters)} character{'s' if len(characters) != 1 else ''} found.")
            fill(characters, voices)

        def scan(reset=False):
            def work():
                try:
                    guesser = speakers.SpeakerGuesser(p for c in self.chapters for p in c.paragraphs)
                    ai = speakers.load_ai_tags(book_stem, CASTING_DIR) if ai_var.get() else None
                    if ai:
                        guesser.use_ai(ai)
                    chosen = {} if reset else speakers.load_cast(book_stem, CASTING_DIR)
                    characters = speakers.find_characters(self.chapters, guesser, keep=chosen)
                    voices = speakers.cast(characters, narrator, female, male, chosen)
                except Exception:  # noqa: BLE001
                    characters, voices = [], {}
                self.ui_q.put(lambda: loaded(characters, voices))
            threading.Thread(target=work, daemon=True).start()

        # ---- the language model reads the book and says who speaks each line
        ai_box = ttk.LabelFrame(body, text="Find speakers with AI", padding=10)
        ai_box.pack(fill="x", pady=(10, 0))
        ai_lbl = ttk.Label(ai_box, text="", foreground="gray", wraplength=int(760 * self.scale))
        ai_lbl.pack(anchor="w")
        arow = ttk.Frame(ai_box)
        arow.pack(fill="x", pady=(8, 0))
        import llm
        ai_models = {label: key for key, label in llm.MODEL_LABELS.items()}
        ai_model_var = tk.StringVar(value=llm.MODEL_LABELS[(self.settings.get("ai_model") or "best")
                                                           if (self.settings.get("ai_model") or "best") in llm.MODELS else "best"])
        ttk.Combobox(arow, textvariable=ai_model_var, values=list(ai_models), state="readonly", width=40).pack(side="left")
        ai_btn = ttk.Button(arow, text="Read the book", style="Accent.TButton")
        ai_btn.pack(side="left", padx=(8, 0))
        ai_stop = ttk.Button(arow, text="Stop", state="disabled")
        ai_stop.pack(side="left", padx=(8, 0))
        ai_bar = ttk.Progressbar(arow, mode="determinate", maximum=100, length=int(150 * self.scale))
        ai_bar.pack(side="left", padx=(12, 0))
        ai_var = tk.BooleanVar(value=(self.settings.get("narration") or {}).get("ai_speakers", True))

        def ai_toggle():
            self.settings.setdefault("narration", {})["ai_speakers"] = bool(ai_var.get())
            save_settings(self.settings)
            status.config(text="Finding who speaks...")
            scan()
        ttk.Checkbutton(ai_box, text="Use the AI's speaker tags for this book (when it has some)", variable=ai_var,
                        command=ai_toggle).pack(anchor="w", pady=(8, 0))
        ai_state = {"proc": None, "stopped": False}

        def ai_describe():
            data = speakers.load_ai_tags(book_stem, CASTING_DIR)
            keys = {speakers.chapter_key(c.paragraphs) for c in self.chapters}
            done = sum(1 for k in (data or {}).get("chapters", {}) if k in keys)
            if done:
                ai_lbl.config(text=f"The AI has read {done} of {len(self.chapters)} chapters"
                                   + (f" ({data.get('made')})" if data.get("made") else "") + ". Its tags are used for "
                                   "lines without a clear speech tag; \"said Rhys\" and \"I said\" in the text still win. "
                                   + ("Press Read the book to carry on with the rest." if done < len(self.chapters) else ""))
            else:
                ai_lbl.config(text="A language model on your graphics card reads every conversation and works out who "
                                   "says each line, including lines with no \"he said\". It downloads once (the best "
                                   "model is 8 GB) and takes roughly 10 to 30 minutes for a novel. Nothing leaves your PC.")
        ai_describe()

        def ai_run():
            if self.running or self.ai_busy:
                ai_lbl.config(text="Wait until the conversion (or the other AI job) has finished: both need the graphics card.")
                return
            key = ai_models.get(ai_model_var.get(), "best")
            self.settings["ai_model"] = key
            save_settings(self.settings)
            cmd = [sys.executable, "-u", str(APP_DIR / "speakertag.py"), str(self.book), "--model", key, "--progress"]
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            try:
                proc = subprocess.Popen(cmd, cwd=APP_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
            except OSError as e:
                ai_lbl.config(text=f"Could not start the AI: {e}")
                return
            self.ai_busy = True
            ai_state.update(proc=proc, stopped=False, started=time.time())
            ai_btn.config(state="disabled")
            ai_stop.config(state="normal")
            ai_bar["value"] = 0
            ai_lbl.config(text="Starting the language model..." + ("" if llm.downloaded(key) else
                                                                   " The first time it downloads, which takes a while."))

            def read():
                problem, summary = None, None
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("@tag "):
                        try:
                            d, t, c = line.split()[1:4]
                        except ValueError:
                            continue
                        self.ui_q.put(lambda d=int(d), t=int(t), c=c: ai_progress(d, t, c))
                    elif line.startswith("@problem "):
                        problem = line[9:]
                    elif line.startswith("Tagged "):
                        summary = line
                    elif line.startswith(("Loading", "Reading", "No graphics")):
                        self.ui_q.put(lambda m=line: win.winfo_exists() and ai_lbl.config(text=m))
                proc.wait()
                self.ui_q.put(lambda: ai_finished(proc.returncode, problem, summary))
            threading.Thread(target=read, daemon=True).start()

        def ai_progress(done_n, total, chapter):
            if not win.winfo_exists():
                return
            ai_bar["value"] = done_n / max(1, total) * 100
            left = ""
            spent = time.time() - ai_state.get("started", time.time())
            if done_n and spent > 20 and done_n < total:
                left = f", about {max(1, round(spent / done_n * (total - done_n) / 60))} min left"
            ai_lbl.config(text=f"Reading... {done_n:,} of {total:,} lines (chapter {chapter}){left}.")

        def ai_finished(code, problem, summary):
            self.ai_busy = False
            ai_state["proc"] = None
            if not win.winfo_exists():
                return
            ai_btn.config(state="normal")
            ai_stop.config(state="disabled")
            if code == 0:
                ai_bar["value"] = 100
                ai_describe()
                if summary:
                    ai_lbl.config(text=summary + " The cast below now uses them.")
                ai_var.set(True)
                self.settings.setdefault("narration", {})["ai_speakers"] = True
                save_settings(self.settings)
                status.config(text="Finding who speaks...")
                scan()
            elif ai_state.get("stopped"):
                ai_describe()
                ai_lbl.config(text="Stopped. What it read so far is kept; Read the book carries on from there.")
            else:
                ai_lbl.config(text=problem or f"The AI stopped with an error (code {code}). Save diagnostics if it keeps happening.")

        def ai_halt():
            proc = ai_state.get("proc")
            if proc and proc.poll() is None:
                ai_state["stopped"] = True
                proc.terminate()

        ai_btn.config(command=ai_run)
        ai_stop.config(command=ai_halt)
        win.protocol("WM_DELETE_WINDOW", lambda: (ai_halt(), win.destroy()))
        scan()

        def save():
            voices = {name: choices.get(var.get(), narrator) for name, var in rows.items()}
            try:
                speakers.save_cast(book_stem, CASTING_DIR, voices)
            except OSError as e:
                messagebox.showerror("Audiobook Forge", f"The cast could not be saved:\n{e}", parent=win)
                return
            self.dialogue_var.set(CAST_VOICES)
            self.persist()
            ai_halt()
            win.destroy()

        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(12, 0))
        ttk.Button(foot, text="Re-cast automatically", command=lambda: (status.config(text="Finding who speaks..."),
                                                                        scan(reset=True))).pack(side="left")
        ttk.Button(foot, text="Cancel", command=lambda: (ai_halt(), win.destroy())).pack(side="right")
        ttk.Button(foot, text="Save", style="Accent.TButton", command=save).pack(side="right", padx=8)

    # ------------------------------------------------------------- name check
    def speak_sample(self, text, on_done, voice=None):
        """Make a short sample of text (already as it will be spoken) with the current voice, in the background.
        on_done(wav or None) runs on the window thread."""
        wav = PREVIEW_DIR / "_sample.wav"

        def work():
            try:
                wav.unlink()
            except OSError:
                pass
            try:
                subprocess.run([sys.executable, str(APP_DIR / "preview.py"), voice or self.voice_id(), str(wav), text, "--raw"],
                               cwd=APP_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=CREATE_NO_WINDOW, timeout=600)
            except Exception:  # noqa: BLE001
                pass
            self.ui_q.put(lambda: on_done(wav if wav.exists() else None))
        threading.Thread(target=work, daemon=True).start()

    def check_names(self):
        if not self.chapters or self.batch:
            messagebox.showinfo("Audiobook Forge", "Load a book first. Then this lists the names and unusual words "
                                                   "the voice will have to guess, so you can check them before converting.")
            return
        win = tk.Toplevel(self.root)
        win.title("Check names")
        win.transient(self.root)
        win.geometry(f"{int(900 * self.scale)}x{int(680 * self.scale)}")
        try:
            win.iconbitmap(str(APP_DIR / "forge.ico"))
        except Exception:  # noqa: BLE001
            pass
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="These words are not in the voice's dictionary, so it guesses how to say them. Most guesses "
                             "are fine. Listen to the ones that appear most, and add a fix for any that sound wrong. "
                             "Fixes apply to every book.", wraplength=int(820 * self.scale), foreground="gray").pack(anchor="w")
        status = ttk.Label(body, text="Looking through the book...")
        status.pack(anchor="w", pady=(8, 0))
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("word", "times", "fixed", "ai", "example")
        tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor, stretch in (("word", "Word", 150, "w", False), ("times", "Times", 70, "e", False),
                                                  ("fixed", "Fix", 50, "center", False),
                                                  ("ai", "AI suggests", 130, "w", False),
                                                  ("example", "Where it appears", 420, "w", True)):
            tree.heading(col, text=text, anchor="w" if anchor == "w" else anchor)
            tree.column(col, width=int(width * self.scale), anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        form = ttk.LabelFrame(body, text="Fix the selected word", padding=10)
        form.pack(fill="x", pady=(10, 0))
        r1 = ttk.Frame(form)
        r1.pack(fill="x")
        word_lbl = ttk.Label(r1, text="(pick a word above)", width=18)
        word_lbl.pack(side="left")
        ttk.Label(r1, text="Say it as").pack(side="left", padx=(8, 6))
        say_var = tk.StringVar()
        say_entry = ttk.Entry(r1, textvariable=say_var, width=24)
        say_entry.pack(side="left")
        hear_old = ttk.Button(r1, text="\u25B6 As it is", state="disabled")
        hear_old.pack(side="left", padx=(10, 0))
        hear_new = ttk.Button(r1, text="\u25B6 My version", state="disabled")
        hear_new.pack(side="left", padx=(6, 0))
        add_btn = ttk.Button(r1, text="Add fix", style="Accent.TButton", state="disabled")
        add_btn.pack(side="left", padx=(10, 0))
        hint = ttk.Label(form, text="Spell it the way it sounds, in small letters with dashes between parts, for example "
                                    "Thanos \u2192 than-ohss. The sample reads the sentence where the word appears.",
                         foreground="gray", wraplength=int(800 * self.scale))
        hint.pack(anchor="w", pady=(6, 0))
        r2 = ttk.Frame(form)
        r2.pack(fill="x", pady=(8, 0))
        sug_btn = ttk.Button(r2, text="Suggest with AI", state="disabled")
        sug_btn.pack(side="left")
        sug_stop = ttk.Button(r2, text="Stop", state="disabled")
        sug_stop.pack(side="left", padx=(6, 0))
        all_btn = ttk.Button(r2, text="Add all suggestions", state="disabled")
        all_btn.pack(side="left", padx=(6, 0))
        sug_lbl = ttk.Label(r2, text="The language model on your graphics card suggests how to say the words that do "
                                     "not have a fix yet. Listen before you add them.", foreground="gray",
                            wraplength=int(520 * self.scale))
        sug_lbl.pack(side="left", padx=(10, 0))
        found = {}
        suggested = {}
        state = {"word": None, "busy": False, "proc": None, "stopped": False}

        def suggest_all():
            if self.running or self.ai_busy:
                sug_lbl.config(text="Wait until the conversion (or the other AI job) has finished: both need the graphics card.")
                return
            import llm
            fixed = {r["find"].strip().lower() for r in pron.load_rules()}
            items = [{"word": w, "example": ex} for iid, (w, ex) in found.items()
                     if w.lower() not in fixed and w not in suggested and not tree.set(iid, "fixed")][:80]
            if not items:
                sug_lbl.config(text="Every word already has a fix or a suggestion.")
                return
            key = self.settings.get("ai_model") or "best"
            key = key if key in llm.MODELS else "best"
            words_file = PREVIEW_DIR / "_suggest.json"
            try:
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                words_file.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                sug_lbl.config(text=f"Could not start: {e}")
                return
            title = self.overrides.get("title") or self.meta.get("title") or self.book.stem
            author = self.overrides.get("author") or self.meta.get("creator", "")
            cmd = [sys.executable, "-u", str(APP_DIR / "llm.py"), "suggest", str(words_file), "--model", key,
                   "--title", title, "--author", author]
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            try:
                proc = subprocess.Popen(cmd, cwd=APP_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
            except OSError as e:
                sug_lbl.config(text=f"Could not start the AI: {e}")
                return
            self.ai_busy = True
            state.update(proc=proc, stopped=False)
            sug_btn.config(state="disabled")
            sug_stop.config(state="normal")
            sug_lbl.config(text=f"Starting the language model for {len(items)} words..." +
                                ("" if llm.downloaded(key) else " The first time it downloads, which takes a while."))
            iid_of = {w: iid for iid, (w, _) in found.items()}

            def read():
                problem = None
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    if line.startswith("@suggest "):
                        word, _, say = line[9:].partition("\t")
                        self.ui_q.put(lambda w=word, v=say.strip(): got(w, v, iid_of.get(w)))
                    elif line.startswith("@problem "):
                        problem = line[9:]
                    elif line.startswith(("Loading", "Suggesting", "No graphics")):
                        self.ui_q.put(lambda m=line: win.winfo_exists() and sug_lbl.config(text=m))
                proc.wait()
                self.ui_q.put(lambda: sug_done(proc.returncode, problem))
            threading.Thread(target=read, daemon=True).start()

        def got(word, say, iid):
            if not say or iid is None or not win.winfo_exists():
                return
            suggested[word] = say
            tree.set(iid, "ai", say)
            all_btn.config(state="normal")
            if state["word"] == iid and say_var.get().strip() in ("", word.lower()):
                say_var.set(say)
            sug_lbl.config(text=f"{len(suggested)} suggestion{'s' if len(suggested) != 1 else ''} so far...")

        def sug_done(code, problem):
            self.ai_busy = False
            state["proc"] = None
            if not win.winfo_exists():
                return
            sug_btn.config(state="normal")
            sug_stop.config(state="disabled")
            if code == 0 or state["stopped"]:
                sug_lbl.config(text=f"{len(suggested)} suggestion{'s' if len(suggested) != 1 else ''}. Pick a word to hear "
                                    "it (\u25B6 My version), change it if needed, then Add fix.")
            else:
                sug_lbl.config(text=problem or f"The AI stopped with an error (code {code}).")

        def sug_halt():
            proc = state.get("proc")
            if proc and proc.poll() is None:
                state["stopped"] = True
                proc.terminate()

        def add_all():
            todo = {iid: suggested[w] for iid, (w, _) in found.items() if w in suggested and not tree.set(iid, "fixed")}
            if not todo or not messagebox.askyesno(
                    "Audiobook Forge", f"Add all {len(todo)} suggestions as pronunciation fixes without listening to "
                                       "them? You can change or turn off any of them later in Pronunciation fixes.",
                    parent=win):
                return
            rules = pron.load_rules()
            words = {found[iid][0].lower() for iid in todo}
            rules = [r for r in rules if r["find"].strip().lower() not in words]
            for iid, say in todo.items():
                rules.append({"find": found[iid][0], "say": say, "whole_word": True, "match_case": False, "enabled": True})
            try:
                pron.save_rules(rules)
            except OSError as e:
                messagebox.showerror("Audiobook Forge", f"The fixes could not be saved:\n{e}", parent=win)
                return
            for iid in todo:
                tree.set(iid, "fixed", "\u2714")
            self.update_pron_label()
            sug_lbl.config(text=f"Added {len(todo)} fixes.")

        sug_btn.config(command=suggest_all)
        sug_stop.config(command=sug_halt)
        all_btn.config(command=add_all)
        win.protocol("WM_DELETE_WINDOW", lambda: (sug_halt(), win.destroy()))

        def loaded(result):
            if not win.winfo_exists():
                return
            if result is None:
                status.config(text="The voice's dictionary is not available here, so words cannot be checked.")
                return
            if not result:
                status.config(text="Every word in this book is in the voice's dictionary. Nothing to check.")
                return
            status.config(text=f"{len(result)} word{'s' if len(result) != 1 else ''} the voice will guess, most frequent first.")
            for i, (word, n, example) in enumerate(result):
                found[str(i)] = (word, example)
                tree.insert("", "end", iid=str(i), values=(word, f"{n:,}", "", "", example))
            sug_btn.config(state="normal")

        def scan():
            try:
                import namescan
                result = namescan.scan(self.chapters, pron.load_rules(), self.narration_options())
            except Exception:  # noqa: BLE001
                result = None
            self.ui_q.put(lambda: loaded(result))
        threading.Thread(target=scan, daemon=True).start()

        def on_select(_=None):
            sel = tree.selection()
            if not sel:
                return
            word, _ = found[sel[0]]
            state["word"] = sel[0]
            word_lbl.config(text=word)
            say_var.set(suggested.get(word) or word.lower())
            for b in (hear_old, add_btn):
                b.config(state="normal")
            hear_new.config(state="normal")
            say_entry.focus_set()
            say_entry.select_range(0, "end")

        def sample_text(replace):
            word, example = found[state["word"]]
            rules = pron.load_rules()
            if replace:
                rules = rules + [{"find": word, "say": say_var.get().strip() or word, "whole_word": True}]
            return self.as_spoken(example, rules, markup=True).strip()

        def hear(replace):
            if state["word"] is None or state["busy"] or self.running:
                return
            state["busy"] = True
            for b in (hear_old, hear_new):
                b.config(state="disabled")
            status.config(text="Making the sample... the first one can take up to a minute.")

            def done(wav):
                state["busy"] = False
                if not win.winfo_exists():
                    return
                for b in (hear_old, hear_new):
                    b.config(state="normal")
                status.config(text="" if wav else "Could not make a sample.")
                if wav:
                    self.play_wav(wav)
            self.speak_sample(sample_text(replace), done)

        def add():
            if state["word"] is None:
                return
            word, _ = found[state["word"]]
            say = say_var.get().strip()
            if not say or say == word:
                return
            rules = pron.load_rules()
            rules = [r for r in rules if r["find"].strip().lower() != word.lower()]
            rules.append({"find": word, "say": say, "whole_word": True, "match_case": False, "enabled": True})
            try:
                pron.save_rules(rules)
            except OSError as e:
                messagebox.showerror("Audiobook Forge", f"The fix could not be saved:\n{e}", parent=win)
                return
            tree.set(state["word"], "fixed", "\u2714")
            self.update_pron_label()
            status.config(text=f"Added: {word} \u2192 {say}")
            nxt = tree.next(state["word"])
            if nxt:
                tree.selection_set(nxt)
                tree.see(nxt)

        hear_old.config(command=lambda: hear(False))
        hear_new.config(command=lambda: hear(True))
        add_btn.config(command=add)
        say_entry.bind("<Return>", lambda e: add())
        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", lambda e: hear(False))
        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(12, 0))
        ttk.Button(foot, text="Close", command=lambda: (sug_halt(), win.destroy())).pack(side="right")
        ttk.Button(foot, text="Pronunciation fixes...", command=lambda: (sug_halt(), win.destroy(),
                                                                          self.open_pronunciations())).pack(side="right", padx=8)

    # ------------------------------------------------------------- pronunciation fixes
    def update_pron_label(self):
        n = len([r for r in pron.load_rules() if r["enabled"]])
        self.pron_lbl.config(text=f"{n} in use" if n else "none yet")

    def open_pronunciations(self):
        rules = pron.load_rules()
        win = tk.Toplevel(self.root)
        win.title("Pronunciation fixes")
        win.transient(self.root)
        win.geometry(f"{int(820 * self.scale)}x{int(640 * self.scale)}")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="When the narrator says a word wrong, add a fix: the word as it is written, and how it "
                             "should be said (spell it the way it sounds). Fixes apply to every book; the book "
                             "file itself is not changed.", wraplength=int(780 * self.scale),
                  foreground="gray").pack(anchor="w")

        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("on", "find", "say", "word", "case", "found")
        tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse", height=8)
        for col, text, width, anchor, stretch in (("on", "On", 44, "center", False), ("find", "Written as", 200, "w", True),
                                                  ("say", "Say it as", 200, "w", True), ("word", "Whole word", 90, "center", False),
                                                  ("case", "Match case", 90, "center", False),
                                                  ("found", "In this book", 100, "e", False)):
            tree.heading(col, text=text, anchor="w" if anchor == "w" else anchor)
            tree.column(col, width=int(width * self.scale), anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        book_text = ""
        if self.chapters and not self.batch:
            book_text = "\n".join(c.title + "\n" + c.text for c in self.chapters)
        yes, no = "\u2714", ""

        def refill(select=None):
            tree.delete(*tree.get_children())
            counts = pron.count_matches(book_text, rules) if book_text else [None] * len(rules)
            for i, r in enumerate(rules):
                found = "" if counts[i] is None else (f"{counts[i]:,}" if r["enabled"] else "off")
                tree.insert("", "end", iid=str(i), values=(CHECKED if r["enabled"] else UNCHECKED, r["find"], r["say"],
                                                            yes if r["whole_word"] else no, yes if r["match_case"] else no, found))
            if select is not None and str(select) in tree.get_children():
                tree.selection_set(str(select))
                tree.see(str(select))
            try_it()

        form = ttk.LabelFrame(body, text="Add or change a fix", padding=10)
        form.pack(fill="x", pady=(10, 0))
        find_var, say_var = tk.StringVar(), tk.StringVar()
        word_var, case_var = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        r1 = ttk.Frame(form)
        r1.pack(fill="x")
        ttk.Label(r1, text="Written as").pack(side="left")
        find_entry = ttk.Entry(r1, textvariable=find_var, width=22)
        find_entry.pack(side="left", padx=(6, 14))
        ttk.Label(r1, text="Say it as").pack(side="left")
        ttk.Entry(r1, textvariable=say_var, width=22).pack(side="left", padx=(6, 14))
        ttk.Checkbutton(r1, text="Whole word", variable=word_var).pack(side="left")
        ttk.Checkbutton(r1, text="Match case", variable=case_var).pack(side="left", padx=(10, 0))
        r2 = ttk.Frame(form)
        r2.pack(fill="x", pady=(8, 0))

        def selected():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def add():
            f = find_var.get().strip()
            if not f:
                return
            rules.append({"find": f, "say": say_var.get().strip(), "whole_word": word_var.get(),
                          "match_case": case_var.get(), "enabled": True})
            find_var.set("")
            say_var.set("")
            refill(len(rules) - 1)
            find_entry.focus_set()

        def update():
            i = selected()
            if i is None or not find_var.get().strip():
                return
            rules[i].update(find=find_var.get().strip(), say=say_var.get().strip(), whole_word=word_var.get(),
                            match_case=case_var.get())
            refill(i)

        def delete():
            i = selected()
            if i is not None:
                rules.pop(i)
                refill()

        def on_select(_):
            i = selected()
            if i is not None:
                r = rules[i]
                find_var.set(r["find"])
                say_var.set(r["say"])
                word_var.set(r["whole_word"])
                case_var.set(r["match_case"])

        def on_click(event):
            if tree.identify_region(event.x, event.y) == "cell" and tree.identify_column(event.x) == "#1":
                iid = tree.identify_row(event.y)
                if iid:
                    rules[int(iid)]["enabled"] = not rules[int(iid)]["enabled"]
                    refill(int(iid))
                    return "break"

        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Button-1>", on_click)
        find_entry.bind("<Return>", lambda e: add())
        ttk.Button(r2, text="Add", style="Accent.TButton", command=add).pack(side="left")
        ttk.Button(r2, text="Update selected", command=update).pack(side="left", padx=8)
        ttk.Button(r2, text="Delete selected", command=delete).pack(side="left")

        test = ttk.LabelFrame(body, text="Try it", padding=10)
        test.pack(fill="x", pady=(10, 0))
        sample = "Type a sentence that uses the word here."
        for c in (self.chapters if not self.batch else []):
            for par in c.paragraphs:
                if any(pron.count_matches(par, [r])[0] for r in rules if r["enabled"]):
                    m = re.search(r"[^.!?]*[.!?]", par)
                    sample = (m.group(0) if m else par)[:200].strip()
                    break
            else:
                continue
            break
        test_var = tk.StringVar(value=sample)
        trow = ttk.Frame(test)
        trow.pack(fill="x")
        ttk.Entry(trow, textvariable=test_var).pack(side="left", fill="x", expand=True)
        hear = ttk.Button(trow, text="\u25B6 Hear it", command=lambda: hear_it())
        hear.pack(side="left", padx=(8, 0))
        result = ttk.Label(test, text="", foreground="gray", wraplength=int(760 * self.scale))
        result.pack(anchor="w", pady=(6, 0))

        def try_it(*_):
            result.config(text=f"Narrator says: {self.as_spoken(test_var.get(), rules)}")
        test_var.trace_add("write", try_it)

        def hear_it():
            spoken = self.as_spoken(test_var.get(), rules, markup=True).strip()
            if not spoken or self.running:
                return
            wav = PREVIEW_DIR / "_try_it.wav"
            hear.config(state="disabled")
            result.config(text="Making the sample... the first one can take up to a minute.")

            def work():
                try:
                    wav.unlink()
                except OSError:
                    pass
                try:
                    subprocess.run([sys.executable, str(APP_DIR / "preview.py"), self.voice_id(), str(wav), spoken, "--raw"],
                                   cwd=APP_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=CREATE_NO_WINDOW, timeout=600)
                except Exception:  # noqa: BLE001
                    pass
                self.ui_q.put(lambda: done())

            def done():
                if not win.winfo_exists():
                    return
                hear.config(state="normal")
                try_it()
                if wav.exists():
                    self.play_wav(wav)
                else:
                    result.config(text="Could not make a sample.")
            threading.Thread(target=work, daemon=True).start()

        foot = ttk.Frame(body)
        foot.pack(fill="x", pady=(12, 0))

        def export():
            if not rules:
                messagebox.showinfo("Audiobook Forge", "There are no fixes to export yet.", parent=win)
                return
            path = filedialog.asksaveasfilename(parent=win, title="Export pronunciation fixes",
                                                initialfile="Pronunciation fixes.json", defaultextension=".json",
                                                filetypes=[("Pronunciation fixes", "*.json")])
            if not path:
                return
            try:
                pron.export_rules(rules, path)
            except OSError as e:
                messagebox.showerror("Audiobook Forge", f"Could not export:\n{e}", parent=win)
                return
            note.config(text=f"Exported {len(rules)} fix{'es' if len(rules) != 1 else ''} to {Path(path).name}.")

        def import_():
            path = filedialog.askopenfilename(parent=win, title="Import pronunciation fixes",
                                              filetypes=[("Pronunciation fixes", "*.json *.txt *.csv"), ("All files", "*.*")])
            if not path:
                return
            try:
                incoming = pron.read_rules_file(path)
            except OSError as e:
                messagebox.showerror("Audiobook Forge", f"Could not read that file:\n{e}", parent=win)
                return
            if not incoming:
                messagebox.showinfo("Audiobook Forge", "No fixes were found in that file. It should be a file made with "
                                                       "Export, or a list with one fix per line, like:  Thanos = than-ohss",
                                    parent=win)
                return
            merged, added, updated, same = pron.merge_rules(rules, incoming)
            rules[:] = merged
            refill()
            parts = [f"{added} added"] + ([f"{updated} changed"] if updated else []) + ([f"{same} already there"] if same else [])
            note.config(text="Imported: " + ", ".join(parts) + ". Click Save to keep them.")

        ttk.Button(foot, text="Import...", command=import_).pack(side="left")
        ttk.Button(foot, text="Export...", command=export).pack(side="left", padx=(8, 12))
        note = ttk.Label(foot, text="Chapters already converted with different fixes are read again.", foreground="gray")
        note.pack(side="left")

        def save():
            try:
                pron.save_rules(rules)
            except OSError as e:
                messagebox.showerror("Audiobook Forge", f"The fixes could not be saved:\n{e}", parent=win)
                return
            self.update_pron_label()
            win.destroy()

        ttk.Button(foot, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(foot, text="Save", style="Accent.TButton", command=save).pack(side="right", padx=8)
        refill()
        find_entry.focus_set()

    # ------------------------------------------------------------- watch folder
    def pythonw(self):
        exe = Path(sys.executable)
        alt = exe.with_name("pythonw.exe")
        return str(alt if alt.exists() else exe)

    def start_watcher(self):
        if watchmod.read_status():
            return
        try:
            watchmod.STOP_FILE.unlink()
        except OSError:
            pass
        try:
            kwargs = {"creationflags": CREATE_NO_WINDOW | DETACHED} if os.name == "nt" else {"start_new_session": True}
            subprocess.Popen([self.pythonw(), str(APP_DIR / "watcher.py")], cwd=APP_DIR,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        except OSError as e:
            messagebox.showerror("Audiobook Forge", f"The watch folder could not be started:\n{e}")

    def stop_watcher(self):
        try:
            watchmod.STOP_FILE.write_text("stop")
        except OSError:
            pass

    def startup_file(self):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_NAME

    def set_startup(self, on):
        target = self.startup_file()
        if target is None:
            return False
        try:
            if on:
                q = lambda p: str(p).replace('"', '""')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    "' Starts the Audiobook Forge watch folder when Windows starts.\r\n"
                    'Set sh = CreateObject("WScript.Shell")\r\n'
                    f'sh.CurrentDirectory = "{q(APP_DIR)}"\r\n'
                    f'sh.Run """{q(self.pythonw())}"" ""{q(APP_DIR / "watcher.py")}""", 0, False\r\n',
                    encoding="utf-8")
            elif target.exists():
                target.unlink()
            return True
        except OSError:
            return False

    def watch_status_text(self):
        if not self.settings.get("watch_enabled") or not self.settings.get("watch_dir"):
            return ""
        st = watchmod.read_status()
        folder = Path(self.settings["watch_dir"]).name or self.settings["watch_dir"]
        if not st:
            return f"Watch folder \"{folder}\": starting..."
        state = st.get("state")
        if state == "converting":
            waiting = st.get("queued", 0)
            more = f", {waiting} waiting" if waiting else ""
            return f"Watch folder: converting {st.get('current', '')}{more}"
        if state == "waiting":
            return f"Watch folder: waiting for this window to finish ({st.get('queued', 0)} waiting)"
        last = st.get("last") or {}
        tail = f" Last: {last.get('name')} {'done' if last.get('ok') else 'failed'}." if last.get("name") else ""
        return f"Watch folder \"{folder}\" is on and waiting for books.{tail}"

    def update_watch_label(self):
        if not hasattr(self, "watch_lbl"):
            return
        self.watch_lbl.config(text=self.watch_status_text())
        if getattr(self, "watch_dialog_status", None) is not None:
            try:
                if self.watch_dialog_status.winfo_exists():
                    text = self.watch_status_text() or "Off."
                    self.watch_dialog_status.config(text=text)
            except tk.TclError:
                pass

    def option_switches(self, box, options, wrap):
        narration = self.settings.setdefault("narration", {})
        for key, label, help_text, default in options:
            var = tk.BooleanVar(value=narration.get(key, default))

            def changed(k=key, v=var):
                self.settings.setdefault("narration", {})[k] = v.get()
                save_settings(self.settings)
            ttk.Checkbutton(box, text=label, variable=var, style="Switch.TCheckbutton", command=changed).pack(anchor="w", pady=(6, 0))
            ttk.Label(box, text=help_text, foreground="gray", wraplength=wrap).pack(anchor="w", padx=(46, 0))

    def build_quality_settings(self, body, sound_body, speed_body):
        wrap = int(440 * self.scale)
        narration = self.settings.setdefault("narration", {})
        box = ttk.LabelFrame(body, text="How the text is read", padding=12)
        box.pack(fill="x")
        self.option_switches(box, TEXT_OPTIONS, wrap)

        notes_row = ttk.Frame(box)
        notes_row.pack(fill="x", pady=(10, 0))
        ttk.Label(notes_row, text="Footnotes").pack(side="left")
        note_choices = {"skip": "Skip them", "end": "Read them at the end of each chapter"}
        notes_var = tk.StringVar(value=note_choices.get(narration.get("footnotes", "skip"), note_choices["skip"]))
        notes_box = ttk.Combobox(notes_row, textvariable=notes_var, values=list(note_choices.values()), state="readonly", width=34)
        notes_box.pack(side="left", padx=(10, 0))

        def notes_changed(_e=None):
            key = next(k for k, v in note_choices.items() if v == notes_var.get())
            self.settings.setdefault("narration", {})["footnotes"] = key
            save_settings(self.settings)
        notes_box.bind("<<ComboboxSelected>>", notes_changed)
        ttk.Label(box, text="EPUB and Word footnotes. When read, each chapter ends with \"Notes for this chapter\" and "
                            "its notes in order.", foreground="gray", wraplength=wrap).pack(anchor="w", padx=(0, 0), pady=(4, 0))

        box = ttk.LabelFrame(body, text="When quoted speech matches the speaker", padding=12)
        box.pack(fill="x", pady=(12, 0))
        ttk.Label(box, text="Used when \"Voice for quoted speech\" is \"Match the speaker (he/she)\", and for minor "
                            "characters with \"Each character their own voice\". Speech whose speaker is unclear stays with "
                            "the narrator.", foreground="gray", wraplength=wrap).pack(anchor="w")
        grid = ttk.Frame(box)
        grid.pack(fill="x", pady=(8, 0))
        for row, (key, label, choices, default) in enumerate((("dialogue_female", "Women", FEMALE_VOICES, "af_bella"),
                                                              ("dialogue_male", "Men", MALE_VOICES, "am_michael"))):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            var = tk.StringVar(value=VOICES.get(narration.get(key, default), VOICES[default]))
            cb = ttk.Combobox(grid, textvariable=var, values=[VOICES[v] for v in choices], state="readonly", width=28)
            cb.grid(row=row, column=1, sticky="w", pady=3)

            def picked(_e=None, k=key, v=var):
                vid = next((x for x, lab in VOICES.items() if lab == v.get()), None)
                if vid:
                    self.settings.setdefault("narration", {})[k] = vid
                    save_settings(self.settings)
            cb.bind("<<ComboboxSelected>>", picked)

        box = ttk.LabelFrame(sound_body, text="How it sounds", padding=12)
        box.pack(fill="x")
        self.option_switches(box, SOUND_OPTIONS, wrap)
        box = ttk.LabelFrame(sound_body, text="Work files", padding=12)
        box.pack(fill="x", pady=(12, 0))
        keep_var = tk.BooleanVar(value=self.settings.get("keep_work", False))
        ttk.Checkbutton(box, text="Keep paragraph audio after finishing", variable=keep_var, style="Switch.TCheckbutton",
                        command=lambda: self.set_setting("keep_work", keep_var.get())).pack(anchor="w")
        ttk.Label(box, text="Then converting the same book again after a pronunciation fix only re-reads the paragraphs "
                            "that changed. Uses about 170 MB of disk per hour of audio, in the work folder.",
                  foreground="gray", wraplength=wrap).pack(anchor="w", padx=(46, 0))
        crow = ttk.Frame(box)
        crow.pack(anchor="w", pady=(10, 0))
        ttk.Button(crow, text="Clean up work files...", command=lambda: self.cleanup_work(box.winfo_toplevel())).pack(side="left")
        work_lbl = ttk.Label(crow, text="", foreground="gray")
        work_lbl.pack(side="left", padx=(10, 0))

        def sized():
            total = sum(b["size"] for b in self.work_folders())
            self.ui_q.put(lambda: work_lbl.winfo_exists() and work_lbl.config(text=f"{total / 1e9:.1f} GB in use"))
        threading.Thread(target=sized, daemon=True).start()

        box = ttk.LabelFrame(speed_body, text="Speed", padding=12)
        box.pack(fill="x")
        self.option_switches(box, SPEED_OPTIONS, wrap)
        crow = ttk.Frame(box)
        crow.pack(anchor="w", pady=(12, 0))
        ttk.Button(crow, text="Compare by ear", command=lambda: self.compare_half(cmp_lbl)).pack(side="left")
        cmp_lbl = ttk.Label(crow, text="Reads the same passage of the loaded book with full and then half precision.",
                            foreground="gray", wraplength=int(wrap * 0.75))
        cmp_lbl.pack(side="left", padx=(10, 0))
        measured = self.settings.get("measured_x")
        ttk.Label(speed_body, text=(f"Your last conversions ran at about {measured:.0f}x realtime. " if measured else "") +
                  "Each option turns itself off for the rest of a conversion if it ever fails. "
                  "bench3.py in the Audiobook-Forge folder measures them on this PC.",
                  foreground="gray", wraplength=wrap).pack(anchor="w", pady=(12, 0))
        for b in (body, sound_body, speed_body):
            ttk.Label(b, text="Changes apply to the next conversion. Only paragraphs affected by a change are read again.",
                      foreground="gray", wraplength=wrap).pack(anchor="w", pady=(12, 0))

    def build_output_settings(self, body):
        wrap = int(440 * self.scale)
        output = self.settings.setdefault("output", {})

        def save(key, value):
            self.settings.setdefault("output", {})[key] = value
            save_settings(self.settings)
            self.update_summary()
        box = ttk.LabelFrame(body, text="File type", padding=12)
        box.pack(fill="x")
        fmt_var = tk.StringVar(value=output.get("format", "m4b"))
        for key, (label, help_text) in FORMATS.items():
            ttk.Radiobutton(box, text=label, value=key, variable=fmt_var,
                            command=lambda: save("format", fmt_var.get())).pack(anchor="w", pady=(6, 0))
            ttk.Label(box, text=help_text, foreground="gray", wraplength=wrap).pack(anchor="w", padx=(28, 0))
        box = ttk.LabelFrame(body, text="Chapter art", padding=12)
        box.pack(fill="x", pady=(12, 0))
        art_var = tk.BooleanVar(value=bool(output.get("chapter_art", False)))
        ttk.Checkbutton(box, text="A title card picture for every chapter", variable=art_var, style="Switch.TCheckbutton",
                        command=lambda: save("chapter_art", art_var.get())).pack(anchor="w")
        ttk.Label(box, text="Each card shows the chapter's number and title over your book's cover. MP3 only: in a folder "
                            "of MP3s every file gets its card as its picture; in a single MP3 the cards sit inside the "
                            "chapter marks, for players that show chapter pictures. M4B files cannot carry them. Many "
                            "players ignore chapter pictures, so check yours (the chapter viewer shows what a card looks "
                            "like).", foreground="gray", wraplength=wrap).pack(anchor="w", padx=(46, 0), pady=(2, 0))
        box = ttk.LabelFrame(body, text="Sound quality", padding=12)
        box.pack(fill="x", pady=(12, 0))
        q_var = tk.StringVar(value=output.get("quality", "standard"))
        for key, (kbps, label) in QUALITIES.items():
            mb_hour = kbps * 1000 / 8 * 3600 / 1e6
            ttk.Radiobutton(box, text=f"{label}: {kbps} kbps, about {mb_hour:.0f} MB per hour", value=key, variable=q_var,
                            command=lambda: save("quality", q_var.get())).pack(anchor="w", pady=(4, 0))
        ttk.Label(box, text="Speech sounds clear at Standard. Higher settings mostly make the file bigger.",
                  foreground="gray", wraplength=wrap).pack(anchor="w", pady=(8, 0))
        ttk.Label(body, text="Changing these only changes the final file. Nothing is read again.",
                  foreground="gray", wraplength=wrap).pack(anchor="w", pady=(12, 0))

    def build_watch_settings(self, body, first=False):
        box = ttk.LabelFrame(body, text="Watch folder", padding=12)
        box.pack(fill="x", pady=(0 if first else 12, 0))
        ttk.Label(box, text="Books saved or copied into this folder are converted on their own, one at a time, even with "
                            "this window closed. Finished books move to a 'done' folder inside it.",
                  foreground="gray", wraplength=int(440 * self.scale)).pack(anchor="w")
        frow = ttk.Frame(box)
        frow.pack(fill="x", pady=(8, 0))
        dir_var = tk.StringVar(value=self.settings.get("watch_dir", ""))
        ttk.Entry(frow, textvariable=dir_var, state="readonly").pack(side="left", fill="x", expand=True)
        enabled_var = tk.BooleanVar(value=bool(self.settings.get("watch_enabled")))
        startup_var = tk.BooleanVar(value=bool(self.startup_file() and self.startup_file().exists()))

        def choose():
            path = filedialog.askdirectory(title="Folder to watch for new books", initialdir=dir_var.get() or str(Path.home()))
            if path:
                dir_var.set(str(Path(path)))
                self.set_setting("watch_dir", str(Path(path)))
                if enabled_var.get():
                    self.stop_watcher()
                    self.root.after(6000, self.start_watcher)

        def toggle():
            if enabled_var.get() and not dir_var.get():
                choose()
                if not dir_var.get():
                    enabled_var.set(False)
                    return
            self.set_setting("watch_enabled", enabled_var.get())
            if enabled_var.get():
                self.start_watcher()
            else:
                self.stop_watcher()
            self.update_watch_label()

        def toggle_startup():
            if not self.set_startup(startup_var.get()):
                startup_var.set(False)
                messagebox.showinfo("Audiobook Forge", "Starting with Windows could not be set up on this computer.")
            self.set_setting("watch_startup", startup_var.get())

        def open_folder():
            if dir_var.get():
                try:
                    os.startfile(dir_var.get())
                except Exception:  # noqa: BLE001
                    pass

        ttk.Button(frow, text="Choose...", command=choose).pack(side="left", padx=(8, 0))
        ttk.Button(frow, text="Open", command=open_folder).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(box, text="Convert new books in this folder automatically", variable=enabled_var,
                        style="Switch.TCheckbutton", command=toggle).pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(box, text="Start watching when Windows starts", variable=startup_var,
                        style="Switch.TCheckbutton", command=toggle_startup).pack(anchor="w", pady=(6, 0))
        self.watch_dialog_status = ttk.Label(box, text=self.watch_status_text() or "Off.", foreground="gray",
                                             wraplength=int(440 * self.scale))
        self.watch_dialog_status.pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------- closing
    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Audiobook Forge", "A conversion is running. Stop it and close?"):
                return
            self.cancel()
        self.close_now()

    def close_now(self):
        for flag in (PAUSE_FILE, GUI_BUSY):
            try:
                flag.unlink()
            except OSError:
                pass
        self.persist()
        self.settings["geometry"] = self.root.geometry()
        save_settings(self.settings)
        self.root.destroy()


def main():
    if tk is None:
        sys.exit("tkinter, tkinterdnd2, sv-ttk or pillow is not available in this Python.")
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:  # noqa: BLE001
        pass
    root = TkinterDnD.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
