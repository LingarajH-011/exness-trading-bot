"""
=============================================================================
  POSITION SIZER — Convert signal + stop-loss into a safe lot size.
  
  Core formula:
      risk_amount     = account_balance * risk_per_trade_pct
      loss_per_lot    = stop_loss_pips * pip_value_per_lot
      lot_size        = risk_amount / loss_per_lot
  
  Then clamp to broker's volume_min/max/step.
=============================================================================
"""
from __future__ import annotations
import logging
import math
from typing import Dict

from core.indicators import pip_value

log = logging.getLogger(__name__)


# Rough USD pip value per standard lot (1.0) for common symbols.
# MT5 symbol_info gives exact values; this is fallback.
_PIP_VALUE_PER_LOT_USD = {
    "EURUSD": 10.0, "GBPUSD": 10.0, "AUDUSD": 10.0, "NZDUSD": 10.0,
    "USDCAD": 10.0, "USDCHF": 10.0, "USDJPY": 10.0,
    "EURJPY": 10.0, "GBPJPY": 10.0, "AUDJPY": 10.0,
    "EURGBP": 10.0, "EURAUD": 10.0, "EURCAD": 10.0, "EURCHF": 10.0,
    "GBPAUD": 10.0, "GBPCAD": 10.0, "GBPCHF": 10.0,
    "XAUUSD": 1.0,  # per 0.01 pip on a 1.0 lot (100 oz)
    "XAGUSD": 5.0,
}


class PositionSizer:
    """
    Compute correct lot size given risk parameters.
    Clamps to broker constraints (volume_min / volume_max / volume_step).
    """

    def __init__(self, mt5_client=None):
        self.mt5 = mt5_client
        self._symbol_info_cache: Dict[str, Dict] = {}

    def _get_symbol_info(self, symbol: str) -> Dict:
        """Cache symbol info to avoid repeated MT5 lookups."""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]
        info = {}
        if self.mt5:
            info = self.mt5.get_symbol_info(symbol) or {}
        # Sensible defaults
        info.setdefault("volume_min",  0.01)
        info.setdefault("volume_max", 100.0)
        info.setdefault("volume_step", 0.01)
        info.setdefault("digits", 5)
        info.setdefault("point", 0.00001)
        self._symbol_info_cache[symbol] = info
        return info

    def _round_lot(self, lot: float, step: float, vmin: float, vmax: float) -> float:
        """Round to broker's volume step, clamp to min/max."""
        lot = math.floor(lot / step) * step
        lot = round(lot, 2)
        lot = max(vmin, min(vmax, lot))
        return lot

    # -------------------------------------------------------------------------
    # Main entry point
    # -------------------------------------------------------------------------
    def calculate(self, symbol: str, account_balance: float,
                  entry_price: float, stop_loss_price: float,
                  risk_per_trade_pct: float = 0.005,
                  max_lot_per_1k: float = 0.01,
                  max_lot_absolute: float = 1.0) -> Dict:
        """
        Calculate the correct lot size.
        
        Args:
            symbol:            e.g. "EURUSD"
            account_balance:   $
            entry_price:       intended entry
            stop_loss_price:   where we'd cut
            risk_per_trade_pct: fraction of balance to risk (0.005 = 0.5%)
            max_lot_per_1k:    hard cap — max lot per $1k of balance
            max_lot_absolute:  hard cap — never exceed this lot size
        
        Returns:
            dict with {lot, risk_amount, sl_pips, pip_value, can_trade, reason}
        """
        info = self._get_symbol_info(symbol)

        # Stop-loss distance
        sl_distance_price = abs(entry_price - stop_loss_price)
        if sl_distance_price <= 0:
            return {"lot": 0.0, "can_trade": False, "reason": "sl_distance <= 0"}

        pip = pip_value(symbol)
        sl_pips = sl_distance_price / pip

        # Pip value per 1.0 lot (USD) — strip broker suffix for lookup
        base_sym = symbol.upper().rstrip("M")
        pv_per_lot = _PIP_VALUE_PER_LOT_USD.get(base_sym, 10.0)

        # Risk amount in USD
        risk_amount = account_balance * risk_per_trade_pct

        # Lot size calc from risk
        loss_per_lot = sl_pips * pv_per_lot
        if loss_per_lot <= 0:
            return {"lot": 0.0, "can_trade": False, "reason": "loss_per_lot <= 0"}

        lot_from_risk = risk_amount / loss_per_lot

        # --- HARD CAPS (prevents runaway lot sizes from tight stops) ---
        lot_cap_balance = (account_balance / 1000.0) * max_lot_per_1k
        lot = min(lot_from_risk, lot_cap_balance, max_lot_absolute)

        # Was the trade lot-capped (not risk-capped)?
        capped = lot < lot_from_risk

        # Round to broker step
        lot = self._round_lot(lot, info["volume_step"],
                             info["volume_min"], info["volume_max"])

        if lot < info["volume_min"]:
            return {
                "lot": 0.0, "can_trade": False,
                "reason": f"calculated lot {lot} below min {info['volume_min']}"
            }

        # Actual risk after capping
        actual_risk = lot * loss_per_lot

        return {
            "lot":         lot,
            "risk_amount": round(actual_risk, 2),
            "target_risk": round(risk_amount, 2),
            "sl_pips":     round(sl_pips, 1),
            "pip_value":   pv_per_lot,
            "expected_loss_if_stopped": round(actual_risk, 2),
            "capped":      capped,
            "can_trade":   True,
            "reason":      ("ok (lot-capped)" if capped else "ok"),
        }
