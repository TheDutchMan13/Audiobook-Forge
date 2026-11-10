"""Stand-in for the tour: 'hears' the same short phrase for every clip."""
import types


class _Feats:
    def __init__(self, n):
        self.n = n

    def to(self, *a, **k):
        return self


class WhisperProcessor:
    @classmethod
    def from_pretrained(cls, name):
        return cls()

    def __call__(self, group, sampling_rate=16000, return_tensors="pt"):
        return types.SimpleNamespace(input_features=_Feats(len(group)))

    def batch_decode(self, ids, skip_special_tokens=True):
        return ["the quiet harbor" for _ in ids]


class WhisperForConditionalGeneration:
    @classmethod
    def from_pretrained(cls, name):
        return cls()

    def to(self, *a, **k):
        return self

    def eval(self):
        return self

    def generate(self, feats, max_new_tokens=200):
        return [[0]] * feats.n


# ---- a chat model for the tour: names "Tony" for lines that mention him, otherwise Natasha; respellings "<word>-ah"
import re as _re


class _Ids(list):
    shape = (1, 0)

    def to(self, *a, **k):
        return self


class _Answer:
    def __init__(self, text):
        self.text = text

    def __getitem__(self, k):
        return self


class AutoTokenizer:
    pad_token_id = 0
    eos_token_id = 0

    @classmethod
    def from_pretrained(cls, name):
        return cls()

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **k):
        return messages[-1]["content"]

    def __call__(self, text, return_tensors="pt"):
        ids = _Ids()
        ids.prompt = text
        return {"input_ids": ids}

    def decode(self, answer, skip_special_tokens=True):
        text = answer.text
        lines = []
        for n, para in _re.findall(r"^\[(\d+)\] (.*)$", text, _re.M):
            lines.append(f"{n}: Tony, man" if "Tony" in para or "never" in para else f"{n}: Natasha, woman")
        for word in _re.findall(r"^(\S+)   \(as in", text, _re.M):
            lines.append(f"{word} = {word.lower()}-ah")
        return "\n".join(lines)


class AutoModelForCausalLM:
    @classmethod
    def from_pretrained(cls, name, **k):
        return cls()

    def to(self, *a, **k):
        return self

    def eval(self):
        return self

    def generate(self, input_ids=None, **k):
        import time
        time.sleep(0.3)
        return [_Answer(input_ids.prompt)]
