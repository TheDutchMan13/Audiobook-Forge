"""Make a short voice sample.

    python preview.py <voice> <out.wav>            # the standard sample sentence
    python preview.py <voice> <out.wav> "some text" # speak this text (pronunciation fixes are applied)
    python preview.py <voice> <out.wav> "text" --raw # speak exactly this text (fixes already applied)
"""
import os
import sys
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HOME", str(HERE / "models"))
warnings.filterwarnings("ignore")

import numpy as np
import soundfile as sf
import torch

torch.backends.cudnn.enabled = False
from kokoro import KPipeline
import normalize
import voicemix
from pronunciations import apply_rules, load_rules

TEXT = "Hello. This is a short sample of how this voice sounds when it reads your book."

args = [a for a in sys.argv[1:] if a != "--raw"]
raw = "--raw" in sys.argv[1:]
voice, out = args[0], Path(args[1])
text = TEXT
if len(args) > 2 and args[2].strip():
    rules = load_rules()
    text = (args[2] if raw else normalize.prepare(args[2], None, lambda t: apply_rules(t, rules)))[:600]   # a short test sentence
pipe = KPipeline(lang_code="a", device="cuda" if torch.cuda.is_available() else "cpu")
parts = [a.cpu().numpy() if hasattr(a, "cpu") else np.asarray(a) for _, _, a in pipe(text, voice=voicemix.resolve(voice))]
out.parent.mkdir(parents=True, exist_ok=True)
tmp = out.with_suffix(".partial.wav")
sf.write(str(tmp), np.concatenate(parts), 24000, format="WAV")
os.replace(tmp, out)
print("done", out)
