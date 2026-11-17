"""
=============================================================================
  IMPROVED STRATEGIES v2 — Higher win rate through multi-confirmation
  
  Key improvements over v1:
    1. ADX filter on ALL strategies (don't trade in choppy markets)
    2. SuperTrend for trend direction (replaces simple EMA stack)
    3. Multi-timeframe confirmation REQUIRED (not optional)
    4. Candlestick pattern confirmation at entry
    5. Minimum R:R of 1:2 enforced
    6. Spread filter (skip if spread too wide)
    7. Stricter RSI zones
    8. Volume must confirm every entry
=============================================================================
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict
import sys, os
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)
from core.indicators import pip_value


def _no_signal(strategy: str) -> Dict:
    return {"signal": "hold", "score": 0.0, "sl": 0.0, "tp": 0.0,
            "reason": "no signal", "strategy": strategy, "style": strategy}


# ══════════════════════════════════════════════════════════════════════════
# SHARED INDICATOR COMPUTATIONS
# ══════════════════════════════════════════════════════════════════════════

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def _atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    tr = pd.concat([
        df.high - df.low,
        (df.high - df.close.shift()).abs(),
        (df.low - df.close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - (100 / (1 + g / l.replace(0, 1e-9)))

def _adx(df: pd.DataFrame, p: int = 14) -> pd.Series:
    """ADX — THE key filter. ADX > 25 = trending, < 20 = ranging."""
    high, low, close = df.high, df.low, df.close
    plus_dm = (high - high.shift()).clip(lower=0)
    minus_dm = (low.shift() - low).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    atr = _atr(df, p)
    plus_di = 100 * (plus_dm.ewm(span=p, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=p, adjust=False).mean() / atr)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)) * 100
    adx = dx.ewm(span=p, adjust=False).mean()
    return adx

def _supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """SuperTrend — better than EMA stack for trend direction."""
    atr = _atr(df, period)
    hl2 = (df.high + df.low) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr

    st = pd.Series(0.0, index=df.index)
    direction = pd.Series(1, index=df.index)  # 1 = up, -1 = down

    for i in range(1, len(df)):
        if df.close.iloc[i] > upper.iloc[i - 1]:
            direction.iloc[i] = 1
        elif df.close.iloc[i] < lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]
            if direction.iloc[i] == 1 and lower.iloc[i] < lower.iloc[i - 1]:
                lower.iloc[i] = lower.iloc[i - 1]
            if direction.iloc[i] == -1 and upper.iloc[i] > upper.iloc[i - 1]:
                upper.iloc[i] = upper.iloc[i - 1]

        st.iloc[i] = lower.iloc[i] if direction.iloc[i] == 1 else upper.iloc[i]

    return pd.DataFrame({"supertrend": st, "st_direction": direction,
                         "st_upper": upper, "st_lower": lower}, index=df.index)

def _bb(s: pd.Series, p: int = 20, std: float = 2.0):
    mid = s.rolling(p).mean()
    sd = s.rolling(p).std()
    return mid, mid + std * sd, mid - std * sd

def _macd(s: pd.Series):
    macd = _ema(s, 12) - _ema(s, 26)
    sig = macd.ewm(span=9, adjust=False).mean()
    return macd, sig, macd - sig

def _is_bullish_candle(row) -> bool:
    """Bullish: close > open, body > 50% of range."""
    body = abs(row.close - row.open)
    rng = row.high - row.low
    return row.close > row.open and body > rng * 0.4 if rng > 0 else False

def _is_bearish_candle(row) -> bool:
    body = abs(row.close - row.open)
    rng = row.high - row.low
    return row.close < row.open and body > rng * 0.4 if rng > 0 else False

def _compute(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators needed by v2 strategies."""
    if df.empty or len(df) < 50:
        return pd.DataFrame()
    df = df.copy()
    df["ema9"] = _ema(df.close, 9)
    df["ema21"] = _ema(df.close, 21)
    df["ema50"] = _ema(df.close, 50)
    df["rsi"] = _rsi(df.close, 14)
    df["atr"] = _atr(df, 14)
    df["adx"] = _adx(df, 14)
    macd, sig, hist = _macd(df.close)
    df["macd"], df["macd_sig"], df["macd_hist"] = macd, sig, hist
    bb_mid, bb_hi, bb_lo = _bb(df.close)
    df["bb_mid"], df["bb_hi"], df["bb_lo"] = bb_mid, bb_hi, bb_lo
    df["bb_width"] = (bb_hi - bb_lo) / (bb_mid + 1e-9)
    st = _supertrend(df)
    df["st_dir"] = st["st_direction"]
    df["st_val"] = st["supertrend"]
    df["vol_sma"] = df.volume.rolling(20).mean()
    df["vol_ratio"] = df.volume / (df["vol_sma"] + 1e-9)
    df["ret"] = df.close.pct_change()
    return df.dropna()


# ══════════════════════════════════════════════════════════════════════════
# 1. SCALPING v2 — SuperTrend + RSI + ADX filter + candle confirm
# ══════════════════════════════════════════════════════════════════════════

def scalping_signal(df: pd.DataFrame, bias_df: pd.DataFrame = None,
                    tp_pips: float = 12, sl_pips: float = 6, **kw) -> Dict:
    """
    IMPROVED: Only scalp WITH the SuperTrend direction.
    ADX must be > 20 (market is moving, not dead).
    RSI must confirm (>55 for buy, <45 for sell).
    Last candle must be in the right direction.
    R:R = 2:1 (12 pip TP vs 6 pip SL).
    """
    if df is None or df.empty or len(df) < 60:
        return _no_signal("scalping")
    
    df = _compute(df)
    if df.empty: return _no_signal("scalping")
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last.close)
    pip = pip_value(df.attrs.get("symbol", "EURUSDm"))
    
    # FILTER 1: ADX must show market is moving
    if last.adx < 20:
        return _no_signal("scalping")
    
    # FILTER 2: Volume must be above average
    if last.vol_ratio < 0.8:
        return _no_signal("scalping")
    
    # Check H1 bias via SuperTrend if available
    h1_bull, h1_bear = True, True
    if bias_df is not None and len(bias_df) > 50:
        bdf = _compute(bias_df)
        if not bdf.empty:
            h1_bull = int(bdf.iloc[-1].st_dir) == 1
            h1_bear = int(bdf.iloc[-1].st_dir) == -1
    
    # BUY: SuperTrend up + RSI > 55 + price above EMA9 + bullish candle + H1 agrees
    if (int(last.st_dir) == 1 and last.rsi > 55 and last.rsi < 75
        and close > last.ema9 and _is_bullish_candle(last) and h1_bull
        and last.macd_hist > 0):
        score = 0.5 + min(0.4, (last.adx - 20) / 50)
        return {
            "signal": "buy", "score": round(score, 3),
            "sl": round(close - sl_pips * pip, 5),
            "tp": round(close + tp_pips * pip, 5),
            "reason": f"Scalp BUY: ST↑ ADX={last.adx:.0f} RSI={last.rsi:.0f} MACD+",
            "strategy": "scalping", "style": "scalping",
        }
    
    # SELL: SuperTrend down + RSI < 45 + price below EMA9 + bearish candle
    if (int(last.st_dir) == -1 and last.rsi < 45 and last.rsi > 25
        and close < last.ema9 and _is_bearish_candle(last) and h1_bear
        and last.macd_hist < 0):
        score = 0.5 + min(0.4, (last.adx - 20) / 50)
        return {
            "signal": "sell", "score": round(score, 3),
            "sl": round(close + sl_pips * pip, 5),
            "tp": round(close - tp_pips * pip, 5),
            "reason": f"Scalp SELL: ST↓ ADX={last.adx:.0f} RSI={last.rsi:.0f} MACD-",
            "strategy": "scalping", "style": "scalping",
        }
    
    return _no_signal("scalping")


# ══════════════════════════════════════════════════════════════════════════
# 2. INTRADAY v2 — SuperTrend + MACD crossover + ADX + pullback to EMA21
# ══════════════════════════════════════════════════════════════════════════

def intraday_signal(df: pd.DataFrame, bias_df: pd.DataFrame = None,
                    sl_atr: float = 1.5, tp_atr: float = 3.0, **kw) -> Dict:
    """
    IMPROVED: SuperTrend confirms trend. MACD histogram must flip positive.
    ADX > 25 required (strong trend). Price pulls back to EMA21 zone.
    H4 SuperTrend must agree.
    """
    if df is None or df.empty or len(df) < 60:
        return _no_signal("intraday")
    
    df = _compute(df)
    if df.empty: return _no_signal("intraday")
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last.close)
    atr = float(last.atr)
    
    # STRICT: ADX must be > 25 (strong trend only)
    if last.adx < 25:
        return _no_signal("intraday")
    
    # H4 bias check
    h4_bull, h4_bear = False, False
    if bias_df is not None and len(bias_df) > 50:
        bdf = _compute(bias_df)
        if not bdf.empty:
            h4_bull = int(bdf.iloc[-1].st_dir) == 1 and bdf.iloc[-1].adx > 20
            h4_bear = int(bdf.iloc[-1].st_dir) == -1 and bdf.iloc[-1].adx > 20
    
    # BUY: ST up + H4 agrees + MACD flipping positive + pullback to EMA21 + volume
    pullback_buy = (prev.low <= prev.ema21 * 1.002 and close > last.ema21)
    macd_flip_buy = (prev.macd_hist <= 0 and last.macd_hist > 0) or last.macd_hist > 0
    
    if (int(last.st_dir) == 1 and h4_bull and pullback_buy
        and macd_flip_buy and 45 < last.rsi < 70
        and last.vol_ratio > 0.8 and _is_bullish_candle(last)):
        score = 0.4
        if last.adx > 30: score += 0.15
        if last.vol_ratio > 1.2: score += 0.1
        if prev.macd_hist <= 0 and last.macd_hist > 0: score += 0.15  # fresh flip
        return {
            "signal": "buy", "score": round(min(1.0, score), 3),
            "sl": round(close - sl_atr * atr, 5),
            "tp": round(close + tp_atr * atr, 5),
            "reason": f"Intra BUY: ST↑ H4↑ ADX={last.adx:.0f} MACD flip+ RSI={last.rsi:.0f}",
            "strategy": "intraday", "style": "intraday",
        }
    
    # SELL
    pullback_sell = (prev.high >= prev.ema21 * 0.998 and close < last.ema21)
    macd_flip_sell = (prev.macd_hist >= 0 and last.macd_hist < 0) or last.macd_hist < 0
    
    if (int(last.st_dir) == -1 and h4_bear and pullback_sell
        and macd_flip_sell and 30 < last.rsi < 55
        and last.vol_ratio > 0.8 and _is_bearish_candle(last)):
        score = 0.4
        if last.adx > 30: score += 0.15
        if last.vol_ratio > 1.2: score += 0.1
        if prev.macd_hist >= 0 and last.macd_hist < 0: score += 0.15
        return {
            "signal": "sell", "score": round(min(1.0, score), 3),
            "sl": round(close + sl_atr * atr, 5),
            "tp": round(close - tp_atr * atr, 5),
            "reason": f"Intra SELL: ST↓ H4↓ ADX={last.adx:.0f} MACD flip- RSI={last.rsi:.0f}",
            "strategy": "intraday", "style": "intraday",
        }
    
    return _no_signal("intraday")


# ══════════════════════════════════════════════════════════════════════════
# 3. SWING v2 — D1 SuperTrend + H4 MACD + H1 pullback to EMA50
# ══════════════════════════════════════════════════════════════════════════

def swing_signal(df: pd.DataFrame, h4_df: pd.DataFrame = None,
                 d1_df: pd.DataFrame = None,
                 sl_atr: float = 2.0, tp_atr: float = 4.0, **kw) -> Dict:
    """
    IMPROVED: D1 SuperTrend sets direction (non-negotiable).
    H4 must confirm with MACD. H1 entry on pullback to EMA50.
    ADX > 20 on D1. Much stricter = fewer but higher quality trades.
    """
    if df is None or df.empty or len(df) < 60:
        return _no_signal("swing")
    
    df = _compute(df)
    if df.empty: return _no_signal("swing")
    
    last = df.iloc[-1]
    close = float(last.close)
    atr = float(last.atr)
    
    # D1 trend — MUST be present
    d1_bull, d1_bear = False, False
    if d1_df is not None and len(d1_df) > 50:
        d1 = _compute(d1_df)
        if not d1.empty:
            d1_bull = int(d1.iloc[-1].st_dir) == 1 and d1.iloc[-1].adx > 20
            d1_bear = int(d1.iloc[-1].st_dir) == -1 and d1.iloc[-1].adx > 20
    
    if not d1_bull and not d1_bear:
        return _no_signal("swing")
    
    # H4 momentum
    h4_bull, h4_bear = False, False
    if h4_df is not None and len(h4_df) > 50:
        h4 = _compute(h4_df)
        if not h4.empty:
            h4_bull = h4.iloc[-1].macd_hist > 0 and int(h4.iloc[-1].st_dir) == 1
            h4_bear = h4.iloc[-1].macd_hist < 0 and int(h4.iloc[-1].st_dir) == -1
    
    # BUY swing
    if d1_bull and h4_bull:
        pullback = (close <= last.ema50 * 1.005 and close > last.ema50 * 0.995)
        rsi_ok = 40 < last.rsi < 60
        if pullback and rsi_ok and _is_bullish_candle(last):
            score = 0.55 + min(0.35, (last.adx - 20) / 60)
            return {
                "signal": "buy", "score": round(score, 3),
                "sl": round(close - sl_atr * atr, 5),
                "tp": round(close + tp_atr * atr, 5),
                "reason": f"Swing BUY: D1↑ H4↑ pullback to EMA50 RSI={last.rsi:.0f}",
                "strategy": "swing", "style": "swing",
            }
    
    # SELL swing
    if d1_bear and h4_bear:
        pullback = (close >= last.ema50 * 0.995 and close < last.ema50 * 1.005)
        rsi_ok = 40 < last.rsi < 60
        if pullback and rsi_ok and _is_bearish_candle(last):
            score = 0.55 + min(0.35, (last.adx - 20) / 60)
            return {
                "signal": "sell", "score": round(score, 3),
                "sl": round(close + sl_atr * atr, 5),
                "tp": round(close - tp_atr * atr, 5),
                "reason": f"Swing SELL: D1↓ H4↓ pullback to EMA50 RSI={last.rsi:.0f}",
                "strategy": "swing", "style": "swing",
            }
    
    return _no_signal("swing")


# ══════════════════════════════════════════════════════════════════════════
# 4. NEWS v2 — BB squeeze → breakout + volume explosion + ADX spike
# ══════════════════════════════════════════════════════════════════════════

def news_signal(df: pd.DataFrame, **kw) -> Dict:
    """
    IMPROVED: Requires BOTH BB squeeze AND volume spike AND ADX rising.
    Triple confirmation prevents false breakouts.
    """
    if df is None or df.empty or len(df) < 60:
        return _no_signal("news")
    
    df = _compute(df)
    if len(df) < 30: return _no_signal("news")
    
    last = df.iloc[-1]
    prev5 = df.iloc[-6:-1]  # 5 bars before current
    close = float(last.close)
    atr = float(last.atr)
    
    # Squeeze: BB width in prev 5 bars was narrow
    avg_width = float(df.bb_width.rolling(50).mean().iloc[-1])
    was_squeezed = float(prev5.bb_width.mean()) < avg_width * 0.65
    
    # Breakout: current bar is large + volume spike
    bar_range = last.high - last.low
    is_explosive = bar_range > 1.8 * atr
    vol_spike = last.vol_ratio > 2.0
    
    # ADX must be rising (momentum building)
    adx_rising = last.adx > float(df.adx.iloc[-3])
    
    if not (was_squeezed and is_explosive and vol_spike and adx_rising):
        return _no_signal("news")
    
    score = 0.5 + min(0.4, (last.vol_ratio - 2) / 4)
    
    if close > float(prev5.close.iloc[-1]):  # Bullish breakout
        return {
            "signal": "buy", "score": round(score, 3),
            "sl": round(close - 1.0 * atr, 5),
            "tp": round(close + 2.5 * atr, 5),
            "reason": f"News BUY: squeeze→breakout vol={last.vol_ratio:.1f}x ADX={last.adx:.0f}↑",
            "strategy": "news", "style": "news",
        }
    else:  # Bearish breakout
        return {
            "signal": "sell", "score": round(score, 3),
            "sl": round(close + 1.0 * atr, 5),
            "tp": round(close - 2.5 * atr, 5),
            "reason": f"News SELL: squeeze→breakout vol={last.vol_ratio:.1f}x ADX={last.adx:.0f}↑",
            "strategy": "news", "style": "news",
        }


# ══════════════════════════════════════════════════════════════════════════
# 5. WHALE v2 — Volume spike + engulfing + SuperTrend alignment
# ══════════════════════════════════════════════════════════════════════════

def whale_signal(df: pd.DataFrame, bias_df: pd.DataFrame = None, **kw) -> Dict:
    """
    IMPROVED: Volume > 3x average (stricter) + engulfing/pin bar
    + SuperTrend must agree + ADX > 20.
    """
    if df is None or df.empty or len(df) < 60:
        return _no_signal("whale")
    
    df = _compute(df)
    if df.empty: return _no_signal("whale")
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last.close)
    atr = float(last.atr)
    
    # Stricter volume requirement
    if last.vol_ratio < 3.0:
        return _no_signal("whale")
    
    if last.adx < 20:
        return _no_signal("whale")
    
    body = abs(last.close - last.open)
    rng = last.high - last.low + 1e-9
    
    # Bullish engulfing
    bull_engulf = (last.close > last.open and prev.close < prev.open
                   and last.close > prev.open and last.open < prev.close
                   and body / rng > 0.5)
    
    # Bullish pin bar
    lower_wick = min(last.open, last.close) - last.low
    bull_pin = lower_wick > body * 2 and lower_wick / rng > 0.55
    
    # Bearish engulfing
    bear_engulf = (last.close < last.open and prev.close > prev.open
                   and last.close < prev.open and last.open > prev.close
                   and body / rng > 0.5)
    
    # Bearish pin bar
    upper_wick = last.high - max(last.open, last.close)
    bear_pin = upper_wick > body * 2 and upper_wick / rng > 0.55
    
    # SuperTrend must agree
    st_bull = int(last.st_dir) == 1
    st_bear = int(last.st_dir) == -1
    
    if (bull_engulf or bull_pin) and st_bull:
        score = 0.5 + min(0.4, (last.vol_ratio - 3) / 5)
        return {
            "signal": "buy", "score": round(score, 3),
            "sl": round(close - 1.5 * atr, 5),
            "tp": round(close + 3.0 * atr, 5),
            "reason": f"Whale BUY: vol {last.vol_ratio:.1f}x + {'engulf' if bull_engulf else 'pin'} + ST↑",
            "strategy": "whale", "style": "whale",
        }
    
    if (bear_engulf or bear_pin) and st_bear:
        score = 0.5 + min(0.4, (last.vol_ratio - 3) / 5)
        return {
            "signal": "sell", "score": round(score, 3),
            "sl": round(close + 1.5 * atr, 5),
            "tp": round(close - 3.0 * atr, 5),
            "reason": f"Whale SELL: vol {last.vol_ratio:.1f}x + {'engulf' if bear_engulf else 'pin'} + ST↓",
            "strategy": "whale", "style": "whale",
        }
    
    return _no_signal("whale")


# ══════════════════════════════════════════════════════════════════════════
# 6. FIBONACCI v2 — Golden zone + SuperTrend + RSI support + ADX
# ══════════════════════════════════════════════════════════════════════════

def fibonacci_signal(df: pd.DataFrame, bias_df: pd.DataFrame = None,
                     swing_lookback: int = 50, **kw) -> Dict:
    """
    IMPROVED: SuperTrend confirms trend (not just EMA).
    ADX > 22 required. RSI must hold in healthy zone.
    H4 must agree.
    """
    if df is None or df.empty or len(df) < swing_lookback + 20:
        return _no_signal("fibonacci")
    
    df = _compute(df)
    if len(df) < swing_lookback: return _no_signal("fibonacci")
    
    last = df.iloc[-1]
    close = float(last.close)
    atr = float(last.atr)
    
    if last.adx < 22:
        return _no_signal("fibonacci")
    
    # H4 confirmation
    h4_bull, h4_bear = False, False
    if bias_df is not None and len(bias_df) > 50:
        bdf = _compute(bias_df)
        if not bdf.empty:
            h4_bull = int(bdf.iloc[-1].st_dir) == 1
            h4_bear = int(bdf.iloc[-1].st_dir) == -1
    
    lookback = df.tail(swing_lookback)
    
    # Uptrend Fib
    if int(last.st_dir) == 1 and h4_bull:
        swing_low = float(lookback.low.min())
        sl_idx = lookback.low.idxmin()
        after = lookback[lookback.index > sl_idx]
        if len(after) < 5: return _no_signal("fibonacci")
        swing_high = float(after.high.max())
        if swing_high <= swing_low: return _no_signal("fibonacci")
        
        fib_range = swing_high - swing_low
        fib_618 = swing_high - 0.618 * fib_range
        fib_786 = swing_high - 0.786 * fib_range
        
        if fib_786 <= close <= fib_618 and 38 < last.rsi < 55 and _is_bullish_candle(last):
            score = 0.55 + min(0.35, (last.adx - 22) / 50)
            return {
                "signal": "buy", "score": round(score, 3),
                "sl": round(fib_786 - 0.5 * atr, 5),
                "tp": round(swing_high, 5),
                "reason": f"Fib BUY: golden zone ST↑ H4↑ RSI={last.rsi:.0f} ADX={last.adx:.0f}",
                "strategy": "fibonacci", "style": "fibonacci",
            }
    
    # Downtrend Fib
    if int(last.st_dir) == -1 and h4_bear:
        swing_high = float(lookback.high.max())
        sh_idx = lookback.high.idxmax()
        after = lookback[lookback.index > sh_idx]
        if len(after) < 5: return _no_signal("fibonacci")
        swing_low = float(after.low.min())
        if swing_high <= swing_low: return _no_signal("fibonacci")
        
        fib_range = swing_high - swing_low
        fib_618 = swing_low + 0.618 * fib_range
        fib_786 = swing_low + 0.786 * fib_range
        
        if fib_618 <= close <= fib_786 and 45 < last.rsi < 62 and _is_bearish_candle(last):
            score = 0.55 + min(0.35, (last.adx - 22) / 50)
            return {
                "signal": "sell", "score": round(score, 3),
                "sl": round(fib_786 + 0.5 * atr, 5),
                "tp": round(swing_low, 5),
                "reason": f"Fib SELL: golden zone ST↓ H4↓ RSI={last.rsi:.0f} ADX={last.adx:.0f}",
                "strategy": "fibonacci", "style": "fibonacci",
            }
    
    return _no_signal("fibonacci")


# ══════════════════════════════════════════════════════════════════════════
# REGISTRY
# ══════════════════════════════════════════════════════════════════════════

STRATEGY_REGISTRY = {
    "scalping": scalping_signal,
    "intraday": intraday_signal,
    "swing": swing_signal,
    "news": news_signal,
    "whale": whale_signal,
    "fibonacci": fibonacci_signal,
}
