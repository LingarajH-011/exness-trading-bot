"""
=============================================================================
  MT5 CONNECTOR — The bridge between Python bot and MT5 terminal
  
  This is the "API" layer for forex — equivalent to ccxt for crypto.
  Uses the official MetaTrader5 Python package.
  
  Architecture:
    [Python Bot] <--MetaTrader5 lib--> [MT5 Terminal] <--FTMO server--> [FTMO]
  
  Install: pip install MetaTrader5
  Note: MetaTrader5 lib only works on Windows. For Mac/Linux use Wine or
        use the MQL5 EA fallback (core/mql5_bridge.py).
=============================================================================
"""
from __future__ import annotations
import logging
import pandas as pd
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

# Attempt to import MetaTrader5. Provide a graceful fallback if missing
# (this lets the dashboard run in demo mode on systems without MT5 installed).
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore
    MT5_AVAILABLE = False

log = logging.getLogger(__name__)


# =============================================================================
# TIMEFRAME MAPPING
# =============================================================================
_TF_MAP = {
    "M1":  1,   "M5":  5,    "M15": 15,   "M30": 30,
    "H1":  16385, "H4": 16388, "D1": 16408, "W1": 32769, "MN1": 49153,
}


def _tf_const(tf: str) -> int:
    """Map string timeframe to MT5 constant."""
    if not MT5_AVAILABLE:
        return 15
    mapping = {
        "M1":  mt5.TIMEFRAME_M1,  "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,  "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,  "W1":  mt5.TIMEFRAME_W1,
    }
    return mapping.get(tf, mt5.TIMEFRAME_M15)


# =============================================================================
# MT5 CLIENT
# =============================================================================

class MT5Client:
    """
    Wrapper around MetaTrader5 Python package.
    
    Handles:
        - Connection lifecycle (init / login / shutdown)
        - Market data (fetch_ohlcv, get_tick, get_symbol_info)
        - Account info (balance, equity, margin)
        - Order placement (market_order, close_position)
        - Position management (get_positions, modify_position)
        - Error handling and automatic reconnection
    """

    def __init__(self, login: int = 0, password: str = "",
                 server: str = "", path: str = "", timeout: int = 60000):
        self.login    = login
        self.password = password
        self.server   = server
        self.path     = path
        self.timeout  = timeout
        self.connected = False
        self._last_error: Optional[str] = None

    # -------------------------------------------------------------------------
    # Connection
    # -------------------------------------------------------------------------
    def connect(self) -> bool:
        """Initialize MT5 terminal and log in to trading account."""
        if not MT5_AVAILABLE:
            self._last_error = "MetaTrader5 package not installed"
            log.warning("MT5 library unavailable — running in demo/synthetic mode")
            return False

        # Step 1: initialize terminal connection
        kwargs: Dict[str, Any] = {"timeout": self.timeout}
        if self.path:
            kwargs["path"] = self.path
        if self.login and self.password and self.server:
            # Log in as part of initialize, so the saved details win over whatever
            # account the terminal was last logged in to.
            kwargs.update(login=int(self.login), password=self.password, server=self.server)

        if not mt5.initialize(**kwargs):
            self._last_error = f"initialize() failed: {mt5.last_error()}"
            log.error(self._last_error)
            return False

        # Step 2: log in to the trading account (if credentials provided)
        if self.login and self.password:
            ok = mt5.login(login=self.login, password=self.password,
                          server=self.server, timeout=self.timeout)
            if not ok:
                self._last_error = f"login() failed: {mt5.last_error()}"
                log.error(self._last_error)
                mt5.shutdown()
                return False

        self.connected = True
        info = mt5.account_info()
        if info:
            log.info(f"Connected to MT5. Account #{info.login} on {info.server}, "
                    f"balance ${info.balance:.2f}")
        return True

    def disconnect(self) -> None:
        """Shut down MT5 connection."""
        if MT5_AVAILABLE and self.connected:
            mt5.shutdown()
        self.connected = False

    def last_error(self) -> Optional[str]:
        return self._last_error

    # -------------------------------------------------------------------------
    # Account info
    # -------------------------------------------------------------------------
    def account_info(self) -> Dict[str, Any]:
        """Return current account state. Empty dict if not connected."""
        if not (MT5_AVAILABLE and self.connected):
            return {}
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "login":    info.login,
            "server":   info.server,
            "currency": info.currency,
            "balance":  info.balance,
            "equity":   info.equity,
            "margin":   info.margin,
            "free_margin": info.margin_free,
            "profit":   info.profit,
            "leverage": info.leverage,
            "name":     info.name,
            "company":  info.company,
        }

    # -------------------------------------------------------------------------
    # Market data
    # -------------------------------------------------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str = "M15",
                    bars: int = 500) -> pd.DataFrame:
        """Fetch OHLCV candles for a symbol. Returns empty df on failure."""
        if not (MT5_AVAILABLE and self.connected):
            return pd.DataFrame()

        if not mt5.symbol_select(symbol, True):
            log.warning(f"Could not select symbol {symbol}")
            return pd.DataFrame()

        rates = mt5.copy_rates_from_pos(symbol, _tf_const(timeframe), 0, bars)
        if rates is None or len(rates) == 0:
            log.warning(f"No data for {symbol} {timeframe}")
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def get_tick(self, symbol: str) -> Dict[str, float]:
        """Get latest bid/ask/last for a symbol."""
        if not (MT5_AVAILABLE and self.connected):
            return {}
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {}
        return {"bid": tick.bid, "ask": tick.ask, "last": tick.last,
                "time": tick.time, "spread": tick.ask - tick.bid}

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Get symbol metadata (pip size, contract size, min lot, etc.)."""
        if not (MT5_AVAILABLE and self.connected):
            return {}
        info = mt5.symbol_info(symbol)
        if info is None:
            return {}
        return {
            "symbol":        info.name,
            "digits":        info.digits,
            "point":         info.point,
            "spread":        info.spread,
            "trade_contract_size": info.trade_contract_size,
            "volume_min":    info.volume_min,
            "volume_max":    info.volume_max,
            "volume_step":   info.volume_step,
            "margin_initial": info.margin_initial,
            "trade_mode":    info.trade_mode,
        }

    # -------------------------------------------------------------------------
    # Trading — order placement
    # -------------------------------------------------------------------------
    def market_order(self, symbol: str, side: str, volume: float,
                     sl: float = 0.0, tp: float = 0.0,
                     comment: str = "ftmo_bot",
                     magic: int = 20260416) -> Dict[str, Any]:
        """
        Place a market order.
        
        Args:
            symbol: e.g. "EURUSD"
            side:   "buy" or "sell"
            volume: lot size (e.g. 0.01)
            sl, tp: stop-loss and take-profit PRICES (not pips)
            comment: order comment
            magic:  magic number (identifies our bot's orders)
        
        Returns:
            dict with {success, ticket, price, message}
        """
        if not (MT5_AVAILABLE and self.connected):
            return {"success": False, "message": "MT5 not connected"}

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "message": f"No tick for {symbol}"}

        order_type = mt5.ORDER_TYPE_BUY if side.lower() == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side.lower() == "buy" else tick.bid

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    symbol,
            "volume":    volume,
            "type":      order_type,
            "price":     price,
            "sl":        sl,
            "tp":        tp,
            "deviation": 20,
            "magic":     magic,
            "comment":   comment,
            "type_time":     mt5.ORDER_TIME_GTC,
            "type_filling":  mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return {"success": False, "message": f"order_send returned None: {mt5.last_error()}"}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"success": False, "ticket": 0, "price": price,
                   "message": f"retcode={result.retcode}, comment={result.comment}"}

        log.info(f"Order filled: {side.upper()} {volume} {symbol} @ {result.price}")
        return {"success": True, "ticket": result.order, "price": result.price,
                "volume": result.volume, "message": "OK"}

    def close_position(self, ticket: int) -> Dict[str, Any]:
        """Close an open position by ticket."""
        if not (MT5_AVAILABLE and self.connected):
            return {"success": False, "message": "MT5 not connected"}

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"success": False, "message": f"Position {ticket} not found"}

        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)

        # Opposite side closes the position
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    pos.symbol,
            "volume":    pos.volume,
            "type":      close_type,
            "position":  ticket,
            "price":     price,
            "deviation": 20,
            "magic":     pos.magic,
            "comment":   "ftmo_bot_close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"success": False, "message": f"Close failed: {mt5.last_error()}"}

        return {"success": True, "ticket": ticket, "price": result.price}

    def close_all_positions(self, symbol: Optional[str] = None,
                            magic: Optional[int] = None) -> int:
        """
        Close all open positions (optionally filtered by symbol or magic).
        Returns number of positions closed.
        """
        if not (MT5_AVAILABLE and self.connected):
            return 0

        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if not positions:
            return 0

        closed = 0
        for p in positions:
            if magic is not None and p.magic != magic:
                continue
            res = self.close_position(p.ticket)
            if res.get("success"):
                closed += 1
        return closed

    # -------------------------------------------------------------------------
    # Position / history queries
    # -------------------------------------------------------------------------
    def get_positions(self, magic: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return list of open positions."""
        if not (MT5_AVAILABLE and self.connected):
            return []
        positions = mt5.positions_get()
        if positions is None:
            return []
        result = []
        for p in positions:
            if magic is not None and p.magic != magic:
                continue
            result.append({
                "ticket":  p.ticket,
                "symbol":  p.symbol,
                "side":    "buy" if p.type == 0 else "sell",
                "volume":  p.volume,
                "price_open":    p.price_open,
                "price_current": p.price_current,
                "sl":      p.sl,
                "tp":      p.tp,
                "profit":  p.profit,
                "swap":    p.swap,
                "time":    datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
                "magic":   p.magic,
                "comment": p.comment,
            })
        return result

    def get_history(self, days: int = 30, magic: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return historical deals for the last N days."""
        if not (MT5_AVAILABLE and self.connected):
            return []
        from_date = datetime.now(tz=timezone.utc).timestamp() - days * 86400
        to_date   = datetime.now(tz=timezone.utc).timestamp()
        deals = mt5.history_deals_get(int(from_date), int(to_date))
        if deals is None:
            return []
        result = []
        for d in deals:
            if magic is not None and d.magic != magic:
                continue
            result.append({
                "ticket":  d.ticket,
                "symbol":  d.symbol,
                "type":    d.type,
                "volume":  d.volume,
                "price":   d.price,
                "profit":  d.profit,
                "time":    datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                "magic":   d.magic,
                "comment": d.comment,
            })
        return result
