"""Which strategies are switched on, their editable settings, and user-made strategies.

Saved as plain JSON in data/strategies.json (nothing secret in it).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4"]
RULES = {
    "ema_cross": "EMA crossover: buy when the fast EMA crosses above the slow EMA, sell on the reverse",
    "rsi_reversal": "RSI reversal: buy when RSI rises back above the low level, sell when it falls back below the high level",
    "python": "Python code: write your own signal(df) function",
    "breakout": "Breakout: buy when price closes above the recent high, sell when it closes below the recent low",
}
MAX_CUSTOM = 10

# Built-in strategies. `unit` says what sl/tp mean: pips (scalping) or ATR multiples.
BUILTIN = {
    "scalping":  {"name": "Scalping",  "tf": "M5",  "unit": "pips", "about": "Quick 5-minute trades in busy sessions."},
    "intraday":  {"name": "Intraday",  "tf": "M15", "unit": "ATR",  "about": "Trend pullbacks on 15-minute charts, London to New York."},
    "swing":     {"name": "Swing",     "tf": "H1",  "unit": "ATR",  "about": "Multi-day trend trades on hourly charts."},
    "news":      {"name": "News spike", "tf": "M5", "unit": "ATR",  "about": "Jumps on sudden large moves."},
    "whale":     {"name": "Whale",     "tf": "M15", "unit": "ATR",  "about": "Follows unusually large volume."},
    "fibonacci": {"name": "Fibonacci", "tf": "H1",  "unit": "ATR",  "about": "Entries at Fibonacci retracement levels."},
}
_SYMBOL = re.compile(r"^[A-Za-z0-9._]{3,12}$")
_NAME = re.compile(r"^[\w .,&()+/'-]{1,40}$")


def _num(raw, lo, hi, label, errors, integer=False):
    try:
        v = int(str(raw).strip()) if integer else float(str(raw).strip())
    except ValueError:
        errors.append(f"{label} must be a number.")
        return None
    if not lo <= v <= hi:
        errors.append(f"{label} must be between {lo} and {hi}.")
        return None
    return v


def validate_tuning(form, unit):
    """Risk / stop-loss / take-profit, shared by built-in and custom strategies."""
    errors, out = [], {}
    risk = _num(form.get("risk_pct", ""), 0.1, 2, "Risk per trade (%)", errors)
    sl = _num(form.get("sl", ""), 0.5, 100 if unit == "pips" else 10, "Stop loss", errors)
    tp = _num(form.get("tp", ""), 0.5, 200 if unit == "pips" else 20, "Take profit", errors)
    if risk is not None:
        out["risk_pct"] = risk
    if sl is not None:
        out["sl"] = sl
    if tp is not None:
        out["tp"] = tp
    if sl is not None and tp is not None and tp < sl:
        errors.append("Take profit should not be smaller than the stop loss.")
    return out, errors


def validate_custom(form):
    errors = []
    name = (form.get("name") or "").strip()
    if not _NAME.match(name):
        errors.append("Give the strategy a name (up to 40 letters, numbers or basic punctuation).")
    symbols = [x.strip().upper() for x in (form.get("symbols") or "").split(",") if x.strip()]
    if not symbols or len(symbols) > 5 or not all(_SYMBOL.match(x) for x in symbols):
        errors.append("Symbols: one to five names separated by commas, e.g. EURUSD, XAUUSD.")
    tf = form.get("timeframe", "")
    if tf not in TIMEFRAMES:
        errors.append("Pick a timeframe from the list.")
    rule = form.get("rule", "")
    if rule not in RULES:
        errors.append("Pick a rule from the list.")
    params = {}
    code = None
    if rule == "python":
        from core.code_worker import check_code
        code = form.get("code", "").replace("\r\n", "\n")
        errors += check_code(code)
        params = {"p1": 0, "p2": 0, "code": code}
    elif rule == "ema_cross":
        fast = _num(form.get("p1", ""), 2, 100, "Fast EMA", errors, True)
        slow = _num(form.get("p2", ""), 3, 300, "Slow EMA", errors, True)
        if fast and slow and fast >= slow:
            errors.append("The fast EMA must be shorter than the slow EMA.")
        params = {"p1": fast, "p2": slow}
    elif rule == "rsi_reversal":
        lo = _num(form.get("p1", ""), 5, 45, "RSI low level", errors)
        hi = _num(form.get("p2", ""), 55, 95, "RSI high level", errors)
        params = {"p1": lo, "p2": hi}
    elif rule == "breakout":
        n = _num(form.get("p1", ""), 5, 200, "Look-back candles", errors, True)
        params = {"p1": n, "p2": 0}
    tune, terr = validate_tuning(form, "ATR")
    errors += terr
    spec = {"name": name, "symbols": symbols, "timeframe": tf, "rule": rule, **params, **tune}
    return spec, errors


class StrategyStore:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.file = self.dir / "strategies.json"
        self._lock = threading.Lock()

    # -- persistence
    def _read(self):
        try:
            data = json.loads(self.file.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("enabled", {})
        data.setdefault("overrides", {})
        data.setdefault("custom", {})
        data.setdefault("next_id", 1)
        return data

    def _write(self, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.file)

    # -- reading
    def config(self):
        """Everything the engine needs, as plain data."""
        with self._lock:
            d = self._read()
        enabled = {k: bool(d["enabled"].get(k, True)) for k in list(BUILTIN) + list(d["custom"])}
        return {"enabled": enabled, "overrides": d["overrides"], "custom": d["custom"]}

    def listing(self):
        """Rows for the strategies page."""
        cfg = self.config()
        rows = []
        for sid, meta in BUILTIN.items():
            rows.append({"id": sid, "name": meta["name"], "kind": "built-in", "about": meta["about"],
                         "tf": meta["tf"], "enabled": cfg["enabled"][sid],
                         "edited": sid in cfg["overrides"]})
        for sid, spec in cfg["custom"].items():
            rows.append({"id": sid, "name": spec["name"], "kind": "custom", "tf": spec["timeframe"],
                         "about": RULES[spec["rule"]].split(":")[0] + " on " + ", ".join(spec["symbols"]),
                         "code": spec["rule"] == "python",
                         "enabled": cfg["enabled"][sid], "edited": False})
        return rows

    # -- changing
    def set_enabled(self, ids):
        ids = set(ids)
        with self._lock:
            d = self._read()
            for sid in list(BUILTIN) + list(d["custom"]):
                d["enabled"][sid] = sid in ids
            self._write(d)

    def save_override(self, sid, tune):
        with self._lock:
            d = self._read()
            d["overrides"][sid] = tune
            self._write(d)

    def reset_override(self, sid):
        with self._lock:
            d = self._read()
            d["overrides"].pop(sid, None)
            self._write(d)

    def save_custom(self, spec, sid=None):
        """Create (sid=None) or update. Returns the id, or None if the limit is hit / id unknown."""
        with self._lock:
            d = self._read()
            if sid is None:
                if len(d["custom"]) >= MAX_CUSTOM:
                    return None
                sid = f"cx{d['next_id']}"
                d["next_id"] += 1
                d["enabled"][sid] = False      # new strategies start switched off: tick them on yourself
            elif sid not in d["custom"]:
                return None
            d["custom"][sid] = spec
            self._write(d)
            return sid

    def delete_custom(self, sid):
        with self._lock:
            d = self._read()
            if sid not in d["custom"]:
                return False
            d["custom"].pop(sid)
            d["enabled"].pop(sid, None)
            self._write(d)
            return True
