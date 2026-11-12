import contextlib, os, pickle, types
import numpy as np
backends = types.SimpleNamespace(cudnn=types.SimpleNamespace(enabled=True))
cuda = types.SimpleNamespace(is_available=lambda: bool(os.environ.get("STUB_CUDA")), get_device_name=lambda i: "stub GPU",
                             mem_get_info=lambda: (12 * 2**30, 16 * 2**30), empty_cache=lambda: None)
version = types.SimpleNamespace(hip=None)
__version__ = "stub"
float16 = "float16"
bfloat16 = "bfloat16"
float32 = "float32"
FloatTensor = np.ndarray
_half = 0


@contextlib.contextmanager
def autocast(device_type="cuda", dtype=None):
    global _half
    _half += 1
    try:
        yield
    finally:
        _half -= 1


def load(f, weights_only=True):
    with open(f, "rb") as fh:
        return pickle.load(fh)


def save(obj, f):
    with open(f, "wb") as fh:
        pickle.dump(obj, fh)

float32 = "float32"


@contextlib.contextmanager
def no_grad():
    yield
