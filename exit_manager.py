"""
exit_manager.py — Trade State & Step-Down Exit Logic
======================================================
Clean, simple implementation.  No partial exits.  Full position
is held from entry until one of these four exit events:

  1. SL_HIT             — spot crosses sl_underlying
  2. TARGET_HIT_1:X     — spot reaches target_price_underlying
  3. FORCE_EXIT_EOD     — 3:15 PM hard exit (called externally)
  4. MANUAL_STOP        — Ctrl+C (called externally)

Step-down (time-based):
  After 6 candles (90 min) without hitting 1:3 → step target to 1:2
  After 12 candles (3 hrs) without hitting 1:2 → step target to 1:1
  Step-down moves the TARGET only — never closes the trade.
  Once stepped down, the target cannot step back up.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeState:
    # ── Required at construction ───────────────────────────────────────────────
    direction:               str    # "buy" (CE) or "sell" (PE)
    entry_price_underlying:  float  # Nifty spot at entry candle close
    sl_underlying:           float  # candle low (buy) or candle high (sell)
    sl_distance:             float  # entry - sl  (never changes)
    tradingsymbol:           str    # Fyers option symbol
    entry_premium:           float  # option LTP at fill
    entry_order_id:          str
    initial_rr:              int    # always 3

    # ── Position ───────────────────────────────────────────────────────────────
    lots:                    int      = 1
    current_rr_target:       int      = 3

    # ── Computed at construction ───────────────────────────────────────────────
    target_price_underlying: float    = 0.0   # active target (steps down over time)
    target_1_3:              float    = 0.0   # entry ± sl_dist × 3 (fixed at entry)
    target_1_2:              float    = 0.0   # entry ± sl_dist × 2 (fixed at entry)
    target_1_1:              float    = 0.0   # entry ± sl_dist × 1 (fixed at entry)

    # ── Meta ───────────────────────────────────────────────────────────────────
    entry_time:              datetime = field(default_factory=datetime.now)
    candles_since_entry:     int      = 0
    is_active:               bool     = True

    def __post_init__(self):
        self._recalc_target()

    def _recalc_target(self):
        m = self.sl_distance * self.current_rr_target
        ep = self.entry_price_underlying
        if self.direction == "buy":
            self.target_price_underlying = round(ep + m, 2)
            # Fixed milestone levels (set once at entry, never change)
            if self.target_1_3 == 0.0:
                self.target_1_3 = round(ep + self.sl_distance * 3, 2)
                self.target_1_2 = round(ep + self.sl_distance * 2, 2)
                self.target_1_1 = round(ep + self.sl_distance * 1, 2)
        else:
            self.target_price_underlying = round(ep - m, 2)
            if self.target_1_3 == 0.0:
                self.target_1_3 = round(ep - self.sl_distance * 3, 2)
                self.target_1_2 = round(ep - self.sl_distance * 2, 2)
                self.target_1_1 = round(ep - self.sl_distance * 1, 2)

    def step_down_rr(self, new_rr: int):
        """Move target down to new_rr. Never moves up."""
        if new_rr >= self.current_rr_target:
            return
        old = self.current_rr_target
        self.current_rr_target = new_rr
        self._recalc_target()
        logger.info(
            f"[STEP-DOWN] 1:{old} → 1:{new_rr} | "
            f"New target: {self.target_price_underlying:.2f} | "
            f"Candles held: {self.candles_since_entry}")

    def status_line(self, spot: float) -> str:
        if self.direction == "buy":
            prog  = spot - self.entry_price_underlying
            to_t  = self.target_price_underlying - spot
            to_sl = spot - self.sl_underlying
        else:
            prog  = self.entry_price_underlying - spot
            to_t  = spot - self.target_price_underlying
            to_sl = self.sl_underlying - spot
        return (
            f"{'CE' if self.direction=='buy' else 'PE'} "
            f"{self.tradingsymbol} [{self.lots}L] | "
            f"spot={spot:.2f}  entry={self.entry_price_underlying:.2f} | "
            f"SL={self.sl_underlying:.2f} ({to_sl:.1f}away) | "
            f"Target={self.target_price_underlying:.2f} "
            f"1:{self.current_rr_target} ({to_t:.1f}away) | "
            f"P&L={prog:+.1f}pts | candles={self.candles_since_entry}")


class ExitManager:
    def __init__(self, config: dict):
        self.step_rules = sorted(
            config.get("step_down_rules", []),
            key=lambda r: r["after_candles"])

    def on_new_candle(self, trade: TradeState) -> TradeState:
        """
        Call on every new 15M candle close.
        Increments candle counter and applies step-down rules if due.
        """
        trade.candles_since_entry += 1
        for rule in self.step_rules:
            if (trade.candles_since_entry >= rule["after_candles"]
                    and trade.current_rr_target > rule["rr"]):
                trade.step_down_rr(rule["rr"])
        return trade

    def check_exit(self, trade: TradeState,
                   spot: float) -> Optional[str]:
        """
        Check if SL or target has been hit.
        SL is checked before target (conservative).
        Returns reason string or None.
        """
        if trade.direction == "buy":
            if spot <= trade.sl_underlying:
                return "SL_HIT"
            if spot >= trade.target_price_underlying:
                return f"TARGET_HIT_1:{trade.current_rr_target}"
        else:
            if spot >= trade.sl_underlying:
                return "SL_HIT"
            if spot <= trade.target_price_underlying:
                return f"TARGET_HIT_1:{trade.current_rr_target}"
        return None

    def check_force_exit(self, now: datetime) -> bool:
        from config import TRADING_CONFIG
        return now.time() >= TRADING_CONFIG["force_exit_at"]
