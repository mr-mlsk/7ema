"""
signal_engine.py — 7 EMA Signal Engine
=======================================
Fetches live Fyers OHLC candles and computes the pure
7 EMA retest strategy signals.

Methods used by forward_test.py:
  get_hourly_bias()            -> 'bullish' | 'bearish' | 'neutral'
  get_entry_signal(bias)       -> signal dict or None
  get_current_nifty_price()    -> float
  get_latest_15m_candle_time() -> str
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
from fyers_apiv3 import fyersModel

logger = logging.getLogger(__name__)

CHUNK_DAYS = 85


class SignalEngine:
    def __init__(self, fyers: fyersModel.FyersModel, config: dict):
        self.fyers      = fyers
        self.config     = config
        self.ema_period = config["ema_period"]
        self.bias_res   = config["bias_resolution"]
        self.entry_res  = config["entry_resolution"]
        self.symbol     = config["index_symbol"]

    # ── Private helpers ────────────────────────────────────────────────────────

    def _fetch(self, resolution: str, days: int = 10) -> pd.DataFrame:
        """Fetch OHLC candles from Fyers with date chunking."""
        all_rows = []
        cursor   = datetime.now() - timedelta(days=days)
        end_dt   = datetime.now()

        while cursor <= end_dt:
            chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end_dt)
            resp = self.fyers.history(data={
                "symbol":      self.symbol,
                "resolution":  resolution,
                "date_format": "1",
                "range_from":  cursor.strftime("%Y-%m-%d"),
                "range_to":    chunk_end.strftime("%Y-%m-%d"),
                "cont_flag":   "1",
            })
            if resp.get("s") != "ok":
                raise ValueError(
                    f"Fyers history error: {resp.get('message', resp)}")
            all_rows.extend(resp.get("candles", []))
            cursor = chunk_end + timedelta(days=1)

        df = pd.DataFrame(
            all_rows,
            columns=["epoch", "open", "high", "low", "close", "volume"])
        df["date"] = (pd.to_datetime(df["epoch"], unit="s", utc=True)
                        .dt.tz_convert("Asia/Kolkata"))
        df.set_index("date", inplace=True)
        df.drop(columns=["epoch"], inplace=True)
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)
        return df

    def _add_ema(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add EMA7 column to dataframe."""
        df = df.copy()
        df["ema7"] = df["close"].ewm(
            span=self.ema_period, adjust=False).mean()
        return df

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_hourly_bias(self) -> str:
        """
        Directional bias from last CLOSED 1H candle.

        Bullish : last closed 1H candle close > EMA7  (buy CE)
        Bearish : last closed 1H candle close < EMA7  (buy PE)
        Neutral : close ~= EMA7                       (no trade)

        Uses iloc[-2] -- iloc[-1] is the still-forming candle.
        Returns 'bullish' | 'bearish' | 'neutral'
        """
        try:
            df   = self._add_ema(self._fetch(self.bias_res, days=10))
            last = df.iloc[-2]
            c, e = last["close"], last["ema7"]
            if c > e * 1.0001:
                return "bullish"
            if c < e * 0.9999:
                return "bearish"
            return "neutral"
        except Exception as ex:
            logger.error(f"get_hourly_bias: {ex}")
            return "neutral"

    def get_entry_signal(self, bias: str) -> dict | None:
        """
        15M retest signal -- Berlin Mindset 7 EMA rule.

        BULLISH (BUY CE):
          c_n2 closed ABOVE EMA7        <- trend was intact
          c_n1 low  <= EMA7             <- candle retested EMA
          c_n1 close > EMA7             <- closed back above (rejection)
          SL  = c_n1 low

        BEARISH (BUY PE):
          c_n2 closed BELOW EMA7
          c_n1 high >= EMA7             <- candle retested EMA
          c_n1 close < EMA7             <- closed back below (rejection)
          SL  = c_n1 high

        candle_time is stored as str() so comparison with last_sig_candle
        is always exact regardless of timezone metadata differences
        between separate Fyers API fetches.

        Returns dict: direction, entry_price, sl, sl_distance,
                      candle_time (str), ema7_at_entry
        Returns None if no valid signal on this candle.
        """
        try:
            df = self._add_ema(self._fetch(self.entry_res, days=3))
            if len(df) < 4:
                return None

            c_n2 = df.iloc[-3]
            c_n1 = df.iloc[-2]    # last fully closed 15M candle

            if bias == "bullish":
                if (c_n2["close"] > c_n2["ema7"]
                        and c_n1["low"]   <= c_n1["ema7"]
                        and c_n1["close"] >  c_n1["ema7"]):
                    dist = c_n1["close"] - c_n1["low"]
                    if dist > 0:
                        logger.info(
                            f"[SIGNAL] BULLISH | "
                            f"entry={c_n1['close']:.2f}  "
                            f"sl={c_n1['low']:.2f}  "
                            f"dist={dist:.2f}pts")
                        return {
                            "direction":     "buy",
                            "entry_price":   c_n1["close"],
                            "sl":            c_n1["low"],
                            "sl_distance":   round(dist, 2),
                            "candle_time":   str(df.index[-2]),
                            "ema7_at_entry": round(c_n1["ema7"], 2),
                        }

            elif bias == "bearish":
                if (c_n2["close"] < c_n2["ema7"]
                        and c_n1["high"]  >= c_n1["ema7"]
                        and c_n1["close"] <  c_n1["ema7"]):
                    dist = c_n1["high"] - c_n1["close"]
                    if dist > 0:
                        logger.info(
                            f"[SIGNAL] BEARISH | "
                            f"entry={c_n1['close']:.2f}  "
                            f"sl={c_n1['high']:.2f}  "
                            f"dist={dist:.2f}pts")
                        return {
                            "direction":     "sell",
                            "entry_price":   c_n1["close"],
                            "sl":            c_n1["high"],
                            "sl_distance":   round(dist, 2),
                            "candle_time":   str(df.index[-2]),
                            "ema7_at_entry": round(c_n1["ema7"], 2),
                        }
            return None

        except Exception as ex:
            logger.error(f"get_entry_signal: {ex}")
            return None

    def get_current_nifty_price(self) -> float:
        """Return live Nifty spot price from Fyers quotes API."""
        resp = self.fyers.quotes(data={"symbols": self.symbol})
        if resp.get("s") != "ok":
            raise ValueError(
                f"Quotes error: {resp.get('message', resp)}")
        return float(resp["d"][0]["v"]["lp"])

    def get_latest_15m_candle_time(self) -> str:
        """
        Return the timestamp of the last fully closed 15M candle as str.
        Used by forward_test.py to detect new candles for step-down logic.
        Returned as str for reliable equality comparison across calls.
        """
        df = self._add_ema(self._fetch(self.entry_res, days=1))
        return str(df.index[-2])
