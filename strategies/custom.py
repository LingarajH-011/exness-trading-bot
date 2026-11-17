"""Rule-based strategies built on the web page (no code needed)."""
from __future__ import annotations

import pandas as pd


def _hold(sid):
    return {"signal": "hold", "score": 0.0, "sl": 0.0, "tp": 0.0, "reason": "no signal",
            "strategy": sid, "style": "custom"}


def _atr(df, n=14):
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, 1e-12))


def _python_signal(df, spec, sid):
    from core.code_runner import RUNNER
    res, err = RUNNER.run(spec["code"], df.tail(500))
    out = _hold(sid)
    if err:
        out["error"] = err
        out["reason"] = "code error"
        return out
    if res["signal"] == "hold":
        return out
    price = float(df["close"].iloc[-1])
    atr = _atr(df).iloc[-1]
    sl, tp = res.get("sl"), res.get("tp")
    if (sl is None or tp is None) and (pd.isna(atr) or atr <= 0):
        return out
    buy = res["signal"] == "buy"
    if sl is None:
        sl = price - spec["sl"] * float(atr) if buy else price + spec["sl"] * float(atr)
    if tp is None:
        tp = price + spec["tp"] * float(atr) if buy else price - spec["tp"] * float(atr)
    return {"signal": res["signal"], "score": 0.5, "sl": round(float(sl), 5), "tp": round(float(tp), 5),
            "reason": f"{spec['name']}: {res.get('reason') or res['signal']}", "strategy": sid, "style": "custom"}


def custom_signal(df, spec, sid="custom"):
    """Look at the last two closed candles and return the same dict shape as the built-ins."""
    need = max(60, int(spec.get("p2") or 0) * 2, int(spec.get("p1") or 0) * 2)
    if df is None or len(df) < need:
        return _hold(sid)
    close = df["close"]
    rule = spec["rule"]
    if rule == "python":
        return _python_signal(df, spec, sid)
    side, why = None, ""
    if rule == "ema_cross":
        fast = close.ewm(span=int(spec["p1"]), adjust=False).mean()
        slow = close.ewm(span=int(spec["p2"]), adjust=False).mean()
        if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
            side, why = "buy", f"EMA{int(spec['p1'])} crossed above EMA{int(spec['p2'])}"
        elif fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
            side, why = "sell", f"EMA{int(spec['p1'])} crossed below EMA{int(spec['p2'])}"
    elif rule == "rsi_reversal":
        r = _rsi(close)
        if r.iloc[-2] <= spec["p1"] < r.iloc[-1]:
            side, why = "buy", f"RSI back above {spec['p1']:g}"
        elif r.iloc[-2] >= spec["p2"] > r.iloc[-1]:
            side, why = "sell", f"RSI back below {spec['p2']:g}"
    elif rule == "breakout":
        n = int(spec["p1"])
        hi = df["high"].iloc[-n - 1:-1].max()
        lo = df["low"].iloc[-n - 1:-1].min()
        if close.iloc[-1] > hi:
            side, why = "buy", f"closed above {n}-candle high"
        elif close.iloc[-1] < lo:
            side, why = "sell", f"closed below {n}-candle low"
    atr = _atr(df).iloc[-1]
    if side is None or pd.isna(atr) or atr <= 0:
        return _hold(sid)
    price = float(close.iloc[-1])
    sl_d, tp_d = spec["sl"] * float(atr), spec["tp"] * float(atr)
    sl, tp = (price - sl_d, price + tp_d) if side == "buy" else (price + sl_d, price - tp_d)
    return {"signal": side, "score": 0.5, "sl": round(sl, 5), "tp": round(tp, 5),
            "reason": f"{spec['name']}: {why}", "strategy": sid, "style": "custom"}
