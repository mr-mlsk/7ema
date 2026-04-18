"""
config.py — 7 EMA Nifty Options Bot
=====================================
Single source of truth for all strategy parameters.

STRATEGY SUMMARY:
  Signal  : 7 EMA retest on 15M with 1H directional bias
  Sizing  : Fixed 1 lot per trade (no dynamic sizing)
  SL      : Candle low (BUY CE) / candle high (BUY PE)
  Target  : Entry ± SL distance × RR  (initial 1:3)
  Step-down: 1:3 → 1:2 after 6 candles → 1:1 after 12 candles
  Filters : SL 10–70 pts | Daily loss 8%
  Excluded: No ADX | No VIX | No re-entry cooldown | No clustering
"""

from datetime import time

# ── Fyers API Credentials ──────────────────────────────────────────────────────
FYERS_CONFIG = {
    "client_id":    "YOUR_CLIENT_ID",       # e.g. "XY12345-100"
    "secret_key":   "YOUR_SECRET_KEY",
    "redirect_uri": "https://127.0.0.1:8080",
    "access_token": "YOUR_ACCESS_TOKEN",    # updated daily by auth_token.py
}

# ── Trading Parameters ─────────────────────────────────────────────────────────
TRADING_CONFIG = {

    # ── Index ──────────────────────────────────────────────────────────────────
    "index_symbol":         "NSE:NIFTY50-INDEX",
    "lot_size":             75,

    # ── Signal — 7 EMA ─────────────────────────────────────────────────────────
    "ema_period":           7,
    "bias_resolution":      "60",       # 1H candles for directional bias
    "entry_resolution":     "15",       # 15M candles for entry signal

    # ── Option selection ───────────────────────────────────────────────────────
    "strike_offset":        100,        # ATM ± 100 pts for strike
    "strike_step":          50,         # Nifty strikes in multiples of 50
    "min_days_to_expiry":   7,          # select expiry ≥ 7 days away

    # ── Position sizing — FIXED 1 LOT ─────────────────────────────────────────
    "capital":              50_000,
    "lots":                 1,          # fixed 1 lot every trade

    # ── Signal quality filters ─────────────────────────────────────────────────
    "min_sl_pts":           10.0,       # skip if SL < 10 pts  (noise)
    "max_sl_pts":           70.0,       # skip if SL > 70 pts  (lottery)

    # ── Daily risk limit ───────────────────────────────────────────────────────
    "daily_loss_limit_pct": 0.08,       # no new entries if down 8% on the day

    # ── Exit — step-down RR ────────────────────────────────────────────────────
    "initial_rr":           3,
    "step_down_rules": [
        {"after_candles": 6,  "rr": 2},
        {"after_candles": 12, "rr": 1},
    ],

    # ── Intraday time limits ───────────────────────────────────────────────────
    "max_trades_per_day":   3,
    "no_new_entry_after":   time(14, 30),
    "force_exit_at":        time(15, 15),

    # ── Forward test ───────────────────────────────────────────────────────────
    "simulation_mode":      True,
    "poll_interval":        15,
}
