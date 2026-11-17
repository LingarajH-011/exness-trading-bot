"""Runs user-written strategy code in a separate, killable process.

Two jobs in one file so the web app and the worker agree on the rules:
  * check_code(code)  -> list of problems (used when saving a strategy)
  * a tiny worker loop (python core/code_worker.py): reads one JSON request per line on stdin,
    runs the user's signal(df) function, writes one JSON answer per line on stdout.

This is a best-effort guard rail, NOT a hard security sandbox. Only run code you wrote or trust.
"""
from __future__ import annotations

import ast
import json
import math
import sys

MAX_CODE_CHARS = 8000

_BANNED_NAMES = {"exec", "eval", "compile", "open", "getattr", "setattr", "delattr", "globals", "locals",
                 "vars", "input", "breakpoint", "help", "dir", "type", "object", "super", "memoryview",
                 "classmethod", "staticmethod", "property", "__import__", "quit", "exit"}
_BANNED_ATTRS = {"format", "format_map", "plot", "eval", "query", "style", "system", "popen", "open", "load",
                 "loads", "dump", "dumps", "save", "savetxt", "tofile", "fromfile", "memmap", "pipe"}
_BANNED_ATTR_PREFIXES = ("_", "read_", "gi_", "f_", "cr_", "tb_", "ag_", "co_")
_ALLOWED_TO = {"to_numpy", "to_list", "tolist"}
_BANNED_NODES = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Global, ast.Nonlocal,
                 ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith, ast.Yield, ast.YieldFrom)


def check_code(code: str) -> list[str]:
    """Return a list of human-readable problems; empty means the code may be saved."""
    if not code or not code.strip():
        return ["Write some code: define a function called signal(df)."]
    if len(code) > MAX_CODE_CHARS:
        return [f"The code is too long (limit {MAX_CODE_CHARS} characters)."]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error on line {e.lineno}: {e.msg}"]
    problems = []
    has_signal = False
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, _BANNED_NODES):
            kind = "import" if isinstance(node, (ast.Import, ast.ImportFrom)) else type(node).__name__
            problems.append(f"Line {line}: '{kind}' is not allowed. ema, sma, rsi, atr, np and pd are ready to use without importing.")
        elif isinstance(node, ast.Name) and (node.id in _BANNED_NAMES or node.id.startswith("__")):
            problems.append(f"Line {line}: the name '{node.id}' is not allowed.")
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if (a in _BANNED_ATTRS or a.startswith(_BANNED_ATTR_PREFIXES)
                    or (a.startswith("to_") and a not in _ALLOWED_TO)):
                problems.append(f"Line {line}: '.{a}' is not allowed.")
        elif isinstance(node, ast.FunctionDef) and node.name == "signal" and node.col_offset == 0:
            has_signal = True
    if not has_signal:
        problems.append("Define a function at the top level called signal(df).")
    return sorted(set(problems))


# ---------------------------------------------------------------------------- worker side
def _build_namespace():
    import numpy as np
    import pandas as pd
    from types import SimpleNamespace

    def ema(s, n):
        return s.ewm(span=int(n), adjust=False).mean()

    def sma(s, n):
        return s.rolling(int(n)).mean()

    def rsi(s, n=14):
        d = s.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, 1e-12))

    def atr(df, n=14):
        prev = df["close"].shift()
        tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
        return tr.rolling(int(n)).mean()

    def crossed_above(a, b):
        return bool(a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1])

    def crossed_below(a, b):
        return bool(a.iloc[-2] >= b.iloc[-2] and a.iloc[-1] < b.iloc[-1])

    safe_np = SimpleNamespace(**{k: getattr(np, k) for k in (
        "sqrt", "log", "exp", "abs", "where", "nan", "isnan", "mean", "std", "maximum", "minimum",
        "sign", "floor", "ceil", "array", "inf", "pi", "clip", "nanmean", "nanmax", "nanmin", "sum", "max", "min")})
    safe_pd = SimpleNamespace(Series=pd.Series, DataFrame=pd.DataFrame, concat=pd.concat,
                              isna=pd.isna, notna=pd.notna)
    builtins = {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
                for k in ("len", "range", "min", "max", "abs", "sum", "round", "float", "int", "bool", "str",
                          "list", "dict", "tuple", "set", "zip", "enumerate", "sorted", "reversed", "any", "all",
                          "isinstance", "True", "False", "None", "ValueError", "Exception")}
    return {"__builtins__": builtins, "np": safe_np, "pd": safe_pd, "ema": ema, "sma": sma, "rsi": rsi,
            "atr": atr, "crossed_above": crossed_above, "crossed_below": crossed_below, "math": math}


def _normalise(raw, price):
    if isinstance(raw, str):
        raw = {"signal": raw}
    if not isinstance(raw, dict):
        raise ValueError("signal(df) must return 'buy', 'sell', 'hold' or a dict like {'signal': 'buy', 'sl': 1.1, 'tp': 1.2}")
    side = str(raw.get("signal", "hold")).lower()
    if side not in ("buy", "sell", "hold"):
        raise ValueError(f"unknown signal '{side}': use 'buy', 'sell' or 'hold'")
    out = {"signal": side, "reason": str(raw.get("reason", ""))[:120]}
    for k in ("sl", "tp"):
        if raw.get(k) is not None:
            v = float(raw[k])
            if not math.isfinite(v) or v <= 0:
                raise ValueError(f"'{k}' must be a positive price")
            out[k] = v
    if side == "buy" and ((out.get("sl") or 0) >= price or (out.get("tp") or 1e18) <= price):
        raise ValueError("for a buy, sl must be below the price and tp above it")
    if side == "sell" and ((out.get("sl") or 1e18) <= price or (out.get("tp") or 0) >= price):
        raise ValueError("for a sell, sl must be above the price and tp below it")
    return out


def _handle(req, cache):
    import pandas as pd
    code = req["code"]
    problems = check_code(code)
    if problems:
        return {"ok": False, "error": problems[0]}
    if code not in cache:
        ns = _build_namespace()
        exec(compile(code, "<your strategy>", "exec"), ns)
        cache.clear()
        cache[code] = ns["signal"]
    df = pd.DataFrame(req["rows"])
    price = float(df["close"].iloc[-1])
    raw = cache[code](df)
    return {"ok": True, "result": _normalise(raw, price)}


def main():
    try:                                    # best-effort memory cap where the OS supports it
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    except Exception:
        pass
    real_out = sys.stdout
    sys.stdout = sys.stderr                 # stray prints must not corrupt the protocol
    cache: dict = {}
    for line in sys.stdin:
        try:
            ans = _handle(json.loads(line), cache)
        except Exception as e:              # the user's code failing is an answer, not a crash
            ans = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        real_out.write(json.dumps(ans) + "\n")
        real_out.flush()


if __name__ == "__main__":
    main()
