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
  Momentum: ADX(14) ≥ 25 on 15M + DI direction confirmation
  Filters : SL 10–70 pts | Daily loss 8%
  Excluded: No VIX filter | No re-entry cooldown | No clustering
"""

from datetime import time

# ── Fyers API Credentials ──────────────────────────────────────────────────────
FYERS_CONFIG = {
    "client_id":    "AK7J3R8CMX-100",       # e.g. "XY1234-100"  (from Fyers API dashboard)
    "secret_key":   "2VB8VZUSZF",      # from Fyers API dashboard
    "redirect_uri": "https://www.google.com", 
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOlsiZDoxIiwiZDoyIiwieDowIiwieDoxIiwieDoyIl0sImF0X2hhc2giOiJnQUFBQUFCcHpfRVRyUmx3WHRoX0ZjaGp6UjZZc2NQbll4OUdtcTFjTkpQQ1pJSUFTSXdSdFg3SnM1WWF4anNMRThNUVd0XzZJZUl3bnJ6dEVPU0IwVTc4ZkR3ZGFHQ0lkZ3hrU1FzOFNNQkhueHBOR3pmSmFpaz0iLCJkaXNwbGF5X25hbWUiOiIiLCJvbXMiOiJLMSIsImhzbV9rZXkiOiJhYzhmZTc1ZWI0OTIwNTVjZmE5ODYwNWZhMWYyMTVlYzYzNzUzMjRjODQ4MzE4OWQ4YTk1ZGRiNiIsImlzRGRwaUVuYWJsZWQiOiJOIiwiaXNNdGZFbmFibGVkIjoiTiIsImZ5X2lkIjoiRkFBNTk2MzciLCJhcHBUeXBlIjoxMDAsImV4cCI6MTc3NTI2MjYwMCwiaWF0IjoxNzc1MjM1MzQ3LCJpc3MiOiJhcGkuZnllcnMuaW4iLCJuYmYiOjE3NzUyMzUzNDcsInN1YiI6ImFjY2Vzc190b2tlbiJ9.uF_LQZiCOJ7dZEZ5vNmkUoezE3eWQLqi3yiesGfJfZU",    # updated daily by auth_token.py
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
    "max_sl_pts":           50.0,       # skip if SL > 50 pts  (lottery)

    # ── Daily risk limit ───────────────────────────────────────────────────────
    "daily_loss_limit_pct": 0.08,       # no new entries if down 8% on the day

    # ── ADX momentum filter (15M) ──────────────────────────────────────────────
    # Wilder's ADX(14). Confirms trending market before entry.
    # BUY CE : ADX ≥ adx_min  AND  +DI > -DI
    # BUY PE : ADX ≥ adx_min  AND  -DI > +DI
    # Fail   : choppy market → skip signal
    # Fail-open: if ADX data unavailable → allow trade
    "adx_period":           14,
    "adx_min":              25,
    "adx_enabled":          True,

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
