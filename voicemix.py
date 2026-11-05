"""Your own voices: a mix of two Kokoro voices, e.g. 70% Heart + 30% Bella.

    python voicemix.py make "Warm Heart" af_heart af_bella 0.7   # save it (voices\\Warm Heart.pt)
    python voicemix.py try af_heart af_bella 0.7 out.wav ["text"]  # hear a mix without saving it
    python voicemix.py list

A mixed voice is used everywhere by the name "custom:<name>" (narrator, quoted speech, characters).
"""
import hashlib
import json
import os
import sys
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
VOICES_DIR = HERE / "voices"
PREFIX = "custom:"
FALLBACK = "af_heart"
SAMPLE = "Hello. This is how this voice sounds when it reads your book, with a question or two? And a pause."


def safe(name):
    import re
    return re.sub(r"[^A-Za-z0-9 ._-]+", "", name).strip()[:40]


def path_of(voice):
    return VOICES_DIR / f"{safe(voice[len(PREFIX):] if voice.startswith(PREFIX) else voice)}.pt"


def is_custom(voice):
    return isinstance(voice, str) and voice.startswith(PREFIX)


def resolve(voice):
    """What to give Kokoro: built-in names stay, "custom:X" becomes the path of its file. Blends ("a,b") are
    resolved part by part. A mix that no longer exists falls back to the standard voice."""
    parts = []
    for v in str(voice).split(","):
        if is_custom(v):
            p = path_of(v)
            parts.append(str(p) if p.exists() else FALLBACK)
        else:
            parts.append(v)
    return ",".join(parts)


def file_hash(voice):
    p = path_of(voice)
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:8]
    except OSError:
        return "missing"


def tag(voice):
    """The voice name plus a fingerprint of each mix it uses, so changing a mix re-reads what it spoke."""
    return ",".join(f"{v}@{file_hash(v)}" if is_custom(v) else v for v in str(voice).split(","))


def list_custom():
    """[(voice id, name, description)] for the saved mixes."""
    out = []
    for p in sorted(VOICES_DIR.glob("*.pt")) if VOICES_DIR.exists() else []:
        if p.stem.startswith("_"):
            continue
        info = {}
        try:
            info = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        desc = info.get("description") or ""
        out.append((PREFIX + p.stem, p.stem, desc))
    return out


def describe(a, b, weight):
    from_label = lambda v: v.split("_", 1)[-1].capitalize()       # noqa: E731
    return f"{round(weight * 100)}% {from_label(a)} + {round((1 - weight) * 100)}% {from_label(b)}"


# ------------------------------------------------------------------ the parts that need torch
def _env():
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HOME", str(HERE / "models"))
    warnings.filterwarnings("ignore")


def load_pack(voice):
    """The voice's style tensor (downloaded once into models\\ for built-in voices)."""
    _env()
    import torch
    if is_custom(voice):
        f = path_of(voice)
    else:
        from huggingface_hub import hf_hub_download
        f = hf_hub_download(repo_id="hexgrad/Kokoro-82M", filename=f"voices/{voice}.pt")
    return torch.load(f, weights_only=True)


def mix(a, b, weight):
    weight = max(0.0, min(1.0, float(weight)))
    return load_pack(a) * weight + load_pack(b) * (1.0 - weight)


def make(name, a, b, weight):
    """Save a mix as voices\\<name>.pt. Returns its voice id."""
    import torch
    name = safe(name)
    if not name:
        raise ValueError("The voice needs a name.")
    VOICES_DIR.mkdir(exist_ok=True)
    pack = mix(a, b, weight)
    target = VOICES_DIR / f"{name}.pt"
    tmp = VOICES_DIR / f"_{name}.partial"
    torch.save(pack, str(tmp))
    os.replace(tmp, target)
    target.with_suffix(".json").write_text(json.dumps({"a": a, "b": b, "weight": round(float(weight), 3),
                                                       "description": describe(a, b, float(weight))}), encoding="utf-8")
    return PREFIX + name


def delete(voice):
    for p in (path_of(voice), path_of(voice).with_suffix(".json")):
        try:
            p.unlink()
        except OSError:
            pass


def speak(voice_or_pack, out_wav, text=SAMPLE):
    """Say text with a voice name or a mixed tensor, into out_wav."""
    _env()
    import numpy as np
    import soundfile as sf
    import torch
    torch.backends.cudnn.enabled = False
    from kokoro import KPipeline
    import normalize
    pipe = KPipeline(lang_code="a", device="cuda" if torch.cuda.is_available() else "cpu")
    voice = resolve(voice_or_pack) if isinstance(voice_or_pack, str) else voice_or_pack
    parts = [a.cpu().numpy() if hasattr(a, "cpu") else np.asarray(a) for _, _, a in pipe(normalize.prepare(text), voice=voice)]
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_wav.with_suffix(".partial.wav")
    sf.write(str(tmp), np.concatenate(parts), 24000, format="WAV")
    os.replace(tmp, out_wav)


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "make" and len(args) >= 5:
        print(make(args[1], args[2], args[3], float(args[4])))
    elif args[0] == "try" and len(args) >= 5:
        speak(mix(args[1], args[2], float(args[3])), args[4], args[5] if len(args) > 5 and args[5].strip() else SAMPLE)
        print("done", args[4])
    elif args[0] == "list":
        for vid, name, desc in list_custom():
            print(f"{vid}\t{desc}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
