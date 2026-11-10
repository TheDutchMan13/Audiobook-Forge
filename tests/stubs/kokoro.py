"""Stand-in for the kokoro package with the same API shape as kokoro 0.9.4.

- g2p() and en_tokenize() do the CPU text work (slow-ish, configurable)
- infer() does the "GPU" work (sleep, configurable) and returns audio padded with silence on both
  ends, at a different loudness per voice, so trimming and loudness evening can be tested
- every spoken text and its voice is logged to spoken.log
"""
import os
import threading
import time
import types

import numpy as np

G2P_DELAY = float(os.environ.get("STUB_G2P", "0.0"))
GPU_DELAY = float(os.environ.get("STUB_DELAY", "0.02"))
LOUDNESS = {"af_heart": 0.08, "am_adam": 0.6, "af_bella": 0.25}
_log_lock = threading.Lock()


def _log(text, voice):
    with _log_lock, open("spoken.log", "a", encoding="utf-8") as f:
        f.write(f"{voice}\t{text}\n")


def _vname(v):
    if isinstance(v, str):
        return "mix:" + os.path.basename(v)[:-3] if v.endswith(".pt") else v
    return "mix:array"


class _Voice:
    def __init__(self, name):
        self.name = _vname(name)

    def to(self, device):
        return self

    def __getitem__(self, i):
        return self


class _Model:
    device = "cpu"


class KPipeline:
    def __init__(self, lang_code="a", repo_id=None, model=True, device=None):
        self.model = _Model() if model else None
        self.calls = 0

    # ---- CPU half
    def g2p(self, text):
        time.sleep(G2P_DELAY)
        return text, [text]

    def en_tokenize(self, tokens):
        for t in tokens:
            words = t.split()
            for i in range(0, len(words), 60):           # long text is cut into chunks, like the real one
                chunk = " ".join(words[i:i + 60])
                yield chunk, chunk, None

    def load_voice(self, voice):
        return _Voice(voice)

    # ---- GPU half
    @staticmethod
    def infer(model, ps, pack, speed=1):
        time.sleep(GPU_DELAY)
        voice = getattr(pack, "name", "?")
        _log(ps, voice)
        words = len(ps.split())
        n = max(2400, int(words * 8400 / max(0.3, speed)))           # about 0.35 s per word, like a real voice
        if os.environ.get("STUB_TRUNCATE") and "glitch" in ps.lower() and words > 10:
            n = 8400                                                   # swallows the rest of the paragraph
        import torch
        if getattr(torch, "_half", 0) and os.environ.get("STUB_HALF_NAN"):
            n = 4800
            speech = np.full(n, np.nan, dtype="float32")
            return types.SimpleNamespace(audio=speech, pred_dur=None)
        if getattr(torch, "_half", 0):
            _log("[half]", voice)
        t = np.arange(n) / 24000
        amp = LOUDNESS.get(voice, 0.2)
        speech = (amp * np.sin(2 * np.pi * 220 * t)).astype("float32")
        pad = np.zeros(int(0.3 * 24000), dtype="float32")          # the model leaves silence around speech
        if os.environ.get("STUB_STALL") and "stalled" in ps.lower():
            half = len(speech) // 2
            speech = np.concatenate([speech[:half], np.zeros(48000, dtype="float32"), speech[half:]])
        audio = np.concatenate([pad, speech, pad])
        return types.SimpleNamespace(audio=audio, pred_dur=None)

    def __call__(self, text, voice=None, speed=1, split_pattern=r"\n+", model=None):
        pack = _Voice(voice)
        _, tokens = self.g2p(text)
        for gs, ps, _ in self.en_tokenize(tokens):
            out = KPipeline.infer(self.model, ps, pack, speed)
            yield gs, ps, out.audio
