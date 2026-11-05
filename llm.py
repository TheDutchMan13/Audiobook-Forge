"""A small language model that runs on this PC's graphics card, for jobs that need reading, not rules:
working out who says each line of a book, and suggesting how to say unusual names.

The model downloads to models\\ the first time (one time only) and nothing is sent anywhere.
It uses the graphics card, so it is never run while a book is being converted.

Command line (the window runs these):
  python llm.py suggest WORDS.json [--model best|fast] [--title T] [--author A]
      WORDS.json: [{"word": "Thanos", "example": "the sentence it is in"}, ...]
      prints "@suggest <word>\\t<respelling>" lines, then "@done"
"""
import argparse
import json
import os
import re
import sys
import time
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HOME", str(HERE / "models"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore")
if os.name == "nt":      # a program that cannot start must fail, not wait behind a hidden dialog
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:  # noqa: BLE001
        pass

# All free to use (Apache 2.0) and downloadable without an account.
MODELS = {
    "best": ("Qwen/Qwen3-4B-Instruct-2507", "Best: Qwen3 4B (8 GB download)", 10.0),
    "fast": ("Qwen/Qwen3-1.7B", "Faster: Qwen3 1.7B (3.5 GB, less accurate)", 5.0),
    "big": ("Qwen/Qwen3-8B", "Larger: Qwen3 8B (16 GB, slower, also uses system RAM)", 16.5),
}
SPLITS = {"big"}          # too big for the graphics card alone: the rest of the model sits in system RAM
INSTALL = 'uv pip install --python "C:\\Audiobook-Forge\\runtime\\python\\python.exe" --break-system-packages accelerate'

MODEL_LABELS = {key: label for key, (_, label, _) in MODELS.items()}


def out(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


class ModelError(Exception):
    """Something the person can act on: no space on the graphics card, no internet for the first download..."""


def downloaded(key):
    """True when the model is already on this PC (so starting it is quick and needs no internet)."""
    name = MODELS.get(key, (key,))[0]
    folder = Path(os.environ["HF_HOME"]) / "hub" / ("models--" + name.replace("/", "--")) / "snapshots"
    try:
        return any(any(p.glob("*.safetensors")) for p in folder.iterdir())
    except OSError:
        return False


class LocalModel:
    """Chat with a local model: answer = model.chat(system, user). Greedy (the same text gives the same answer)."""

    def __init__(self, key="best", say=out):
        name, _, need_gb = MODELS.get(key, (key, key, 8.0))
        import torch
        torch.backends.cudnn.enabled = False
        from transformers import AutoModelForCausalLM, AutoTokenizer
        try:
            from transformers.utils import logging as hf_logging
            hf_logging.set_verbosity_error()
            hf_logging.disable_progress_bar()
        except Exception:  # noqa: BLE001
            pass
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        split = key in SPLITS and self.device == "cuda"
        if split:
            try:
                import accelerate  # noqa: F401
            except ImportError as e:
                raise ModelError("The larger model needs one more small program (accelerate) to share the model between "
                                 f"the graphics card and system RAM. In PowerShell run:  {INSTALL}") from e
        if split:
            import checks
            try:
                free = torch.cuda.mem_get_info()[0] / 2**30
            except Exception:  # noqa: BLE001
                free = 12.0
            gpu = max(3.0, free - 3.0)                    # leave room for the conversation itself
            ram = (checks.ram_free() or 0) / 2**30
            need = need_gb - gpu + 3.0
            if ram < need:
                raise ModelError(f"The larger model needs about {need:.0f} GB of free system RAM next to the {gpu:.0f} GB on "
                                 f"the graphics card, and {ram:.0f} GB is free. Close other programs, or pick the other model.")
            self.max_memory = {0: f"{gpu:.1f}GiB", "cpu": f"{max(4.0, ram - 3.0):.0f}GiB"}
            say(f"The larger model is shared between the graphics card ({gpu:.0f} GB) and system RAM: slower than the others.")
        elif self.device == "cuda":
            try:
                free = torch.cuda.mem_get_info()[0] / 1e9
            except Exception:  # noqa: BLE001
                free = None
            if free is not None and free < need_gb * 0.8:
                raise ModelError(f"The graphics card has {free:.1f} GB free and this model needs about {need_gb:g} GB. "
                                 "Close other programs that use it (games, video editors) or pick the faster model.")
        else:
            say("No graphics card found: the model runs on the processor, which is very slow.")
        say("Loading the language model" + ("" if downloaded(key) else " (downloading it the first time, this takes a while)") + "...")
        try:
            self.tok = AutoTokenizer.from_pretrained(name)
            dtype = torch.bfloat16 if self.device == "cuda" else torch.float32
            kw = {"device_map": "auto", "max_memory": self.max_memory} if split else {}
            try:
                model = AutoModelForCausalLM.from_pretrained(name, dtype=dtype, **kw)
            except TypeError:                                   # older transformers
                model = AutoModelForCausalLM.from_pretrained(name, torch_dtype=dtype, **kw)
        except OSError as e:
            if not downloaded(key):
                raise ModelError("The language model could not be downloaded. The first time needs an internet "
                                 f"connection. ({str(e).splitlines()[0][:160]})") from e
            raise
        self.model = (model if split else model.to(self.device)).eval()      # a shared model is already placed
        self.name = name

    def chat(self, system, user, max_new_tokens=600):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            text = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        except TypeError:
            text = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        enc = self.tok(text, return_tensors="pt")
        enc = {k: v.to(self.device) for k, v in enc.items()}
        with self.torch.no_grad():
            ids = self.model.generate(**enc, max_new_tokens=max_new_tokens, do_sample=False,
                                      pad_token_id=self.tok.pad_token_id or self.tok.eos_token_id)
        answer = self.tok.decode(ids[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)
        return re.sub(r"<think>.*?(</think>|$)", "", answer, flags=re.S).strip()


# ------------------------------------------------------------------------------------------- pronunciations
SUGGEST_SYSTEM = ("You help an English text-to-speech voice say unusual words correctly. For each word you write a "
                  "respelling made of ordinary English syllables, in small letters, with a dash between syllables, "
                  "that an English reader would say correctly on the first try. Examples: Thanos = than-ohss, "
                  "Hermione = her-my-oh-nee, Siobhan = shiv-awn, Daenerys = deh-nair-iss, Nguyen = win. "
                  "Never use capitals, accents, IPA or other symbols.")


def suggest_prompt(items, title="", author=""):
    book = f" from the book {title}" + (f" by {author}" if author else "") if title else ""
    lines = [f"These words come{book}. If the author has said how a name is said, use that; otherwise give the most "
             "likely English pronunciation. Answer with one line per word, exactly as: word = respelling", ""]
    for it in items:
        ex = re.sub(r"\s+", " ", it.get("example", "")).strip()[:220]
        lines.append(f"{it['word']}" + (f"   (as in: {ex})" if ex else ""))
    return "\n".join(lines)


def clean_respelling(say, word):
    say = say.strip().strip("\"'`*.").strip()
    say = re.sub(r"\s*\(.*?\)\s*$", "", say)            # a trailing explanation
    say = say.lower()
    say = re.sub(r"[^a-z\- ']", "", say).strip(" -")
    say = re.sub(r"-{2,}", "-", say)
    if not say or say == word.lower() or len(say) > 3 * len(word) + 12:
        return None
    return say


def parse_suggestions(answer, words):
    want = {w.lower(): w for w in words}
    found = {}
    for line in answer.splitlines():
        m = re.match(r"^\s*(?:[-*\d.)]+\s*)?\**([^=:→]+?)\**\s*(?:=|:|→|->)\s*(.+?)\s*$", line)
        if not m:
            continue
        word = want.get(m.group(1).strip().strip("\"'").lower())
        if word and word not in found:
            say = clean_respelling(m.group(2), word)
            if say:
                found[word] = say
    return found


def suggest(model, items, title="", author="", batch=20, on_found=None):
    """{word: respelling} for [{"word", "example"}]."""
    result = {}
    for i in range(0, len(items), batch):
        group = items[i:i + batch]
        answer = model.chat(SUGGEST_SYSTEM, suggest_prompt(group, title, author), max_new_tokens=40 + 16 * len(group))
        got = parse_suggestions(answer, [it["word"] for it in group])
        for word, say in got.items():
            result[word] = say
            if on_found:
                on_found(word, say)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suggest")
    s.add_argument("words")
    s.add_argument("--model", default="best")
    s.add_argument("--title", default="")
    s.add_argument("--author", default="")
    args = ap.parse_args()
    if args.cmd == "suggest":
        items = json.loads(Path(args.words).read_text(encoding="utf-8"))
        try:
            model = LocalModel(args.model)
        except ModelError as e:
            out(f"@problem {e}")
            sys.exit(3)
        t = time.time()
        out(f"Suggesting {len(items)} word{'s' if len(items) != 1 else ''}...")
        result = suggest(model, items, args.title, args.author,
                         on_found=lambda w, say: out(f"@suggest {w}\t{say}"))
        out(f"@done {len(result)} {time.time() - t:.0f}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
        if "out of memory" in msg.lower():
            msg = "The graphics card ran out of memory. Close other programs that use it, or pick the faster model."
        out(f"@problem {msg}")
        sys.exit(1)
