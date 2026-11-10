"""Audiobook Forge: book -> chaptered M4B audiobook, using Kokoro on the GPU.

Examples (run with runtime\\python\\python.exe):
    python convert.py "book.epub"                 # whole book (also .txt .md .docx .html .pdf)
    python convert.py "book.epub" --chapters 32   # just chapter 32 (quick test)
    python convert.py "book.epub" --chapters 1-3  # a range
Progress is saved per paragraph, so a stopped run resumes where it left off, and after a change
(a pronunciation fix, say) only the paragraphs that changed are spoken again.
Narration options (text clean-up, pauses, loudness, dialogue voices, speed modes) come from
settings.json, which the window writes; --options '{"...": ...}' overrides them.
"""
import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HOME", str(HERE / "models"))  # voice models live in the app folder
warnings.filterwarnings("ignore")

import numpy as np
import soundfile as sf

import checks
import normalize
import speakers
import voicemix
from books import BookError, read_book, read_book_cover
from epub_reader import safe_name
from pronunciations import RULES_FILE, apply_rules, compile_rules, load_rules, rules_hash

SAMPLE_RATE = 24000
# ffmpeg: the copy in the app's bin folder (installed builds) or the one on the PATH
if os.name == "nt":
    # A program that cannot start (a missing DLL, say) must fail at once with an error code, not sit behind a
    # hidden "cannot continue" dialog that nobody sees. Child processes (ffmpeg) inherit this.
    try:
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:  # noqa: BLE001
        pass
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
FFMPEG_NOTE = ""


def _ffmpeg_works(path):
    try:
        r = subprocess.run([path, "-hide_banner", "-version"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return r.returncode == 0 and b"ffmpeg version" in r.stdout[:300]
    except Exception:  # noqa: BLE001
        return False


def find_ffmpeg():
    """The ffmpeg in the app's bin folder if it runs (an incomplete copy is skipped), else the one on the PATH."""
    global FFMPEG_NOTE
    bundled = HERE / "bin" / "ffmpeg.exe"
    if bundled.exists():
        if _ffmpeg_works(str(bundled)):
            return str(bundled)
        FFMPEG_NOTE = ("The copy of ffmpeg in the bin folder does not start (usually its DLL files are missing), so the "
                       "one installed on this PC is used instead.")
    return "ffmpeg"


FFMPEG = find_ffmpeg()
PAUSE_LIMIT = 6 * 3600    # a forgotten pause never blocks a conversion for more than this
PLAN_VERSION = "3"        # part of the cache fingerprint: bump when the way chapters are spoken changes
PIECE_VERSION = "1"       # part of each paragraph's cache key
FFMPEG_STALL = int(os.environ.get("FORGE_FFMPEG_STALL", "300"))       # ffmpeg making no progress for this long has hung (a hidden dialog, a dead disk...)
WORKER_STALL = 20 * 60    # the second converter silent this long has hung
OUTPUTS_FILE = HERE / "logs" / "outputs.json"   # finished audiobook -> the book file it was made from
BOOK_LUFS = -19.0         # whole-book loudness (audiobook stores ask for about -18 to -23 dB RMS, peaks under -3 dB)
LETTERS_PER_SEC = 12.0    # typical Kokoro pace at speed 1.0, for the quality self-check
DEFAULT_OPTIONS = {
    "normalize": True,       # numbers, dates, money, abbreviations...
    "clutter": True,         # footnote markers, links, captions
    "heteronyms": True,      # lead (metal), bass (music)...
    "natural_pauses": True,  # pause length depends on what just ended
    "trim": True,            # remove the model's stray silences and clicks
    "loudness": True,        # every chapter and book at the same volume
    "shouting": True,        # ALL-CAPS words read as words, acronyms spelled
    "self_check": True,      # re-speak paragraphs that came out much too short or too long
    "book_loudness": True,   # whole book matched to the audiobook loudness standard (two passes)
    "room_tone": False,      # faint natural background instead of digital silence
    "footnotes": "skip",     # "skip" or "end": read the chapter's footnotes after it
    "fast": False,           # overlap text work (CPU) with speech (GPU)
    "half": False,           # half-precision math on the GPU
    "parallel": False,       # two chapters at once (two processes)
    "dialogue_voice": "",    # quoted speech: "" narrator, a voice, "auto" (women/men) or "cast" (per character)
    "dialogue_female": "af_bella",   # with "auto": voice for women's speech
    "dialogue_male": "am_michael",   # with "auto": voice for men's speech
    "format": "m4b",         # m4b, mp3 (one file with chapters) or mp3_chapters (a folder, one file per chapter)
    "chapter_art": False,    # a title card picture for every chapter (MP3 formats only)
    "keep_awake": True,      # stop Windows from going to sleep while converting
    "ai_speakers": True,     # use the language model's speaker tags (casting\\<book>.speakers.json) when there are some
    "quality": "standard",   # small / standard / high / best (see QUALITY_KBPS)
}
QUALITY_KBPS = {"small": 48, "standard": 64, "high": 96, "best": 128}
NOT_SHAPING = ("fast", "parallel", "book_loudness", "format", "quality", "chapter_art", "keep_awake", "ai_speakers")   # options that do not change a chapter's audio
SCENE_BREAK_RE = re.compile(r"^[\s*•·~#=_–—\-⁂◆♦●○✧✦§]{1,20}$")
QUOTE_RE = re.compile(r"(“[^“”]*”|\"[^\"]*\")")


def load_options(override=None):
    opts = dict(DEFAULT_OPTIONS)
    try:
        settings = json.loads((HERE / "settings.json").read_text(encoding="utf-8"))
        opts.update({k: v for k, v in (settings.get("narration") or {}).items() if k in DEFAULT_OPTIONS})
        if settings.get("dialogue_voice") is not None:
            opts["dialogue_voice"] = settings.get("dialogue_voice") or ""
        opts.update({k: v for k, v in (settings.get("output") or {}).items() if k in ("format", "quality", "chapter_art")})
        if "keep_awake" in settings:
            opts["keep_awake"] = bool(settings["keep_awake"])
    except Exception:  # noqa: BLE001
        pass
    if override:
        opts.update({k: v for k, v in override.items() if k in DEFAULT_OPTIONS})
    return opts


def fingerprint(voice, speed, opts, rules):
    """Cached chapter audio is reused only while everything that shapes the sound stays the same."""
    shaping = {k: v for k, v in opts.items() if k not in NOT_SHAPING}
    if opts.get("dialogue_voice") not in ("auto", "cast"):
        shaping.pop("dialogue_female", None)
        shaping.pop("dialogue_male", None)
    mixes = sorted(set(re.findall(r"custom:[^,\"]+", json.dumps([voice, shaping], ensure_ascii=False))))
    key = json.dumps([voice, f"{speed:g}", shaping, rules_hash(rules), normalize.VERSION, PLAN_VERSION,
                      [voicemix.tag(m) for m in mixes]], sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def parse_range(spec, total):
    if not spec:
        return list(range(1, total + 1))
    picked = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            picked += range(int(a), int(b) + 1)
        else:
            picked.append(int(part))
    return [n for n in picked if 1 <= n <= total]


_out_lock = threading.Lock()
_log = {"file": None}
LOG_DIR = HERE / "logs"
CASTING_DIR = HERE / "casting"
KEEP_LOGS = 20


def start_log(book_stem):
    """Write this run's messages to logs/<date time> <book>.txt too (for Save diagnostics). Keeps the newest 20."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        old = sorted(LOG_DIR.glob("*.txt"))
        for f in old[:max(0, len(old) - (KEEP_LOGS - 1))]:
            f.unlink()
        path = LOG_DIR / f"{time.strftime('%Y-%m-%d %H-%M-%S')} {safe_name(book_stem)[:40]}.txt"
        _log["file"] = open(path, "a", encoding="utf-8")
        _log["file"].write(f"command: {' '.join(sys.argv[1:])}\n")
        sys.stdout.write(f"@log {path}\n")
    except OSError:
        _log["file"] = None


def out(msg):
    """Print one whole line at once (the second chapter process's lines are relayed through here too)."""
    with _out_lock:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
        f = _log["file"]
        if f and not msg.startswith("@progress"):
            try:
                f.write(time.strftime("%H:%M:%S ") + msg + "\n")
                f.flush()
            except OSError:
                pass


def to_numpy(audio):
    return audio.cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio, dtype=np.float32)


def silence(seconds):
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


_awake = {"on": False}


def stay_awake(on):
    """Ask Windows not to sleep while this process works (the screen may still turn off). Windows forgets the
    request by itself when the process ends, so a crash can never leave the PC unable to sleep."""
    _awake["on"] = on
    if os.name != "nt":
        return
    try:
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:  # noqa: BLE001
        pass


def wait_while_paused(pause_file):
    """Hold here while the pause flag file exists. The window creates and removes the file."""
    if not pause_file or not os.path.exists(pause_file):
        return
    out("@paused 1")
    was_awake = _awake["on"]
    if was_awake:
        stay_awake(False)            # a paused conversion should not keep the PC up all night
    started = time.time()
    while os.path.exists(pause_file) and time.time() - started < PAUSE_LIMIT:
        time.sleep(0.4)
    if was_awake:
        stay_awake(True)
    out("@paused 0")


# --------------------------------------------------------------------------------------- sound shaping
def tidy(audio, keep=0.05, fade=0.008):
    """Cut the silence the model leaves before and after speech, keeping a short margin, and fade the
    edges so joins never click."""
    if not len(audio):
        return audio
    peak = float(np.abs(audio).max())
    if peak <= 0:
        return audio[:0]
    loud = np.where(np.abs(audio) > max(0.004, 0.015 * peak))[0]
    start = max(0, loud[0] - int(keep * SAMPLE_RATE))
    end = min(len(audio), loud[-1] + int(keep * SAMPLE_RATE))
    out = audio[start:end].astype(np.float32, copy=True)
    f = min(int(fade * SAMPLE_RATE), len(out) // 2)
    if f > 0:
        ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)
        out[:f] *= ramp
        out[-f:] *= ramp[::-1]
    return out


def even_loudness(audio, target_db=-20.0, peak_db=-1.0):
    """Bring speech to a steady level: the loudness of the spoken parts (quiet gaps ignored) is set to
    target_db, without letting the loudest peak go above peak_db."""
    frame = int(0.05 * SAMPLE_RATE)
    n = len(audio) // frame
    if n < 5:
        return audio
    frames = audio[:n * frame].reshape(n, frame)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1) + 1e-12)
    voiced = rms[20 * np.log10(rms) > -45]
    if len(voiced) < 5:
        return audio
    level = 20 * np.log10(np.sqrt((voiced ** 2).mean()))
    peak = float(np.abs(audio).max())
    gain = target_db - level
    gain = min(gain, peak_db - 20 * np.log10(peak + 1e-12))
    gain = max(-24.0, min(24.0, gain))
    return (audio * (10 ** (gain / 20))).astype(np.float32)


def add_room_tone(audio, level_db=-66.0, seed=0):
    """A faint, soft background (like a quiet recording booth) under the whole chapter, so pauses are
    not dead digital silence. Kept below -60 dB, the noise floor audiobook stores allow."""
    if not len(audio):
        return audio
    rng = np.random.default_rng(seed)
    tone = np.convolve(rng.standard_normal(len(audio)).astype(np.float32), np.ones(6, dtype=np.float32) / 6, "same")
    tone *= (10 ** (level_db / 20)) / (float(np.sqrt((tone ** 2).mean())) + 1e-12)
    f = min(len(tone) // 2, int(0.2 * SAMPLE_RATE))
    if f:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        tone[:f] *= ramp
        tone[-f:] *= ramp[::-1]
    return (audio + tone).astype(np.float32)


def expected_seconds(text, speed):
    letters = len(re.sub(r"[^A-Za-z0-9]", "", normalize.strip_markup(text)))
    return letters, letters / (LETTERS_PER_SEC * max(0.3, speed))


SENTENCE_RE = re.compile(r"[^.!?\u2026]+(?:[.!?\u2026]+[\"\u201d\u2019)]*|$)")


def sentences_of(text):
    parts = [m.group(0).strip() for m in SENTENCE_RE.finditer(text) if m.group(0).strip()]
    if len(parts) < 2:
        parts = [p.strip() for p in re.split(r"(?<=[,;:\u2014])\s+", text) if p.strip()]
    return parts


# --------------------------------------------------------------------------------------- planning
def split_dialogue(text):
    """[(text, is_dialogue)]. Quotes of three words or fewer ("so-called" quotes, titles) stay with the narrator."""
    s = text.strip()
    if s[:1] in ("“", '"') and s.count(s[0]) == 1 and "”" not in s:
        return [(s, True)]                      # speech running on to the next paragraph has no closing quote
    parts, pos = [], 0
    for m in QUOTE_RE.finditer(s):
        short = len(m.group(0).split()) <= 3
        spoken_line = m.start() == 0 and re.search(r"[,.!?\u2026\u2014][\"\u201d]$", m.group(0))
        if short and not spoken_line:
            continue                            # "so-called" quotes and titles stay with the narrator
        before = s[pos:m.start()].strip()
        if before:
            parts.append((before, False))
        parts.append((m.group(0), True))
        pos = m.end()
    rest = s[pos:].strip()
    if rest:
        parts.append((rest, False))
    return parts or [(s, False)]


def plan_chapter(chapter, voice, opts, rules, guesser=None, only=None):
    """The chapter as items to say, each [(text, voice), ...] followed by a pause in seconds.
    Returns (items, lead_in, tail): the silences at the very start and end of the chapter.
    With dialogue_voice "auto", quotes are read by a female or male voice, guessed per paragraph.
    only: paragraph numbers to keep (for samples); the title and footnotes are then left out, but
    speakers are still worked out from the whole chapter."""
    natural = opts["natural_pauses"]
    dialogue_voice = opts.get("dialogue_voice") or ""
    by_speaker = dialogue_voice == "auto"
    by_cast = dialogue_voice == "cast"
    genders, who = [], []
    if by_speaker:
        guesser = guesser or speakers.SpeakerGuesser(chapter.paragraphs)
        genders = guesser.genders(chapter.paragraphs)
    if by_cast:
        guesser = guesser or speakers.SpeakerGuesser(chapter.paragraphs)
        who = guesser.speakers(chapter.paragraphs)
    cast_voices = opts.get("cast") or {}
    gender_voice = {"f": opts.get("dialogue_female") or "af_bella", "m": opts.get("dialogue_male") or "am_michael"}

    def speakable(t):
        return normalize.prepare(t, {k: opts[k] for k in ("clutter", "normalize", "heteronyms", "shouting")},
                                 (lambda x: apply_rules(x, rules)) if rules else None)

    items = [([(speakable(chapter.title) + ".", voice)], 1.0 if natural else 0.8)] if only is None else []
    for n_par, p in enumerate(chapter.paragraphs):
        if only is not None and n_par not in only:
            continue
        if SCENE_BREAK_RE.match(p):
            if items:
                items[-1] = (items[-1][0], max(items[-1][1], 1.6 if natural else 0.8))
            continue
        if opts["clutter"] and normalize.is_caption(p):
            continue
        text = speakable(p)
        if not re.search(r"[A-Za-z0-9]", text):
            continue
        quote_voice = dialogue_voice
        if by_speaker:
            quote_voice = gender_voice.get(genders[n_par]) if n_par < len(genders) else None
        elif by_cast:
            name, g = who[n_par] if n_par < len(who) else (None, None)
            quote_voice = cast_voices.get(name) or gender_voice.get(g)
        if quote_voice and quote_voice != voice:
            pieces = [(t, quote_voice if is_q else voice) for t, is_q in split_dialogue(text)]
            merged = []                          # neighbours with the same voice are spoken in one go
            for t, v in pieces:
                if merged and merged[-1][1] == v:
                    merged[-1] = (merged[-1][0] + " " + t, v)
                else:
                    merged.append((t, v))
            pieces = merged
        else:
            pieces = [(text, voice)]
        if not natural:
            pause = 0.35
        elif len(p.split()) <= 8 and not re.search(r"[.!?”\"':;,]$", p.strip()):
            pause = 0.75                         # a heading or a short line inside the chapter
        elif p.strip().endswith(("”", '"')):
            pause = 0.4                          # dialogue keeps moving
        else:
            pause = 0.55
        items.append((pieces, pause))
    notes = getattr(chapter, "notes", None) or []
    if opts.get("footnotes") == "end" and notes and only is None:
        items[-1] = (items[-1][0], max(items[-1][1], 1.2 if natural else 0.8))
        items.append(([("Notes for this chapter.", voice)], 0.8 if natural else 0.5))
        for n, note in enumerate(notes, 1):
            text = speakable(f"Note {n}. {note}")
            if re.search(r"[A-Za-z0-9]", text):
                items.append(([(text, voice)], 0.7 if natural else 0.35))
    return items, (0.3 if natural else 0.0), (1.2 if natural else 0.0)


# --------------------------------------------------------------------------------------- speaking
class HalfPrecisionFailed(RuntimeError):
    pass


class Speaker:
    """Turns text into audio. Faster mode prepares upcoming text (CPU) while the GPU speaks; half
    precision runs the GPU math in 16-bit floats."""

    def __init__(self, pipe, speed, fast=False, half=False):
        self.pipe, self.speed, self.fast, self.half = pipe, speed, fast, half
        self.packs = {}

    def _pack(self, voice):
        if voice not in self.packs:
            self.packs[voice] = self.pipe.load_voice(voicemix.resolve(voice)).to(self.pipe.model.device)
        return self.packs[voice]

    def precision(self):
        """Context for GPU work: autocast to float16 when half precision is on, else nothing."""
        if not self.half:
            return contextlib.nullcontext()
        import torch
        return torch.autocast(device_type="cuda", dtype=torch.float16)

    def _checked(self, chunks):
        if self.half:
            for c in chunks:
                if not np.all(np.isfinite(c)):
                    raise HalfPrecisionFailed("half precision produced invalid audio")
        return chunks

    def _phonemes(self, text):
        _, tokens = self.pipe.g2p(text)
        out_ = []
        for _, ps, _ in self.pipe.en_tokenize(tokens):
            if ps:
                out_.append(ps[:510])
        return out_

    def chunks_normal(self, text, voice):
        with self.precision():
            chunks = [to_numpy(a).astype(np.float32) for _, _, a in self.pipe(text, voice=voicemix.resolve(voice), speed=self.speed)
                      if a is not None]
        return self._checked(chunks)

    def run(self, items, pause_file):
        """Yield (k, spoken) for each item: spoken is [(voice, [chunks])] per piece, in order."""
        if not self.fast:
            for k, (pieces, _pause) in enumerate(items, 1):
                wait_while_paused(pause_file)
                yield k, [(voice, self.chunks_normal(text, voice)) for text, voice in pieces]
            return
        from kokoro import KPipeline
        jobs = queue.Queue(maxsize=24)          # text work runs up to 24 paragraphs ahead of the GPU
        stop = threading.Event()

        def worker():
            try:
                for k, (pieces, _pause) in enumerate(items, 1):
                    prepared = [(voice, self._phonemes(text)) for text, voice in pieces]
                    while not stop.is_set():
                        try:
                            jobs.put((k, prepared), timeout=0.5)
                            break
                        except queue.Full:
                            continue
                    if stop.is_set():
                        return
            except Exception as e:  # noqa: BLE001
                jobs.put(e)
                return
            jobs.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            while True:
                wait_while_paused(pause_file)
                job = jobs.get()
                if job is None:
                    break
                if isinstance(job, Exception):
                    raise job
                k, prepared = job
                spoken = []
                for voice, ps_list in prepared:
                    pack = self._pack(voice)
                    chunks = []
                    with self.precision():
                        for ps in ps_list:
                            result = KPipeline.infer(self.pipe.model, ps, pack, self.speed)
                            if result is not None and result.audio is not None:
                                chunks.append(to_numpy(result.audio).astype(np.float32))
                    spoken.append((voice, self._checked(chunks)))
                yield k, spoken
        finally:
            stop.set()


def join_chunks(chunks, trim):
    parts = []
    for i, chunk in enumerate(chunks):
        if trim:
            chunk = tidy(chunk)
            if i:
                parts.append(silence(0.15))     # where a long paragraph was split
        parts.append(chunk)
    return np.concatenate(parts) if parts else silence(0.0)


def self_check(speaker, text, voice, audio, trim):
    """Kokoro now and then swallows the end of a paragraph or rambles on. When a piece is far shorter
    or longer than its text should take, say it again sentence by sentence and keep whichever
    version's length fits the text better. Returns (audio, status): "ok", "fixed" (re-read), or "suspect"
    (still far from the expected length; listed in the listening report)."""
    letters, expect = expected_seconds(text, speaker.speed)
    got = len(audio) / SAMPLE_RATE
    if letters < 40 or 0.45 * expect <= got <= 2.5 * expect:
        return audio, "ok"
    parts = sentences_of(text)
    if len(parts) < 2:
        return audio, "suspect"
    redo = []
    for i, part in enumerate(parts):
        if i:
            redo.append(silence(0.2 if trim else 0.0))
        redo.append(join_chunks(speaker.chunks_normal(part, voice), trim))
    redo = np.concatenate(redo)
    new = len(redo) / SAMPLE_RATE
    if abs(np.log(max(new, 0.05) / expect)) < abs(np.log(max(got, 0.05) / expect)):
        return redo, "fixed"
    return audio, "suspect"


def long_silences(audio, min_seconds=1.3):
    """Start times (s) of silences longer than min_seconds inside a stretch of speech (the voice stalling)."""
    if len(audio) < SAMPLE_RATE * min_seconds * 2:
        return []
    frame = SAMPLE_RATE // 50
    n = len(audio) // frame
    level = np.abs(audio[:n * frame]).reshape(n, frame).max(axis=1)
    quiet = level < max(0.004, 0.015 * float(level.max() if n else 0))
    found, start = [], None
    for i, q in enumerate(np.append(quiet, False)):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * 0.02 >= min_seconds and start > 0 and i < n:     # inside the speech, not at its ends
                found.append(round(start * 0.02, 2))
            start = None
    return found


WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*[A-Za-z]")


def unfamiliar_words(text, cache):
    """Words in the text that Kokoro's dictionary does not know (it guesses how to say them).
    cache: dict word -> verdict, shared within a chapter. [] when the dictionary is unavailable."""
    found = []
    for m in WORD_RE.finditer(normalize.MARKUP_RE.sub(" ", text)):
        word = re.sub(r"['\u2019]s$|['\u2019]$", "", m.group(0))
        for part in word.split("-"):
            if len(part) < 3 or part.isupper() and len(part) <= 3:
                continue
            key = part.lower()
            if key not in cache:
                known = normalize.known_word(part)
                cache[key] = None if known is None else (not known)
            if cache[key]:
                found.append(part if not part.isupper() else part.capitalize())
    return found


def snippet(text, limit=90):
    t = normalize.strip_markup(text).strip()
    return t if len(t) <= limit else t[:limit - 3].rsplit(" ", 1)[0] + "..."


def piece_key(pieces, speaker, opts):
    pieces = [(t, voicemix.tag(v)) for t, v in pieces]          # a changed mix is spoken again
    key = json.dumps([pieces, f"{speaker.speed:g}", bool(opts["trim"]), bool(speaker.half), bool(opts["self_check"]),
                      PIECE_VERSION], ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def load_piece(path):
    try:
        return np.load(path).astype(np.float32) / 32767.0
    except Exception:  # noqa: BLE001  (a half-written file from a crash is simply spoken again)
        return None


def save_piece(path, audio):
    tmp = path.with_name(path.stem + ".partial.npy")
    np.save(tmp, (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16))
    os.replace(tmp, path)


def synth_chapter(speaker, chapter, voice, idx, opts, rules, pause_file=None, pieces_dir=None, used=None,
                  guesser=None, report=None):
    """The chapter's audio and how many paragraphs the self-check re-spoke. Each paragraph's audio is
    kept in pieces_dir as soon as it is made, so only new or changed paragraphs are ever spoken.
    report: a dict that receives what is worth listening to (see write_report)."""
    items, lead_in, tail = plan_chapter(chapter, voice, opts, rules, guesser)
    total = len(items)
    keys = [piece_key(pieces, speaker, opts) for pieces, _ in items]
    if used is not None:
        used.update(keys)
    audio_of, notes_of = {}, {}
    if pieces_dir is not None:
        for i, key in enumerate(keys):
            f = pieces_dir / f"{key}.npy"
            if f.exists():
                a = load_piece(f)
                if a is not None:
                    audio_of[i] = a
                    try:
                        notes_of[i] = json.loads((pieces_dir / f"{key}.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
    todo = [i for i in range(total) if i not in audio_of]
    done = total - len(todo)
    out(f"@progress {idx} {done} {total}")  # read by the window, not shown to people
    fixed = 0
    trim = opts["trim"]
    for k, spoken in speaker.run([items[i] for i in todo], pause_file):
        i = todo[k - 1]
        pieces = items[i][0]
        parts, statuses = [], []
        for n, (v, chunks) in enumerate(spoken):
            if n:
                parts.append(silence(0.12 if trim else 0.0))          # between narration and a quote
            a = join_chunks(chunks, trim)
            if opts["self_check"]:
                a, status = self_check(speaker, pieces[n][0], v, a, trim)
                fixed += status == "fixed"
                statuses.append(status)
            parts.append(a)
        audio_of[i] = np.concatenate(parts) if parts else silence(0.0)
        note = {}
        if "fixed" in statuses:
            note["fixed"] = True
        if "suspect" in statuses:
            note["suspect"] = True
        stalls = long_silences(audio_of[i])
        if stalls:
            note["silences"] = stalls
        if note:
            notes_of[i] = note
        if pieces_dir is not None:
            save_piece(pieces_dir / f"{keys[i]}.npy", audio_of[i])
            if note:
                (pieces_dir / f"{keys[i]}.json").write_text(json.dumps(note), encoding="utf-8")
        done += 1
        out(f"@progress {idx} {done} {total}")
    seq = [silence(lead_in)] if lead_in else []
    at = lead_in
    events, names, unknown, placed = [], {}, {}, []
    for _pattern, say in (rules or []):          # respellings from pronunciation fixes are not "unfamiliar"
        for w in WORD_RE.findall(str(say)):
            for part in w.split("-"):
                unknown[part.lower()] = False
    for i, (pieces, pause) in enumerate(items):
        seq.append(audio_of[i])
        seq.append(silence(pause))
        if report is not None:
            text = " ".join(t for t, _ in pieces)
            placed.append({"at": round(at, 3), "dur": round(len(audio_of[i]) / SAMPLE_RATE, 3),
                           "said": normalize.strip_markup(text)})
            note = notes_of.get(i, {})
            if note.get("fixed"):
                events.append({"at": round(at, 2), "kind": "fixed", "text": snippet(text)})
            elif note.get("suspect"):
                events.append({"at": round(at, 2), "kind": "suspect", "text": snippet(text)})
            for s_at in note.get("silences", []):
                events.append({"at": round(at + s_at, 2), "kind": "silence", "text": snippet(text)})
            for word in unfamiliar_words(text, unknown):
                count, first = names.get(word, (0, at))
                names[word] = (count + 1, first)
        at += len(audio_of[i]) / SAMPLE_RATE + pause
    if tail:
        seq.append(silence(tail))
    audio = np.concatenate(seq) if seq else silence(0.5)
    if opts["loudness"]:
        audio = even_loudness(audio)
    if opts["room_tone"]:
        audio = add_room_tone(audio, seed=idx)
    if report is not None:
        report.update(chapter=idx, title=chapter.title, duration=round(len(audio) / SAMPLE_RATE, 3), events=events,
                      names={w: [n, round(first, 2)] for w, (n, first) in names.items()}, items=placed)
    return audio, fixed, total - len(todo)


# --------------------------------------------------------------------------------------- assembling
def ffmeta_escape(s):
    for ch in ("\\", "=", ";", "#", "\n"):
        s = s.replace(ch, "\\" + ch)
    return s


def build_ffmetadata(meta, titles, durations):
    lines = [";FFMETADATA1",
             f"title={ffmeta_escape(meta.get('title', ''))}",
             f"artist={ffmeta_escape(meta.get('creator', ''))}",
             f"album={ffmeta_escape(meta.get('title', ''))}",
             "genre=Audiobook"]
    start = 0
    for title, dur in zip(titles, durations):
        end = start + int(round(dur * 1000))
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={ffmeta_escape(title)}"]
        start = end
    return "\n".join(lines) + "\n"


def measure_lufs(list_file, total=None):
    """Integrated loudness of the whole book (EBU R128), or None if it cannot be measured."""
    try:
        err = run_ffmpeg([FFMPEG, "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(list_file),
                          "-af", "ebur128=framelog=quiet", "-f", "null", "-"], total=total, label="measuring")
        found = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
        value = float(found[-1]) if found else None
        return value if value is not None and value > -70 else None
    except Exception:  # noqa: BLE001
        return None


class FfmpegStalled(RuntimeError):
    pass


def run_ffmpeg(cmd, total=None, label=None, stall=None):
    """Run an ffmpeg command and return what it wrote to stderr. It is watched: progress goes to the window
    ("@assemble <label> <seconds done> <seconds total>" when total is known), and if it makes no progress at all
    for FFMPEG_STALL seconds (a hidden error dialog, a disk that stopped answering) it is stopped instead of
    waiting for ever. A failure becomes a plain explanation that says what to do."""
    stall = FFMPEG_STALL if stall is None else stall
    cmd = [cmd[0], "-nostdin", "-progress", "pipe:1", "-nostats"] + list(cmd[1:])
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg was not found. Install it (winget install Gyan.FFmpeg) or put ffmpeg.exe and its "
                           "DLL files in the bin folder.") from None
    err, state = [], {"seen": time.time(), "done": 0.0}

    def drain():
        for line in proc.stderr:
            err.append(line)
            if len(err) > 400:
                del err[:100]

    def read():
        for line in proc.stdout:
            state["seen"] = time.time()
            if line.startswith("out_time_us="):
                try:
                    state["done"] = max(0.0, int(line.split("=", 1)[1]) / 1e6)
                except ValueError:
                    pass
    threads = [threading.Thread(target=drain, daemon=True), threading.Thread(target=read, daemon=True)]
    for t in threads:
        t.start()
    shown = 0.0
    while proc.poll() is None:
        time.sleep(0.25)
        if time.time() - state["seen"] > stall:
            proc.kill()
            proc.wait()
            raise FfmpegStalled(f"ffmpeg stopped responding while {label or 'making the audiobook'}: no progress for "
                                f"{stall // 60} minutes, so it was stopped. Finished chapters are kept; converting "
                                "again carries on from there.")
        if total and label and time.time() - shown > 1.5:
            shown = time.time()
            out(f"@assemble {label} {state['done']:.0f} {total:.0f}")
    for t in threads:
        t.join(5)
    text = "".join(err)
    if proc.returncode != 0:
        code = proc.returncode & 0xFFFFFFFF
        hint = {0xC0000135: "ffmpeg could not start because some of its files (DLLs) are missing.",
                0xC000013A: "ffmpeg was stopped from outside (closed, cancelled, or the PC went to sleep)."}.get(code, "")
        raise RuntimeError(f"ffmpeg failed (code {code:#x}). {hint} {text.strip()[-600:]}".strip())
    if total and label:
        out(f"@assemble {label} {total:.0f} {total:.0f}")
    return text


# --------------------------------------------------------------------------------------- finished files
def source_id(book):
    """Identifies the book file an audiobook was made from (name and size, so a moved folder still matches)."""
    p = Path(book)
    try:
        return f"{p.name}|{p.stat().st_size}"
    except OSError:
        return p.name


def load_outputs():
    try:
        return json.loads(OUTPUTS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def remember_output(path, book):
    data = load_outputs()
    data[str(Path(path).resolve()).lower()] = source_id(book)
    try:
        OUTPUTS_FILE.parent.mkdir(exist_ok=True)
        tmp = OUTPUTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, OUTPUTS_FILE)
    except OSError:
        pass


def output_ext(fmt):
    return {"mp3": ".mp3", "mp3_chapters": ""}.get(fmt, ".m4b")


def pick_output(out_base, fmt, book):
    """out_base, or "out_base (2)", "(3)"... so an audiobook made from a different book file is never replaced.
    Two books can carry the same title (a mislabelled EPUB once made two books write one file)."""
    known, me = load_outputs(), source_id(book)
    for n in range(1, 100):
        base = Path(str(out_base) if n == 1 else f"{out_base} ({n})")
        target = Path(str(base) + output_ext(fmt))
        if not target.exists() or known.get(str(target.resolve()).lower()) == me:
            return base
    return Path(f"{out_base} {time.strftime('%Y%m%d-%H%M%S')}")


def put_in_place(partial, target):
    """Move the finished file (or folder) over the old one. A player holding the old file open on Windows makes
    that fail; then the new one keeps a "(new)" name instead of being lost."""
    partial, target = Path(partial), Path(target)
    for attempt in range(5):
        try:
            if partial.is_dir():
                old = None
                if target.exists():
                    old = target.with_name(target.name + ".old")
                    import shutil
                    shutil.rmtree(old, ignore_errors=True)
                    os.replace(target, old)
                os.replace(partial, target)
                if old is not None:
                    import shutil
                    shutil.rmtree(old, ignore_errors=True)
            else:
                os.replace(partial, target)
            return target
        except PermissionError:
            time.sleep(1 + attempt)
    alt = target.with_name(f"{target.stem} (new){target.suffix}")
    os.replace(partial, alt)
    out(f"The old {target.name} is open in another program, so the new audiobook was saved as {alt.name}.")
    return alt


def book_filter(list_file, opts, total=None):
    """Pass 1 of the loudness standard: measure the whole book. Returns the ffmpeg filter for pass 2
    (one gain for the whole book plus a peak limit), or None."""
    if not opts.get("book_loudness"):
        return None
    lufs = measure_lufs(list_file, total)
    if lufs is None:
        out("Could not measure the book's loudness; keeping it as it is.")
        return None
    gain = max(-12.0, min(12.0, BOOK_LUFS - lufs))
    out(f"Book loudness {lufs:.1f} LUFS, adjusted to {BOOK_LUFS:.0f} LUFS with peaks under -3 dB.")
    return f"volume={gain:.2f}dB,alimiter=limit=0.708:level=false:latency=true"


def cover_args(index, cover_path, mp3):
    if not cover_path:
        return []
    args = ["-map", f"{index}:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    if mp3:
        args += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    return args


def make_chapter_cards(meta, titles, cover_path, work):
    """One title card (JPEG file) per chapter, in work/cards. Returns the list of paths, or [] if the pictures
    could not be made (a conversion never fails because of chapter art)."""
    try:
        from covers import make_chapter_card
        cover = Path(cover_path).read_bytes() if cover_path else None
        folder = work / "cards"
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for n, title in enumerate(titles, 1):
            f = folder / f"{n:02d}.jpg"
            f.write_bytes(make_chapter_card(n, title, meta.get("title", ""), cover, total=len(titles)))
            paths.append(f)
        return paths
    except Exception as e:  # noqa: BLE001
        out(f"Chapter art could not be made ({type(e).__name__}: {e}); continuing without it.")
        return []


def add_chapter_pictures(mp3_path, cards):
    """Put each chapter's card inside the chapter marks of a single MP3 (ID3 CHAP frames with a picture)."""
    try:
        from mutagen.id3 import APIC, ID3
    except ImportError:
        out("Chapter art in a single MP3 needs the mutagen package (runtime\\python\\python.exe -m uv pip install mutagen); skipped.")
        return False
    tags = ID3(str(mp3_path))
    chaps = sorted(tags.getall("CHAP"), key=lambda c: c.start_time)
    if len(chaps) != len(cards):
        out(f"Chapter art skipped: the MP3 has {len(chaps)} chapter marks but {len(cards)} pictures were made.")
        return False
    # mutagen writes frames smallest first, and players may read chapters in file order. Pad the (invisible)
    # picture description so each chapter's frame is bigger than the one before it.
    from mutagen.id3._tags import ID3SaveConfig, save_frame
    config = ID3SaveConfig(3, "/")
    previous = 0
    for chap, card in zip(chaps, cards):
        data = Path(card).read_bytes()
        pad = 0
        while True:
            chap.sub_frames.setall("APIC", [APIC(encoding=0, mime="image/jpeg", type=0, desc=" " * pad, data=data)])
            size = len(save_frame(chap, config=config))
            if size > previous:
                break
            pad += previous - size + 1
        previous = size
    tags.save(str(mp3_path), v2_version=3)
    return True


def assemble_book(meta, titles, wavs, durations, out_base, work, cover_path=None, opts=None):
    """Make the finished audiobook. out_base: the path without extension. Returns the file (or folder)."""
    opts = opts or DEFAULT_OPTIONS
    fmt = opts.get("format", "m4b")
    cards = []
    if opts.get("chapter_art"):
        if fmt == "m4b":
            out("Chapter art needs an MP3 format: M4B files cannot carry a picture for every chapter here. "
                "Choose MP3 in Settings > Output to use it.")
        else:
            cards = make_chapter_cards(meta, titles, cover_path, work)
    kbps = QUALITY_KBPS.get(opts.get("quality"), 64)
    meta_file = work / "chapters.ffmetadata"
    meta_file.write_text(build_ffmetadata(meta, titles, durations), encoding="utf-8")
    list_file = work / "concat.txt"
    list_file.write_text(
        "".join("file '{}'\n".format(str(w.resolve()).replace("\\", "/").replace("'", "'\\''")) for w in wavs),
        encoding="utf-8")
    total = sum(durations)
    audio_filter = book_filter(list_file, opts, total)
    filt = ["-af", audio_filter] if audio_filter else []

    if fmt == "mp3_chapters":            # a folder with one MP3 per chapter, numbered, plus a playlist
        target = Path(str(out_base))
        folder = Path(str(out_base) + ".partial")
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        done_before = 0.0
        names = []
        for n, (wav, title) in enumerate(zip(wavs, titles), 1):
            name = f"{n:02d} - {safe_name(title)[:60]}.mp3"
            picture = str(cards[n - 1]) if cards else (str(cover_path) if cover_path else None)
            cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav)]
            if picture:
                cmd += ["-i", picture]
            cmd += ["-map", "0:a"] + cover_args(1, picture, True) + filt
            cmd += ["-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-id3v2_version", "3",
                    "-metadata", f"title={title}", "-metadata", f"artist={meta.get('creator', '')}",
                    "-metadata", f"album={meta.get('title', '')}", "-metadata", f"track={n}/{len(wavs)}",
                    "-metadata", "genre=Audiobook", str(folder / name)]
            run_ffmpeg(cmd)
            names.append(name)
            done_before += durations[n - 1]
            out(f"@assemble encoding {done_before:.0f} {total:.0f}")
        (folder / f"{safe_name(meta.get('title') or 'book')}.m3u").write_text(
            "#EXTM3U\n" + "".join(f"#EXTINF:{int(d)},{t}\n{nm}\n" for nm, t, d in zip(names, titles, durations)),
            encoding="utf-8")
        return put_in_place(folder, target)

    mp3 = fmt == "mp3"
    out_path = Path(str(out_base) + (".mp3" if mp3 else ".m4b"))
    partial = out_path.with_name(out_path.stem + ".partial" + out_path.suffix)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-i", str(meta_file)]
    if cover_path:
        cmd += ["-i", str(cover_path)]
    cmd += ["-map_metadata", "1", "-map_chapters", "1", "-map", "0:a"] + cover_args(2, cover_path, mp3) + filt
    if mp3:
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-id3v2_version", "3", "-f", "mp3", str(partial)]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart", "-f", "ipod", str(partial)]
    try:
        run_ffmpeg(cmd, total=total, label="encoding")
        if mp3 and cards:
            add_chapter_pictures(partial, cards)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    return put_in_place(partial, out_path)


def assemble_m4b(meta, titles, wavs, durations, out_path, work, cover_path=None, book_loudness=False):
    """Older entry point (kept for scripts): an M4B at out_path."""
    opts = dict(DEFAULT_OPTIONS, book_loudness=book_loudness, format="m4b")
    return assemble_book(meta, titles, wavs, durations, Path(out_path).with_suffix(""), work, cover_path, opts)


def cleanup_work(wavs, work, audio_dir, cover_path, pieces_dir=None, used=()):
    """Remove the temporary files of a finished book. Only the files this run made are deleted."""
    extra = [work / "concat.txt", work / "chapters.ffmetadata"] + ([cover_path] if cover_path else [])
    if pieces_dir is not None:
        extra += [pieces_dir / f"{k}.npy" for k in used]
    extra += list(audio_dir.glob("*.claim"))
    extra += list((work / "cards").glob("*.jpg")) if (work / "cards").exists() else []
    extra += [Path(w).with_suffix(".report.json") for w in wavs]
    if pieces_dir is not None:
        extra += [pieces_dir / f"{k}.json" for k in used]
    for f in list(wavs) + extra:
        try:
            Path(f).unlink()
        except OSError:
            pass
    for d in (audio_dir, pieces_dir, work / "cards", work):  # remove the folders too, but only if nothing else is left in them
        if d is None:
            continue
        try:
            d.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------------------- two at once
def claim(audio_dir, idx):
    """Take a chapter so the other process does not start it too. False if it is already taken."""
    try:
        fd = os.open(str(audio_dir / f"{idx:02d}.claim"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def chapter_done(wav):
    return wav.exists() and wav.stat().st_size > 0


def start_second_process():
    """A second converter for 'two chapters at once'. It takes chapters from the same list, writes their
    audio next to ours and exits; we assemble. It quits on its own if this process ends (its input
    pipe closes), so cancelling never leaves it running."""
    cmd = [sys.executable, "-u", str(Path(__file__).resolve())] + sys.argv[1:] + ["--worker"]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, cwd=os.getcwd(), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=0x08000000 if os.name == "nt" else 0)

    proc.last_seen = time.time()

    def relay():
        for line in proc.stdout:
            proc.last_seen = time.time()
            line = line.rstrip()
            if line and not line.startswith(("WARNING: Defaulting repo_id", "@log ")):
                out(line)
    threading.Thread(target=relay, daemon=True).start()
    return proc


def wait_for_second(proc, pause_file=None):
    """Wait for the second converter, but not for ever: silent for WORKER_STALL (not paused) means it hung.
    Its unfinished chapters are then spoken by this process."""
    while proc.poll() is None:
        time.sleep(2)
        if pause_file and os.path.exists(pause_file):
            proc.last_seen = time.time()
        elif time.time() - proc.last_seen > WORKER_STALL:
            out(f"The second converter stopped responding for {WORKER_STALL // 60} minutes; its chapters are finished here.")
            proc.kill()
            proc.wait()
            return


def exit_when_parent_goes():
    def watch():
        try:
            sys.stdin.read()
        except Exception:  # noqa: BLE001
            pass
        os._exit(3)
    threading.Thread(target=watch, daemon=True).start()


# --------------------------------------------------------------------------------------- listening report
def clock(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def write_report(meta, picked, chapters, durations, audio_dir, out_path, book_stem):
    """A list of the places worth a listen, with their time in the finished audiobook. Written to the logs
    folder as text (for people) and JSON (for the window). Returns (json path, number of places) or (None, 0)."""
    events, names, start, paragraphs = [], {}, 0.0, []
    labels = {"fixed": "Read again (it first came out cut off or rambling): check it sounds natural",
              "suspect": "May be cut off or stretched (could not be fixed automatically)",
              "silence": "A long silence in the middle of speech"}
    for pos, idx in enumerate(picked):
        try:
            rep = json.loads((audio_dir / f"{idx:02d}.report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rep = {}
        for e in rep.get("events", []):
            events.append(dict(e, book_at=round(start + e["at"], 2), chapter=idx, chapter_pos=pos,
                               chapter_title=chapters[idx - 1].title))
        if rep.get("items"):
            paragraphs.append({"chapter": idx, "chapter_pos": pos, "title": chapters[idx - 1].title,
                               "start": round(start, 3), "duration": durations.get(idx, rep.get("duration", 0)),
                               "items": rep["items"]})
        for word, (count, first) in rep.get("names", {}).items():
            have = names.get(word)
            names[word] = [count + (have[0] if have else 0), have[1] if have else round(start + first, 2),
                           have[2] if have else idx]
        start += durations.get(idx, rep.get("duration", 0))
    top = sorted(names.items(), key=lambda kv: (-kv[1][0], kv[0]))[:20]
    data = {"book": meta.get("title", ""), "output": str(Path(out_path).resolve()), "events": events,
            "names": [{"word": w, "count": c, "book_at": at, "chapter": ch} for w, (c, at, ch) in top],
            "paragraphs": paragraphs}       # where every paragraph is, for "Check by listening" (bookcheck.py)
    lines = [f"Listening report: {meta.get('title', '')}", f"Made {time.strftime('%Y-%m-%d %H:%M')} for {Path(out_path).name}",
             "Times are positions in the finished audiobook; the chapter and the time inside it are in brackets.", ""]
    for kind in ("fixed", "suspect", "silence"):
        group = [e for e in events if e["kind"] == kind]
        if group:
            lines.append(labels[kind] + ":")
            for e in group:
                lines.append(f"  {clock(e['book_at'])}  ({e['chapter_title']}, {clock(e['at'])[2:]})  {e['text']}")
            lines.append("")
    if top:
        lines.append("Unfamiliar words the voice had to guess, most used first. If one sounds wrong, add a pronunciation fix:")
        for w, (c, at, ch) in top:
            lines.append(f"  {w:<20} {c:>5}x   first at {clock(at)}")
        lines.append("")
    if not events and not top:
        lines.append("Nothing in particular to check.")
    try:
        (LOG_DIR / "reports").mkdir(parents=True, exist_ok=True)
        base = LOG_DIR / "reports" / f"{safe_name(book_stem)[:60]} - listening report"
        base.with_suffix(".txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        base.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return base.with_suffix(".json"), len(events)
    except OSError:
        return None, 0


# --------------------------------------------------------------------------------------- main
def render_chapter(speaker, ch, idx, n, total, args, opts, compiled, audio_dir, pieces_dir, used, guesser):
    """Speak one chapter into audio_dir/NN.wav, falling back from experimental modes if they fail."""
    wav = audio_dir / f"{idx:02d}.wav"
    c0 = time.time()
    oom_retried = False
    report = {}
    for _attempt in range(4):
        try:
            audio, fixed, reused = synth_chapter(speaker, ch, args.voice, idx, opts, compiled, args.pause_file,
                                                 pieces_dir, used, guesser, report)
            break
        except Exception as e:  # noqa: BLE001
            if checks.is_out_of_memory(e) and not oom_retried:
                out("The graphics card ran out of memory; freeing it and trying this chapter again.")
                oom_retried = True
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(3)
            elif speaker.half:
                out(f"Half precision stopped working ({type(e).__name__}: {e}); continuing in full precision.")
                speaker.half = False
            elif speaker.fast:
                out(f"Faster mode stopped working ({type(e).__name__}: {e}); continuing in normal mode.")
                speaker.fast = False
            else:
                raise
    tmp = audio_dir / f"{idx:02d}.partial.wav"
    sf.write(str(tmp), audio, SAMPLE_RATE, format="WAV")
    try:
        (audio_dir / f"{idx:02d}.report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    os.replace(tmp, wav)
    dur = len(audio) / SAMPLE_RATE
    took = max(0.001, time.time() - c0)
    notes = []
    if reused:
        notes.append(f"{reused} paragraph{'s' if reused != 1 else ''} reused")
    if fixed:
        notes.append(f"re-spoke {fixed} that sounded cut off")
    out(f"[{n}/{total}] chapter {idx}: {ch.title} - {dur / 60:.1f} min of audio in {took:.0f}s "
        f"({dur / took:.1f}x realtime)" + (f" ({', '.join(notes)})" if notes else ""))
    try:
        (audio_dir / f"{idx:02d}.claim").unlink()
    except OSError:
        pass
    return dur


def pick_sample(chapters, picked, at=None, words=150):
    """(chapter number, set of paragraph numbers) for a sample. at: "chapter" or "chapter:para" or
    "chapter:first-last" (paragraph numbers from 1). Without it: about a minute of ordinary narration
    from a third of the way into the middle chapter of the selection."""
    if at:
        ch_part, _, par_part = at.partition(":")
        idx = max(1, min(int(ch_part), len(chapters)))
        pars = chapters[idx - 1].paragraphs
        if par_part:
            a, _, b = par_part.partition("-")
            first = max(1, min(int(a), len(pars)))
            last = max(first, min(int(b or a), len(pars)))
            return idx, set(range(first - 1, last))
        start = 0
    else:
        idx = picked[len(picked) // 2]
        pars = chapters[idx - 1].paragraphs
        start = len(pars) // 3
        while start < len(pars) - 1 and (len(pars[start].split()) < 25 or SCENE_BREAK_RE.match(pars[start])):
            start += 1                      # begin on a real paragraph, not a heading or a one-liner
    chosen, count = set(), 0
    for i in range(start, len(pars)):
        chosen.add(i)
        count += len(pars[i].split())
        if count >= words or len(chosen) >= 8:
            break
    i = start - 1
    while not at and count < words and i >= 0 and len(chosen) < 8:   # near the chapter's end: reach back
        chosen.add(i)
        count += len(pars[i].split())
        i -= 1
    return idx, chosen


def make_sample(args, chapters, picked, opts, compiled, guesser, speaker):
    idx, only = pick_sample(chapters, picked, args.sample_at)
    ch = chapters[idx - 1]
    items, _, _ = plan_chapter(ch, args.voice, opts, compiled, guesser, only=only)
    sample = type(ch)(ch.title, [ch.paragraphs[i] for i in sorted(only)])
    out(f"@sample-from {idx} {min(only) + 1} {max(only) + 1}")
    parts = [silence(0.2)]
    for k, spoken in speaker.run(items, None):
        pieces = items[k - 1][0]
        for n, (v, chunks) in enumerate(spoken):
            if n:
                parts.append(silence(0.12 if opts["trim"] else 0.0))
            a = join_chunks(chunks, opts["trim"])
            if opts["self_check"]:
                a, _status = self_check(speaker, pieces[n][0], v, a, opts["trim"])
            parts.append(a)
        parts.append(silence(items[k - 1][1]))
    audio = np.concatenate(parts)
    if opts["loudness"]:
        audio = even_loudness(audio)
    if opts["room_tone"]:
        audio = add_room_tone(audio, seed=idx)
    target = Path(args.sample)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.stem + ".partial.wav")
    sf.write(str(tmp), audio, SAMPLE_RATE, format="WAV")
    os.replace(tmp, target)
    out(f"Sample of chapter {idx} ({sample.words} words) -> {target.resolve()}")


def title_mismatch(title, stem):
    """True when the title inside a book has nothing to do with its file name ("One Year After" inside
    "One Second After by ...epub"). File names that are only numbers or codes are not compared."""
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
    t, f = norm(title), norm(stem)
    if not t or len(re.findall(r"[a-z]{3,}", f)) < 2:
        return False
    return t not in f and f.split(" by ")[0].split("  ")[0] not in t and \
        not set(re.findall(r"[a-z]{4,}", t)) <= set(re.findall(r"[a-z]{4,}", f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epub", help="the book file (.epub .txt .md .docx .html .pdf)")
    ap.add_argument("--voice", default="af_heart")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--out", default="output", help="folder for the finished audiobook")
    ap.add_argument("--keep-work", action="store_true", help="keep the paragraph and chapter audio after finishing")
    ap.add_argument("--title", help="use this title instead of the book's")
    ap.add_argument("--author", help="use this author instead of the book's")
    ap.add_argument("--cover", help="use this image file as the cover")
    ap.add_argument("--rules", default=str(RULES_FILE), help="pronunciation rules file")
    ap.add_argument("--pause-file", help="while this file exists, wait (used by the Pause button)")
    ap.add_argument("--options", help="JSON narration options overriding settings.json")
    ap.add_argument("--chapters", help="e.g. 5, 1-3, or 1,4,9 (numbers as listed in the window)")
    ap.add_argument("--sample", help="only make a short sample with the current settings, into this .wav file")
    ap.add_argument("--sample-at", help="where the sample comes from: CHAPTER or CHAPTER:PARA or CHAPTER:FIRST-LAST")
    ap.add_argument("--skip-checks", action="store_true", help="convert even if disk space looks too low")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)  # the second process of 'two at once'
    args = ap.parse_args()
    if args.worker:
        exit_when_parent_goes()
    elif not args.sample:
        start_log(Path(args.epub).stem)

    epub = Path(args.epub)
    try:
        meta, chapters = read_book(epub)
    except BookError as e:
        out(f"Could not read the book: {e}")
        sys.exit(2)
    if args.title:
        meta["title"] = args.title
    elif not args.worker and title_mismatch(meta.get("title", ""), epub.stem):
        out(f"Note: this book calls itself \"{meta.get('title')}\", but its file is named \"{epub.stem}\". The audiobook "
            "is named after the book's own title; if that is wrong, use Edit details... in the window.")
    if args.author is not None:
        meta["creator"] = args.author
    picked = parse_range(args.chapters, len(chapters))
    if not picked:
        sys.exit("No chapters selected.")

    opts = load_options(json.loads(args.options) if args.options else None)
    rules = load_rules(args.rules)
    compiled = compile_rules(rules)
    guesser = None
    if opts.get("dialogue_voice") in ("auto", "cast"):
        guesser = speakers.SpeakerGuesser(p for c in chapters for p in c.paragraphs)
        ai = speakers.load_ai_tags(epub.stem, CASTING_DIR) if opts.get("ai_speakers", True) else None
        if ai:
            keys = {speakers.chapter_key(c.paragraphs) for c in chapters}
            matched = sum(1 for k in ai["chapters"] if k in keys)
            if matched:
                guesser.use_ai(ai)
                opts["ai_tags"] = speakers.ai_hash(ai)          # new tags -> the chapters are put together again
                if not args.worker:
                    out(f"Using the AI speaker tags for {matched} of {len(chapters)} chapters.")
    chosen_cast = speakers.load_cast(epub.stem, CASTING_DIR) if guesser else {}
    if guesser and not chosen_cast and not opts.get("ai_tags") and not speakers.looks_like_fiction(chapters):
        if not args.worker:
            out("This book has little dialogue (it reads like non-fiction), so quotations stay in the narrator's voice. "
                "To give them voices anyway, use Cast... in the window.")
        opts["dialogue_voice"] = ""
        guesser = None
    characters = []
    if opts.get("dialogue_voice") == "cast":     # each character their own voice (chosen in the window, or automatic)
        characters = speakers.find_characters(chapters, guesser, keep=chosen_cast)
        opts["cast"] = speakers.cast(characters, args.voice, opts["dialogue_female"], opts["dialogue_male"],
                                     speakers.load_cast(epub.stem, CASTING_DIR))

    if args.sample:                   # a short sample with the current settings, nothing else
        import torch
        torch.backends.cudnn.enabled = False
        from kokoro import KPipeline
        device = "cuda" if torch.cuda.is_available() else "cpu"
        pipe = KPipeline(lang_code="a", device=device)
        speaker = Speaker(pipe, args.speed, fast=False, half=bool(opts["half"]) and device == "cuda")
        try:
            make_sample(args, chapters, picked, opts, compiled, guesser, speaker)
        except HalfPrecisionFailed:
            speaker.half = False
            make_sample(args, chapters, picked, opts, compiled, guesser, speaker)
        return

    work = Path("work") / safe_name(epub.stem)
    # per voice / speed / narration options / pronunciation fixes: changing any of them re-assembles the
    # chapter, but paragraphs that did not change come from pieces/ instead of being spoken again
    voice_part = re.sub(r"[^A-Za-z0-9._-]+", "-", args.voice)[:40]          # "custom:My Voice" -> "custom-My-Voice"
    audio_dir = work / f"audio-{voice_part}-{args.speed:g}-{fingerprint(args.voice, args.speed, opts, rules)}"
    audio_dir.mkdir(parents=True, exist_ok=True)
    pieces_dir = work / "pieces"
    pieces_dir.mkdir(parents=True, exist_ok=True)

    todo = [i for i in picked if not chapter_done(audio_dir / f"{i:02d}.wav")]
    if opts["keep_awake"]:
        stay_awake(True)
    if not args.worker and not args.skip_checks:
        problem = checks.disk_problem(work, args.out, sum(chapters[i - 1].words for i in todo),
                                      sum(chapters[i - 1].words for i in picked), QUALITY_KBPS.get(opts["quality"], 64))
        if problem:
            out(problem)
            out(f"@problem {problem}")
            sys.exit(4)
    second = None
    if opts["parallel"] and not args.worker and len(todo) >= 2:
        for c in audio_dir.glob("*.claim"):
            try:
                c.unlink()
            except OSError:
                pass

    import torch
    # MIOpen re-tunes for every new input length, which made real books ~40x slower.
    # Turning it off keeps ~25x realtime on the RX 9070 XT (measured with bench2.py).
    torch.backends.cudnn.enabled = False
    from kokoro import KPipeline
    device = "cuda" if torch.cuda.is_available() else "cpu"
    half = bool(opts["half"]) and device == "cuda"
    if not args.worker:
        parallel_ok, notes = checks.memory_plan(torch, bool(opts["parallel"]) and len(todo) >= 2)
        if opts["parallel"] and len(todo) >= 2 and not parallel_ok:
            opts["parallel"] = False
        for note in notes:
            out(note)
        out(f"{meta.get('title')} - {meta.get('creator')}")
        out(f"Device: {device}" + (f" ({torch.cuda.get_device_name(0)})" if device == "cuda" else ""))
        if FFMPEG_NOTE:
            out(FFMPEG_NOTE)
        if device == "cpu":
            out("No supported graphics card was found, so the voice runs on the processor. That works, but is many "
                "times slower. Audiobook Forge needs an AMD Radeon RX 9000 series card with a recent driver.")
        if compiled:
            out(f"Using {len(compiled)} pronunciation fix{'es' if len(compiled) != 1 else ''}.")
        if opts.get("dialogue_voice") == "cast":
            own = [f"{n} ({v})" for n, v in opts["cast"].items() if v not in (opts["dialogue_female"], opts["dialogue_male"], args.voice)]
            out((f"Each character has their own voice: {', '.join(own[:12])}" if own else
                 "Each character their own voice: none could be given one automatically")
                + (f" and {len(own) - 12} more" if len(own) > 12 else "") + ". Others use "
                f"{opts['dialogue_female']} (women) and {opts['dialogue_male']} (men).")
            unsure = [c["name"] for c in characters if not c.get("gender") and c["name"] not in speakers.load_cast(epub.stem, CASTING_DIR)]
            if unsure:
                out(f"Could not tell whether {', '.join(unsure[:8])}{' and others' if len(unsure) > 8 else ''} "
                    f"{'is a man or a woman' if len(unsure) == 1 else 'are men or women'}, so their lines stay with the "
                    "narrator. Give them a voice with Cast... in the window.")
        elif opts.get("dialogue_voice") == "auto":
            out(f"Quoted speech is read by {opts['dialogue_female']} (women) and {opts['dialogue_male']} (men) "
                "when the speaker is clear.")
        elif opts.get("dialogue_voice") and opts["dialogue_voice"] != args.voice:
            out(f"Quoted speech is read by {opts['dialogue_voice']}.")
        modes = [m for m, on in (("faster mode", opts["fast"]), ("half precision", half),
                                 ("two chapters at once", opts["parallel"] and len(todo) >= 2)) if on]
        if opts["half"] and device != "cuda":
            out("Half precision needs the graphics card; using full precision.")
        if modes:
            out("Experimental: " + ", ".join(modes) + ".")
        out(f"Converting {len(picked)} of {len(chapters)} chapters. First chapter includes ~1 minute GPU warm-up.")
    if opts["parallel"] and not args.worker and len(todo) >= 2:
        second = start_second_process()
    pipe = KPipeline(lang_code="a", device=device)
    speaker = Speaker(pipe, args.speed, fast=bool(opts["fast"]), half=half)

    used = set()
    durations = {}
    t0 = time.time()
    waiting = []      # chapters the second process is speaking
    for n, idx in enumerate(picked, 1):
        ch = chapters[idx - 1]
        wav = audio_dir / f"{idx:02d}.wav"
        wait_while_paused(args.pause_file)
        if chapter_done(wav):
            if not args.worker:
                dur = sf.info(str(wav)).duration
                if idx not in todo:
                    out(f"[{n}/{len(picked)}] chapter {idx}: {ch.title} - already done ({dur / 60:.1f} min)")
                durations[idx] = dur
            continue
        if (second is not None or args.worker) and not claim(audio_dir, idx):
            waiting.append((n, idx))
            continue
        durations[idx] = render_chapter(speaker, ch, idx, n, len(picked), args, opts, compiled, audio_dir,
                                        pieces_dir, used, guesser)
    if args.worker:
        return
    if second is not None:
        wait_for_second(second, args.pause_file)
    for n, idx in waiting:           # the second process's chapters (spoken here if it stopped early)
        wav = audio_dir / f"{idx:02d}.wav"
        if not chapter_done(wav):
            durations[idx] = render_chapter(speaker, chapters[idx - 1], idx, n, len(picked), args, opts, compiled,
                                            audio_dir, pieces_dir, used, guesser)
        else:
            durations[idx] = sf.info(str(wav)).duration
    if second is not None:          # the paragraphs the second process spoke belong to this run too
        for idx in picked:
            for pieces, _ in plan_chapter(chapters[idx - 1], args.voice, opts, compiled, guesser)[0]:
                used.add(piece_key(pieces, speaker, opts))
    wavs = [audio_dir / f"{i:02d}.wav" for i in picked]
    titles = [chapters[i - 1].title for i in picked]
    durs = [durations[i] for i in picked]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cover = None
    if args.cover and Path(args.cover).is_file():
        suffix = Path(args.cover).suffix.lower()
        cover = (Path(args.cover).read_bytes(), ".png" if suffix == ".png" else ".jpg")
    if not cover:
        cover = read_book_cover(epub)
    if not cover:  # the book has no cover: make a simple one so players still show something
        try:
            from covers import make_cover
            cover = (make_cover(meta.get("title") or epub.stem, meta.get("creator", "")), ".jpg")
        except Exception:  # noqa: BLE001
            cover = None
    cover_path = None
    if cover:
        cover_path = work / f"cover{cover[1]}"
        cover_path.write_bytes(cover[0])
    suffix = "" if len(picked) == len(chapters) else f" (chapters {args.chapters})"
    title_part = safe_name(meta.get("title") or epub.stem)
    author_part = safe_name(meta["creator"]) if meta.get("creator", "").strip() else ""
    out_base = out_dir / ((f"{author_part} - " if author_part else "") + f"{title_part}{suffix}")
    first_choice = out_base
    out_base = pick_output(out_base, opts["format"], epub)
    if out_base != first_choice:
        out(f"{first_choice.name}{output_ext(opts['format'])} already exists and was made from a different book file, "
            f"so this one is saved as {out_base.name}{output_ext(opts['format'])}.")
    out(f"@summary {sum(durs):.1f}")
    out({"mp3": "Assembling one MP3 with chapter markers...",
         "mp3_chapters": "Making one MP3 file per chapter..."}.get(opts["format"], "Assembling M4B with chapter markers..."))
    out_path = assemble_book(meta, titles, wavs, durs, out_base, work, cover_path, opts)
    remember_output(out_path, epub)
    report_path, places = write_report(meta, picked, chapters, durations, audio_dir, out_path, epub.stem)
    if report_path:
        out(f"Listening report: {places} place{'s' if places != 1 else ''} worth a listen -> {report_path.with_suffix('.txt')}")
        out(f"@report {report_path}")
    if not args.keep_work:  # the audiobook exists now; the paragraph and chapter audio only served resuming
        cleanup_work(wavs, work, audio_dir, cover_path, pieces_dir, used)
    out(f"Done in {(time.time() - t0) / 60:.1f} min -> {out_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        out(traceback.format_exc().rstrip())
        if checks.is_disk_full(exc):
            msg = ("The disk is full, so the conversion stopped. Finished paragraphs and chapters are kept: free "
                   "up some space and convert the same book again to carry on.")
            out(msg)
            out(f"@problem {msg}")
            sys.exit(4)
        if isinstance(exc, RuntimeError) and str(exc).startswith(("ffmpeg failed", "ffmpeg was not found")):
            msg = (str(exc).splitlines()[0][:300] + " Your chapters are kept: fix this, then convert the same book "
                   "again and it will only assemble the audiobook.")
            out(msg)
            out(f"@problem {msg}")
            sys.exit(6)
        if checks.is_out_of_memory(exc):
            msg = ("The graphics card ran out of memory. Close games or video programs, or turn off two chapters "
                   "at once, then convert the same book again. Finished paragraphs are kept.")
            out(msg)
            out(f"@problem {msg}")
            sys.exit(5)
        sys.exit(1)
