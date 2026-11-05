import time

import soundfile as sf
import torch
from kokoro import KPipeline

device = "cuda" if torch.cuda.is_available() else "cpu"
print("device:", device, torch.cuda.get_device_name(0) if device == "cuda" else "")

pipeline = KPipeline(lang_code="a", device=device)  # 'a' = American English

text = (
    "This is a test of the Audiobook Forge. "
    "If you can hear this sentence, text to speech is running on your graphics card."
)

start = time.time()
chunks = []
for _, _, audio in pipeline(text, voice="af_heart"):
    chunks.append(audio)

import numpy as np
audio = np.concatenate([c.cpu().numpy() if hasattr(c, "cpu") else c for c in chunks])
sf.write("tts_test.wav", audio, 24000)
print(f"wrote tts_test.wav ({len(audio) / 24000:.1f}s of audio in {time.time() - start:.1f}s)")
