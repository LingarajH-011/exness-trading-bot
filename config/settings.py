"""
=============================================================================
  EXNESS MULTI-STRATEGY BOT — Central Configuration
  
  No prop firm rules. Your own $10k. Moderate risk.
  6 Trading Styles: Scalping, Intraday, Swing, News, Whale, Fibonacci
  Each style has its own timeframe, risk params, and session windows.
=============================================================================
"""
import os
from dataclasses import dataclass, field

# Optional: still honour a local .env file (account details are normally saved from the web form).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass
from typing import List, Dict

# =============================================================================
# ACCOUNT
# =============================================================================
@dataclass
class AccountConfig:
    account_size: float = 10000.0
    currency: str = "USD"

# =============================================================================
# RISK — Moderate (1-2% per trade)
# =============================================================================
@dataclass
class RiskConfig:
    # Per-trade risk (varies by strategy type)
    risk_per_trade: Dict[str, float] = field(default_factory=lambda: {
        "scalping":   0.005,   # 0.5% — many trades, tight risk
        "intraday":   0.01,    # 1.0% — standard day trade
        "swing":      0.015,   # 1.5% — fewer trades, wider stops
        "news":       0.005,   # 0.5% — volatile, keep small
        "whale":      0.01,    # 1.0% — high-conviction moves
        "fibonacci":  0.01,    # 1.0% — precision entries
    })

    # Hard limits
    max_daily_loss_pct: float = 0.04        # Stop trading after 4% daily loss
    max_total_drawdown_pct: float = 0.15    # 15% max drawdown (your money, be careful)
    max_open_positions: int = 6             # 1 per strategy type max
    max_lot_per_1k: float = 0.02            # 0.02 lots per $1k = 0.20 on $10k
    max_lot_absolute: float = 1.0           # Never exceed 1 lot

    # Daily profit target (optional — stop when you're ahead)
    daily_profit_target_pct: float = 0.03   # Bank 3% daily gains

# =============================================================================
# SYMBOLS — Grouped by what works best for each style
# =============================================================================
@dataclass
class SymbolConfig:
    # Scalping: tight spreads, high liquidity
    scalping: List[str] = field(default_factory=lambda: [
        "EURUSDm", "USDJPYm", "GBPUSDm"
    ])
    # Intraday: trending pairs
    intraday: List[str] = field(default_factory=lambda: [
        "EURUSDm", "GBPUSDm", "USDJPYm", "AUDUSDm", "XAUUSDm"
    ])
    # Swing: all majors + gold
    swing: List[str] = field(default_factory=lambda: [
        "EURUSDm", "GBPUSDm", "USDJPYm", "AUDUSDm", "USDCADm", "XAUUSDm"
    ])
    # News: USD pairs (most affected by US news)
    news: List[str] = field(default_factory=lambda: [
        "EURUSDm", "GBPUSDm", "USDJPYm", "XAUUSDm"
    ])
    # Whale: gold + volatile pairs
    whale: List[str] = field(default_factory=lambda: [
        "XAUUSDm", "GBPJPYm", "EURUSDm", "USDJPYm"
    ])
    # Fibonacci: trending pairs
    fibonacci: List[str] = field(default_factory=lambda: [
        "EURUSDm", "GBPUSDm", "XAUUSDm", "USDJPYm", "AUDUSDm"
    ])

# =============================================================================
# STRATEGY-SPECIFIC PARAMETERS
# =============================================================================

@dataclass 
class ScalpingParams:
    """Triple EMA scalping on M1-M5. 5-15 pip targets."""
    timeframe: str = "M5"
    bias_timeframe: str = "H1"
    ema_fast: int = 8
    ema_mid: int = 13
    ema_slow: int = 21
    rsi_period: int = 7
    rsi_entry_bull: float = 55.0      # RSI > 55 for longs
    rsi_entry_bear: float = 45.0      # RSI < 45 for shorts
    tp_pips: float = 10.0             # 10-pip target
    sl_pips: float = 7.0              # 7-pip stop
    max_spread_pips: float = 2.0      # Don't trade if spread > 2 pips
    max_trades_per_day: int = 10      # Cap daily scalps
    # Session: London + NY overlap only (tightest spreads)
    allowed_sessions_utc: List[tuple] = field(default_factory=lambda: [
        (7, 11), (13, 17)
    ])

@dataclass
class IntradayParams:
    """EMA stack + MACD + RSI trend following on M15. 30-80 pip targets."""
    timeframe: str = "M15"
    bias_timeframe: str = "H4"
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0          # 1:2 R:R
    min_signal_score: float = 0.35
    # All active sessions
    allowed_sessions_utc: List[tuple] = field(default_factory=lambda: [
        (7, 17)  # London open to NY close
    ])

@dataclass
class SwingParams:
    """Multi-timeframe: D1 bias → H4 setup → H1 entry. Hold 2-10 days."""
    timeframe: str = "H1"
    setup_timeframe: str = "H4"
    bias_timeframe: str = "D1"
    ema_fast: int = 20
    ema_slow: int = 50
    ema_trend: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    sl_atr_mult: float = 2.0          # Wider stops for swing
    tp_atr_mult: float = 4.0          # 1:2 R:R
    # RSI-based exit zones
    rsi_tp_bull: float = 72.0         # Consider closing longs
    rsi_tp_bear: float = 28.0         # Consider closing shorts
    min_signal_score: float = 0.40    # Higher bar for swing
    trailing_stop_atr: float = 1.5    # Trailing stop in ATR multiples

@dataclass
class NewsParams:
    """Trade volatility spikes around high-impact news. Hold minutes."""
    timeframe: str = "M5"
    atr_period: int = 14
    # Pre-news: detect squeeze (low ATR → BB narrowing)
    bb_period: int = 20
    bb_squeeze_threshold: float = 0.6  # BB width < 60% of avg = squeeze
    # Post-news breakout
    breakout_atr_mult: float = 1.5    # Move > 1.5× ATR = breakout
    sl_atr_mult: float = 1.0          # Tight stop on news trades
    tp_atr_mult: float = 2.5          # Wide target
    # News events handled externally (calendar API or manual)
    news_lookback_minutes: int = 5    # Check last 5 min for spike
    news_cooldown_minutes: int = 30   # Don't re-enter for 30 min
    max_trades_per_event: int = 1

@dataclass
class WhaleParams:
    """Detect large institutional moves via volume + price action."""
    timeframe: str = "M15"
    bias_timeframe: str = "H1"
    # Volume spike detection
    vol_spike_mult: float = 2.5       # Volume > 2.5× average = whale
    vol_sma_period: int = 20
    # Price momentum confirmation
    atr_period: int = 14
    momentum_bars: int = 3            # Check last 3 bars
    min_momentum_atr: float = 1.0     # Move > 1 ATR in 3 bars
    # Engulfing/pin bar detection
    body_to_range_min: float = 0.6    # Engulfing: body > 60% of range
    # Entry/exit
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    min_signal_score: float = 0.45

@dataclass
class FibonacciParams:
    """Golden zone (61.8%-78.6%) retracement entries in trending markets."""
    timeframe: str = "H1"
    bias_timeframe: str = "H4"
    # Fibonacci levels
    fib_golden_low: float = 0.618
    fib_golden_high: float = 0.786
    fib_50: float = 0.50
    # Trend detection
    ema_trend: int = 50
    atr_period: int = 14
    # Swing detection (lookback)
    swing_lookback: int = 50          # Bars to find swing high/low
    swing_min_bars: int = 10          # Min bars between swing points
    # Entry refinement
    rsi_period: int = 14
    rsi_support_bull: float = 42.0    # RSI holding above 40 in uptrend
    rsi_support_bear: float = 58.0    # RSI holding below 60 in downtrend
    # Exit
    sl_below_fib_786: bool = True     # SL below 78.6% level
    tp_at_swing_high: bool = True     # TP at previous swing high/low
    sl_atr_mult: float = 1.0          # Fallback: 1 ATR below entry
    tp_atr_mult: float = 3.0          # Fallback: 3 ATR target

# =============================================================================
# MT5 CONNECTION (Exness)
# =============================================================================
@dataclass
class MT5Config:
    login: int = int(os.getenv("MT5_LOGIN", "0"))
    password: str = os.getenv("MT5_PASSWORD", "")
    server: str = os.getenv("MT5_SERVER", "Exness-MT5Real")
    path: str = os.getenv("MT5_PATH", "")
    timeout: int = 60000

# =============================================================================
# SYSTEM
# =============================================================================
@dataclass
class SystemConfig:
    # 127.0.0.1 = only this machine. Put a reverse proxy (HTTPS) in front, or set FLASK_HOST=0.0.0.0
    # and restrict the port in your firewall / AWS security group.
    flask_host: str = os.getenv("FLASK_HOST", "127.0.0.1")
    flask_port: int = int(os.getenv("PORT", "5000"))
    flask_debug: bool = False
    bot_tick_seconds: int = 15          # Faster tick for scalping
    log_level: str = "INFO"
    log_dir: str = "logs"
    data_dir: str = "data"
    mode: str = "paper"                 # "paper" | "live"
    auto_trade: bool = False

# =============================================================================
# SINGLETONS
# =============================================================================
ACCOUNT   = AccountConfig()
RISK      = RiskConfig()
SYMBOLS   = SymbolConfig()
SCALP     = ScalpingParams()
INTRADAY  = IntradayParams()
SWING     = SwingParams()
NEWS      = NewsParams()
WHALE     = WhaleParams()
FIB       = FibonacciParams()
MT5_CFG   = MT5Config()
SYS       = SystemConfig()


def describe_config() -> dict:
    """Return full config as dict for dashboard."""
    return {
        "account": ACCOUNT.__dict__,
        "risk": RISK.__dict__,
        "symbols": {
            "scalping": SYMBOLS.scalping,
            "intraday": SYMBOLS.intraday,
            "swing": SYMBOLS.swing,
            "news": SYMBOLS.news,
            "whale": SYMBOLS.whale,
            "fibonacci": SYMBOLS.fibonacci,
        },
        "strategies": {
            "scalping": SCALP.__dict__,
            "intraday": INTRADAY.__dict__,
            "swing": SWING.__dict__,
            "news": NEWS.__dict__,
            "whale": WHALE.__dict__,
            "fibonacci": FIB.__dict__,
        },
        "mt5": {"server": MT5_CFG.server, "login": MT5_CFG.login or "not set"},
        "system": SYS.__dict__,
    }
