"""
=============================================================================
  RISK GUARDIAN — Protects YOUR capital (no prop firm rules, just smart limits)
=============================================================================
"""
from __future__ import annotations
import json, logging, os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, date
from typing import Dict, List, Optional
from config.settings import RISK

log = logging.getLogger(__name__)

@dataclass
class DailyStats:
    date: str = ""
    starting_balance: float = 0.0
    realized_pnl: float = 0.0
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    by_strategy: Dict[str, float] = field(default_factory=dict)

class RiskGuardian:
    def __init__(self, state_file: str = "data/guardian_state.json"):
        self.state_file = state_file
        self.starting_balance: float = 0.0
        self.daily_stats: Dict[str, DailyStats] = {}
        self._disabled = False
        self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    d = json.load(f)
                self.starting_balance = d.get("starting_balance", 0)
                self.daily_stats = {k: DailyStats(**v) for k, v in d.get("daily_stats", {}).items()}
            except: pass

    def _save(self):
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        try:
            with open(self.state_file, "w") as f:
                json.dump({"starting_balance": self.starting_balance,
                          "daily_stats": {k: asdict(v) for k, v in self.daily_stats.items()}}, f, indent=2)
        except: pass

    def init_balance(self, balance: float):
        if self.starting_balance == 0 and balance > 0:
            self.starting_balance = balance
            self._save()

    def update(self, balance: float, equity: float, positions: list) -> dict:
        if self.starting_balance == 0 and balance > 0:
            self.init_balance(balance)

        today = date.today().isoformat()
        if today not in self.daily_stats:
            self.daily_stats[today] = DailyStats(date=today, starting_balance=balance)
        ds = self.daily_stats[today]

        today_pnl = equity - ds.starting_balance
        today_pnl_pct = today_pnl / ds.starting_balance if ds.starting_balance else 0
        today_dd = max(0, -today_pnl_pct)

        total_pnl = equity - self.starting_balance
        total_pnl_pct = total_pnl / self.starting_balance if self.starting_balance else 0
        total_dd = max(0, -total_pnl_pct)

        can_trade = True
        state = "ok"
        warnings = []

        if today_dd >= RISK.max_daily_loss_pct:
            can_trade = False
            state = "frozen"
            warnings.append(f"Daily loss {today_dd*100:.1f}% hit limit {RISK.max_daily_loss_pct*100}%")
        elif today_dd >= RISK.max_daily_loss_pct * 0.7:
            state = "warning"
            warnings.append(f"Approaching daily loss limit: {today_dd*100:.1f}%")

        if total_dd >= RISK.max_total_drawdown_pct:
            can_trade = False
            state = "emergency"
            warnings.append(f"Total DD {total_dd*100:.1f}% hit max {RISK.max_total_drawdown_pct*100}%")

        if today_pnl_pct >= RISK.daily_profit_target_pct:
            warnings.append(f"Daily profit target {RISK.daily_profit_target_pct*100}% hit! Consider stopping.")

        if len(positions) >= RISK.max_open_positions:
            can_trade = False
            warnings.append(f"Max positions ({RISK.max_open_positions}) reached")

        if self._disabled:
            can_trade = False
            state = "disabled"

        self._save()
        trading_days = sum(1 for s in self.daily_stats.values() if s.trades_count > 0)
        total_trades = sum(s.trades_count for s in self.daily_stats.values())
        total_wins = sum(s.wins for s in self.daily_stats.values())

        return {
            "state": state, "can_trade": can_trade, "warnings": warnings,
            "balance": balance, "equity": equity,
            "starting_balance": self.starting_balance,
            "today_pnl": round(today_pnl, 2),
            "today_pnl_pct": round(today_pnl_pct * 100, 2),
            "today_dd_pct": round(today_dd * 100, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct * 100, 2),
            "total_dd_pct": round(total_dd * 100, 2),
            "open_positions": len(positions),
            "trading_days": trading_days,
            "total_trades": total_trades,
            "win_rate": round(total_wins / max(total_trades, 1) * 100, 1),
            "daily_loss_remaining": round(max(0, (RISK.max_daily_loss_pct - today_dd) * ds.starting_balance), 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def mark_trade(self, profit: float, strategy: str):
        today = date.today().isoformat()
        if today in self.daily_stats:
            ds = self.daily_stats[today]
            ds.trades_count += 1
            ds.realized_pnl += profit
            ds.by_strategy[strategy] = ds.by_strategy.get(strategy, 0) + profit
            if profit > 0: ds.wins += 1
            elif profit < 0: ds.losses += 1
            self._save()

    def disable(self): self._disabled = True
    def enable(self): self._disabled = False
    def get_daily_stats(self): return sorted([asdict(s) for s in self.daily_stats.values()], key=lambda x: x["date"])
