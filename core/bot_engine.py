"""
=============================================================================
  MULTI-STRATEGY BOT ENGINE
  Runs 6 strategies simultaneously, each with own timeframe & risk params
=============================================================================
"""
from __future__ import annotations
import logging, threading, time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config.settings import (RISK, SYMBOLS, SCALP, INTRADAY, SWING,
                             NEWS, WHALE, FIB, MT5_CFG, SYS)
from core.mt5_connector import MT5Client, MT5_AVAILABLE
from core.risk_guardian import RiskGuardian
from core.indicators import compute_all
from core.position_sizer import PositionSizer
from strategies.custom import custom_signal
from strategies.all_strategies import (
    scalping_signal, intraday_signal, swing_signal,
    news_signal, whale_signal, fibonacci_signal
)

log = logging.getLogger(__name__)
MAGIC = 20260423


class BotEngine:
    def __init__(self, creds=None, data_dir=None):
        """creds: optional dict (login, password, server, path) saved from the web form.
        Falls back to environment / .env values when not given."""
        c = creds or {}
        self.mt5 = MT5Client(login=c.get("login", MT5_CFG.login),
                            password=c.get("password", MT5_CFG.password),
                            server=c.get("server", MT5_CFG.server),
                            path=c.get("path", MT5_CFG.path))
        self._reconnecting = False
        state_file = str(Path(data_dir) / "guardian_state.json") if data_dir else "data/guardian_state.json"
        self.guardian = RiskGuardian(state_file=state_file)
        self.sizer = PositionSizer(mt5_client=self.mt5)
        self.running = False
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._snapshot: Dict = {}
        self._lock = threading.Lock()
        self.decisions: List[Dict] = []
        self.strategy_stats: Dict[str, Dict] = {
            s: {"trades": 0, "wins": 0, "pnl": 0.0, "signals": 0}
            for s in ["scalping", "intraday", "swing", "news", "whale", "fibonacci"]
        }
        self.enabled: Dict[str, bool] = {}     # missing = on
        self.custom: Dict[str, Dict] = {}
        self.names: Dict[str, str] = {}
        self.last_errors: Dict[str, Optional[str]] = {}

    def set_strategy_config(self, cfg: Dict, names: Optional[Dict[str, str]] = None):
        """Apply what was chosen on the Strategies page. Safe to call while running."""
        live = {"scalping": SCALP, "intraday": INTRADAY, "swing": SWING,
                "news": NEWS, "whale": WHALE, "fibonacci": FIB}
        if not hasattr(self, "_defaults"):          # remember the original values once, for "reset"
            self._defaults = {}
            for sid, p in live.items():
                sl, tp = (p.sl_pips, p.tp_pips) if sid == "scalping" else (p.sl_atr_mult, p.tp_atr_mult)
                self._defaults[sid] = {"risk_pct": RISK.risk_per_trade.get(sid, 0.01) * 100, "sl": sl, "tp": tp}
        for sid, p in live.items():
            o = cfg.get("overrides", {}).get(sid) or self._defaults[sid]
            RISK.risk_per_trade[sid] = o["risk_pct"] / 100.0
            if sid == "scalping":
                p.sl_pips, p.tp_pips = o["sl"], o["tp"]
            else:
                p.sl_atr_mult, p.tp_atr_mult = o["sl"], o["tp"]
        for sid, spec in cfg.get("custom", {}).items():
            RISK.risk_per_trade[sid] = spec["risk_pct"] / 100.0
            self.strategy_stats.setdefault(sid, {"trades": 0, "wins": 0, "pnl": 0.0, "signals": 0})
        self.custom = dict(cfg.get("custom", {}))
        self.enabled = dict(cfg.get("enabled", {}))
        self.names = dict(names or {})

    def _on(self, sid: str) -> bool:
        return self.enabled.get(sid, True)

    def start(self) -> bool:
        if self.running: return True
        log.info("Starting multi-strategy bot engine...")
        self.mt5.connect()
        self._stop.clear()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if not self.running: return
        self._stop.set()
        self.running = False
        if self.thread: self.thread.join(timeout=10)
        self.mt5.disconnect()

    def apply_credentials(self, login: int, password: str, server: str, path: str = "") -> bool:
        """Switch to new account details and reconnect. Safe to call while the bot is running."""
        self._reconnecting = True
        try:
            self.mt5.disconnect()
            self.mt5.login, self.mt5.password = int(login), password
            self.mt5.server, self.mt5.path = server, path
            return self.mt5.connect()
        finally:
            self._reconnecting = False

    def _loop(self):
        while not self._stop.is_set():
            try: self._tick()
            except Exception as e: log.exception(f"Tick error: {e}")
            self._stop.wait(SYS.bot_tick_seconds)

    def _tick(self):
        if self._reconnecting:      # account details are being changed: skip this cycle
            return
        account = self.mt5.account_info()
        positions = self.mt5.get_positions(magic=MAGIC)
        guardian = self.guardian.update(
            float(account.get("balance", 0)),
            float(account.get("equity", 0)),
            positions
        )
        can_trade = guardian["can_trade"] and SYS.auto_trade

        # Track which strategies already have open positions
        active_strategies = set()
        for p in positions:
            comment = p.get("comment", "")
            for s in self.strategy_stats:
                if comment == f"ex_{s}":
                    active_strategies.add(s)

        results = {}
        now_utc = datetime.now(tz=timezone.utc)
        hour = now_utc.hour

        # ── Run each strategy ──
        # 1. SCALPING (M5) — only during high-liquidity sessions
        if self._on("scalping") and any(s <= hour < e for s, e in SCALP.allowed_sessions_utc):
            for sym in SYMBOLS.scalping:
                if "scalping" in active_strategies: break
                r = self._run_strategy("scalping", sym, SCALP.timeframe,
                                      SCALP.bias_timeframe, can_trade, positions)
                results[f"scalp_{sym}"] = r

        # 2. INTRADAY (M15) — London to NY
        if self._on("intraday") and any(s <= hour < e for s, e in INTRADAY.allowed_sessions_utc):
            for sym in SYMBOLS.intraday:
                if "intraday" in active_strategies: break
                r = self._run_strategy("intraday", sym, INTRADAY.timeframe,
                                      INTRADAY.bias_timeframe, can_trade, positions)
                results[f"intra_{sym}"] = r

        # 3. SWING (H1) — always on (hold for days)
        for sym in (SYMBOLS.swing if self._on("swing") else []):
            if "swing" in active_strategies: break
            r = self._run_strategy("swing", sym, SWING.timeframe,
                                  SWING.setup_timeframe, can_trade, positions,
                                  extra_tf=SWING.bias_timeframe)
            results[f"swing_{sym}"] = r

        # 4. NEWS (M5) — always on (detects spikes)
        for sym in (SYMBOLS.news if self._on("news") else []):
            if "news" in active_strategies: break
            r = self._run_strategy("news", sym, NEWS.timeframe,
                                  None, can_trade, positions)
            results[f"news_{sym}"] = r

        # 5. WHALE (M15) — always on (detects big moves)
        for sym in (SYMBOLS.whale if self._on("whale") else []):
            if "whale" in active_strategies: break
            r = self._run_strategy("whale", sym, WHALE.timeframe,
                                  WHALE.bias_timeframe, can_trade, positions)
            results[f"whale_{sym}"] = r

        # 6. FIBONACCI (H1) — always on
        for sym in (SYMBOLS.fibonacci if self._on("fibonacci") else []):
            if "fibonacci" in active_strategies: break
            r = self._run_strategy("fibonacci", sym, FIB.timeframe,
                                  FIB.bias_timeframe, can_trade, positions)
            results[f"fib_{sym}"] = r

        # 7. CUSTOM strategies made on the web page
        for cid, spec in list(self.custom.items()):
            if not self._on(cid):
                continue
            for sym in spec["symbols"]:
                if cid in active_strategies: break
                results[f"{cid}_{sym}"] = self._run_strategy(
                    cid, sym, spec["timeframe"], None, can_trade, positions)

        with self._lock:
            self._snapshot = {
                "account": account,
                "guardian": guardian,
                "positions": positions,
                "strategies": results,
                "strategy_stats": self.strategy_stats,
                "last_update": now_utc.isoformat(),
                "mode": SYS.mode,
                "auto_trade": SYS.auto_trade,
                "mt5_connected": self.mt5.connected,
                "mt5_available": MT5_AVAILABLE,
            }

    def _run_strategy(self, strategy: str, symbol: str,
                      tf: str, bias_tf: Optional[str],
                      can_execute: bool, positions: list,
                      extra_tf: str = None) -> Dict:
        """Fetch data, run strategy, optionally execute."""
        df = self.mt5.fetch_ohlcv(symbol, tf, bars=500)
        if df.empty:
            return {"symbol": symbol, "strategy": strategy, "status": "no_data"}

        df.attrs["symbol"] = symbol
        bias_df = self.mt5.fetch_ohlcv(symbol, bias_tf, bars=200) if bias_tf else None
        extra_df = self.mt5.fetch_ohlcv(symbol, extra_tf, bars=200) if extra_tf else None

        # Call the appropriate strategy function
        if strategy == "scalping":
            sig = scalping_signal(df, bias_df, tp_pips=SCALP.tp_pips, sl_pips=SCALP.sl_pips)
        elif strategy == "intraday":
            sig = intraday_signal(df, bias_df, sl_atr=INTRADAY.sl_atr_mult, tp_atr=INTRADAY.tp_atr_mult)
        elif strategy == "swing":
            sig = swing_signal(df, bias_df, extra_df, sl_atr=SWING.sl_atr_mult, tp_atr=SWING.tp_atr_mult)
        elif strategy == "news":
            sig = news_signal(df)
        elif strategy == "whale":
            sig = whale_signal(df, bias_df)
        elif strategy == "fibonacci":
            sig = fibonacci_signal(df, bias_df)
        elif strategy in self.custom:
            sig = custom_signal(df, self.custom[strategy], strategy)
            self.last_errors[strategy] = sig.get("error")
        else:
            sig = {"signal": "hold", "score": 0, "strategy": strategy}

        if sig["signal"] != "hold":
            self.strategy_stats[strategy]["signals"] += 1

        # Build indicator snapshot
        idf = compute_all(df).dropna()
        last = idf.iloc[-1] if not idf.empty else None
        snap = {
            "symbol": symbol, "strategy": strategy, "status": "ok",
            "price": float(last.close) if last is not None else 0,
            "rsi": round(float(last.rsi), 1) if last is not None else 0,
            "atr": round(float(last.atr), 5) if last is not None else 0,
            "signal": sig["signal"], "score": sig["score"],
            "reason": sig["reason"],
        }

        # Execute if conditions met
        if can_execute and sig["signal"] in ("buy", "sell") and sig["score"] >= 0.3:
            # Check not already in same direction on same symbol
            existing = [p for p in positions if p["symbol"] == symbol and p["side"] == sig["signal"]]
            if not existing:
                self._execute(symbol, sig, strategy)

        return snap

    def _execute(self, symbol: str, sig: Dict, strategy: str):
        account = self.mt5.account_info()
        balance = float(account.get("balance", 10000))
        risk_pct = RISK.risk_per_trade.get(strategy, 0.01)

        sizing = self.sizer.calculate(
            symbol=symbol, account_balance=balance,
            entry_price=sig.get("sl", 0) and float(sig.get("tp", 0)),  # just needs price
            stop_loss_price=sig["sl"],
            risk_per_trade_pct=risk_pct,
            max_lot_per_1k=RISK.max_lot_per_1k,
            max_lot_absolute=RISK.max_lot_absolute,
        )

        if not sizing.get("can_trade"): return

        result = self.mt5.market_order(
            symbol=symbol, side=sig["signal"],
            volume=sizing["lot"], sl=sig["sl"], tp=sig["tp"],
            comment=f"ex_{strategy}", magic=MAGIC,
        )

        if result.get("success"):
            log.info(f"[{strategy.upper()}] {sig['signal'].upper()} {sizing['lot']} "
                    f"{symbol} @ {result['price']} | SL={sig['sl']} TP={sig['tp']}")
            self.guardian.mark_trade(0, strategy)
            self.strategy_stats[strategy]["trades"] += 1
            self.decisions.insert(0, {
                "time": datetime.now(timezone.utc).isoformat(),
                "strategy": strategy, "symbol": symbol,
                "signal": sig["signal"], "score": sig["score"],
                "lot": sizing["lot"], "entry": result["price"],
                "sl": sig["sl"], "tp": sig["tp"],
                "risk": sizing["risk_amount"], "reason": sig["reason"],
            })
            self.decisions = self.decisions[:100]

    # ── Public API ──
    def get_snapshot(self) -> Dict:
        with self._lock: return dict(self._snapshot)

    def enable_auto_trade(self):
        SYS.auto_trade = True
        self.guardian.enable()

    def disable_auto_trade(self):
        SYS.auto_trade = False
        self.guardian.disable()

    def close_all(self) -> int:
        return self.mt5.close_all_positions(magic=MAGIC)
