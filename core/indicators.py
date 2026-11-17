"""
=============================================================================
  INDICATORS — Technical indicators used by strategies.
  All functions take a DataFrame with columns [open, high, low, close, volume]
  and return a DataFrame with added indicator columns.
=============================================================================
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


def compute_all(df: pd.DataFrame,
                ema_fast: int = 9, ema_slow: int = 21, ema_trend: int = 50,
                rsi_period: int = 14, atr_period: int = 14,
                bb_period: int = 20, bb_std: float = 2.0) -> pd.DataFrame:
    """
    Compute all indicators needed by the default strategy stack.
    Returns a new DataFrame with indicator columns appended.
    """
    if df.empty or len(df) < max(ema_trend, 50):
        return df.copy()

    df = df.copy()
    c, h, l, v = df.close, df.high, df.low, df.volume

    # ── EMAs ────────────────────────────────────────────────────────
    df["ema_fast"]  = _ema(c, ema_fast)
    df["ema_slow"]  = _ema(c, ema_slow)
    df["ema_trend"] = _ema(c, ema_trend)
    df["ema_200"]   = _ema(c, 200) if len(df) > 200 else np.nan

    # ── RSI ─────────────────────────────────────────────────────────
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(rsi_period).mean()
    loss  = (-delta.clip(upper=0)).rolling(rsi_period).mean()
    rs    = gain / loss.replace(0, 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ── MACD ────────────────────────────────────────────────────────
    macd_line   = _ema(c, 12) - _ema(c, 26)
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    df["macd"]      = macd_line
    df["macd_sig"]  = signal_line
    df["macd_hist"] = macd_line - signal_line

    # ── ATR ─────────────────────────────────────────────────────────
    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=atr_period, adjust=False).mean()

    # ── Bollinger Bands ─────────────────────────────────────────────
    bb_mid = _sma(c, bb_period)
    bb_sd  = c.rolling(bb_period).std()
    df["bb_mid"] = bb_mid
    df["bb_hi"]  = bb_mid + bb_std * bb_sd
    df["bb_lo"]  = bb_mid - bb_std * bb_sd
    df["bb_width"] = (df["bb_hi"] - df["bb_lo"]) / bb_mid

    # ── VWAP (session) ──────────────────────────────────────────────
    tp = (h + l + c) / 3
    df["vwap"] = (tp * v).cumsum() / v.cumsum().replace(0, 1e-9)

    # ── Volume ratio ────────────────────────────────────────────────
    df["vol_sma"]   = _sma(v, 20)
    df["vol_ratio"] = v / (df["vol_sma"] + 1e-9)

    # ── Z-score (for mean reversion) ────────────────────────────────
    df["z_close"] = (c - _sma(c, 20)) / (c.rolling(20).std() + 1e-9)

    # ── Stochastic RSI ──────────────────────────────────────────────
    rsi_min = df["rsi"].rolling(14).min()
    rsi_max = df["rsi"].rolling(14).max()
    df["stoch_rsi"] = (df["rsi"] - rsi_min) / (rsi_max - rsi_min + 1e-9)

    # ── Returns and realized vol ────────────────────────────────────
    df["ret"]  = c.pct_change()
    df["rvol"] = df["ret"].rolling(20).std() * np.sqrt(252 * 24)  # annualized

    return df


def pip_value(symbol: str, price: float = 0) -> float:
    """
    Return the pip size for a symbol.
    Majors (non-JPY): 0.0001
    JPY pairs:        0.01
    XAUUSD:           0.10
    Indices/others:   depends, default to 1 point.
    """
    symbol = symbol.upper().rstrip("M")  # Strip Exness 'm' suffix
    if "JPY" in symbol:
        return 0.01
    if symbol.startswith("XAU") or symbol.startswith("GOLD"):
        return 0.10
    if symbol.startswith("XAG") or symbol.startswith("SILVER"):
        return 0.01
    # Major forex pairs
    if len(symbol) == 6:
        return 0.0001
    return 0.01  # sensible default
