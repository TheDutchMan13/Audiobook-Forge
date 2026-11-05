"""Compare ways of avoiding MIOpen re-tuning on varied paragraph lengths.

    python bench2.py fast     --chapter 31
    python bench2.py nocudnn  --chapter 30
Use a different chapter per mode so every paragraph length is new to the GPU cache.
"""
import os
import sys
import time
import warnings

mode = sys.argv[1]
chapter_no = int(sys.argv[sys.argv.index("--chapter") + 1])
N_PARAS = 5

if mode == "fast":
    os.environ["MIOPEN_FIND_MODE"] = "FAST"
warnings.filterwarnings("ignore")

import numpy as np
import torch
from kokoro import KPipeline
from epub_reader import read_epub

if mode == "nocudnn":
    torch.backends.cudnn.enabled = False

_, chapters = read_epub("MCU - Joanna Robinson.epub")
paras = [p for p in chapters[chapter_no - 1].paragraphs if len(p) > 200][:N_PARAS]
print(f"mode={mode} chapter={chapter_no} paragraphs={len(paras)}", flush=True)

pipe = KPipeline(lang_code="a", device="cuda")
total_audio = total_time = 0.0
for i, p in enumerate(paras, 1):
    t = time.time()
    n = 0
    for _, _, a in pipe(p, voice="af_heart"):
        n += len(a.cpu().numpy() if hasattr(a, "cpu") else a)
    torch.cuda.synchronize()
    took, secs = time.time() - t, n / 24000
    total_audio += secs
    total_time += took
    print(f"  para {i}: {secs:.1f}s audio in {took:.1f}s ({secs / took:.1f}x)", flush=True)
print(f"TOTAL {mode}: {total_audio:.0f}s audio in {total_time:.0f}s -> {total_audio / total_time:.1f}x realtime", flush=True)
