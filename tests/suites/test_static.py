"""Static checks over every program file: no undefined names (a missing import, a typo), and the graphics-card
speed setting (cuDNN off) still applied wherever a model is loaded. Needs pyflakes for the first part
(pip install pyflakes); skipped with a note when it is missing."""
import re, sys, types
from pathlib import Path
res = []
def ok(c, m): res.append(bool(c)); print(("PASS " if c else "FAIL ") + m, flush=True)
app = [p for p in Path(".").glob("*.py") if not p.name.startswith(("test_", "run_", "t_", "tour", "kokoro", "torch",
                                                                       "huggingface_hub", "bench", "tts_test", "check_gpu"))]
try:
    from pyflakes.api import check
    from pyflakes.reporter import Reporter
    import io
    for p in sorted(app):
        out, err = io.StringIO(), io.StringIO()
        check(p.read_text(encoding="utf-8"), str(p), Reporter(out, err))
        bad = [l for l in out.getvalue().splitlines() if re.search(r"undefined name|referenced before assignment|"
                                                                    r"redefinition of unused .* from line|syntax", l)]
        ok(not bad and not err.getvalue(), f"{p.name}: no undefined names" + (f": {bad[:3]}" if bad else ""))
except ImportError:
    print("pyflakes is not installed; undefined-name checks skipped")
import ast
def shadowed(tree):
    """Names imported inside a function but used in it before that import (Python then treats the name as local
    in the whole function: UnboundLocalError at the first use). pyflakes misses this when the name is also
    imported at the top of the file."""
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        inner = {}
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    name = (a.asname or a.name).split(".")[0]
                    inner[name] = min(inner.get(name, 10 ** 9), node.lineno)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in inner and node.lineno < inner[node.id]:
                found.append(f"{fn.name}: '{node.id}' used on line {node.lineno}, imported on line {inner[node.id]}")
    return found
for p in sorted(app):
    bad = shadowed(ast.parse(p.read_text(encoding="utf-8")))
    ok(not bad, f"{p.name}: no name used before an import of it in the same function" + (f": {bad[:3]}" if bad else ""))
import checks
fake = types.SimpleNamespace(backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(enabled=True)))
checks.fast_gpu(fake)
ok(fake.backends.cudnn.enabled is False, "checks.fast_gpu turns cuDNN off (what makes the RX 9070 XT fast)")
for p in app:
    s = p.read_text(encoding="utf-8")
    if re.search(r"from kokoro import|from_pretrained\(", s):
        ok("checks.fast_gpu(torch)" in s or "cudnn.enabled = False" in s, f"{p.name} turns cuDNN off before using the card")
print(f"{sum(res)} passed, {len(res) - sum(res)} failed")
