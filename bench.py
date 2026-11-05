import sys
import time
import warnings

warnings.filterwarnings("ignore")

import numpy as np
import torch
from kokoro import KPipeline

TEXT = (
    "The old lighthouse stood at the edge of the cliff, its white paint peeling in the salt wind. "
    "Every evening the keeper climbed the spiral stairs, lit the great lamp, and watched the ships "
    "move slowly along the horizon. Nobody in the village could remember a time before the light, "
    "and nobody wanted to. On stormy nights the beam swept across the water like a slow, patient "
    "heartbeat, and the fishermen steered home by it. When the keeper finally retired, the whole "
    "town gathered on the rocks to say goodbye, and the lamp burned a little brighter that night."
)


def run(device, passes):
    pipe = KPipeline(lang_code="a", device=device)
    for n in range(1, passes + 1):
        start = time.time()
        total = 0
        for _, _, audio in pipe(TEXT, voice="af_heart"):
            a = audio.cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio)
            total += len(a)
        if device == "cuda":
            torch.cuda.synchronize()
        took = time.time() - start
        secs = total / 24000
        print(f"{device} pass {n}: {secs:.1f}s audio in {took:.1f}s -> {secs / took:.1f}x realtime", flush=True)


if torch.cuda.is_available():
    run("cuda", 3)
if "--cpu" in sys.argv:
    run("cpu", 2)
