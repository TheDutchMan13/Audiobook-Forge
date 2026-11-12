"""Stand-in: 'downloads' a voice by making a small array file whose values depend on the voice name."""
import hashlib, os, pickle
from pathlib import Path
import numpy as np


def hf_hub_download(repo_id, filename, **kw):
    target = Path(os.environ.get("HF_HOME", "models")) / "stubvoices" / Path(filename).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        seed = int(hashlib.sha1(filename.encode()).hexdigest()[:6], 16) % 1000 / 1000
        with open(target, "wb") as fh:
            pickle.dump(np.full((510, 1, 256), seed, dtype=np.float32), fh)
    return str(target)
