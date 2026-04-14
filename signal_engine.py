"""
signal_engine.py — 7 EMA Signal Engine
Fetches real Fyers OHLC candles, computes 7 EMA,
detects hourly bias and 15-min retest entry signals.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
from fyers_apiv3 import fyersModel

logger = logging.getLogger(__name__)

CHUNK_DAYS = 85   # Fyers intraday limit per request


class SignalEngine:
    def __init__(self, fyers: fyersModel.FyersModel, config: dict):
        self.fyers       = fyers
        self.config      = config
        self.ema_period  = config["ema_period"]
        self.bias_res    = config["bias_resolution"]
        self.entry_res   = config["entry_resolution"]
        self.symbol      = config["index_symbol"]

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch(self, resolution: str, days: int = 10) -> pd.DataFrame:
        """Fetch OHLC from Fyers with chunking and date_format=1."""
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

        df = pd.DataFrame(all_rows,
                          columns=["epoch","open","high","low","close","volume"])
        df["date"] = (pd.to_datetime(df["epoch"], unit="s", utc=True)
                        .dt.tz_convert("Asia/Kolkata"))
        df.set_index("date", inplace=True)
        df.drop(columns=["epoch"], inplace=True)
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)
        return df

    def _add_ema(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema7"] = df["close"].ewm(
            span=self.ema_period, adjust=False).mean()
        return df

    # ── Public ─────────────────────────────────────────────────────────────────

    def get_hourly_bias(self) -> str:
        """
        1H bias using last CLOSED candle.
        Returns 'bullish' | 'bearish' | 'neutral'
        """
        try:
            df   = self._add_ema(self._fetch(self.bias_res, days=10))
            last = df.iloc[-2]   # iloc[-1] is still forming
            c, e = last["close"], last["ema7"]
            logger.debug(f"[1H] close={c:.2f} ema7={e:.2f}")
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
        15-min retest signal — mirrors Berlin Mindset rule exactly.

        BULLISH (bias == 'bullish'):
          c_n2 closed above EMA  (trend was up)
          c_n1 low <= EMA  AND  c_n1 closed above EMA  (retest confirmed)
          SL = low of c_n1

        BEARISH (bias == 'bearish'):
          c_n2 closed below EMA
          c_n1 high >= EMA  AND  c_n1 closed below EMA
          SL = high of c_n1
        """
        try:
            df = self._add_ema(self._fetch(self.entry_res, days=3))
            if len(df) < 4:
                return None

            c_n2 = df.iloc[-3]
            c_n1 = df.iloc[-2]   # last closed candle

            if bias == "bullish":
                if (c_n2["close"] > c_n2["ema7"]
                        and c_n1["low"]   <= c_n1["ema7"]
                        and c_n1["close"] >  c_n1["ema7"]):
                    dist = c_n1["close"] - c_n1["low"]
                    if dist > 0:
                        logger.info(
                            f"[SIGNAL] BULLISH | entry={c_n1['close']:.2f} "
                            f"sl={c_n1['low']:.2f} dist={dist:.2f}")
                        return {
                            "direction":     "buy",
                            "entry_price":   c_n1["close"],
                            "sl":            c_n1["low"],
                            "sl_distance":   dist,
                            "candle_time":   df.index[-2],
                            "ema7_at_entry": round(c_n1["ema7"], 2),
                        }

            elif bias == "bearish":
                if (c_n2["close"] < c_n2["ema7"]
                        and c_n1["high"]  >= c_n1["ema7"]
                        and c_n1["close"] <  c_n1["ema7"]):
                    dist = c_n1["high"] - c_n1["close"]
                    if dist > 0:
                        logger.info(
                            f"[SIGNAL] BEARISH | entry={c_n1['close']:.2f} "
                            f"sl={c_n1['high']:.2f} dist={dist:.2f}")
                        return {
                            "direction":     "sell",
                            "entry_price":   c_n1["close"],
                            "sl":            c_n1["high"],
                            "sl_distance":   dist,
                            "candle_time":   df.index[-2],
                            "ema7_at_entry": round(c_n1["ema7"], 2),
                        }
            return None

        except Exception as ex:
            logger.error(f"get_entry_signal: {ex}")
            return None

    def get_current_nifty_price(self) -> float:
        resp = self.fyers.quotes(data={"symbols": self.symbol})
        if resp.get("s") != "ok":
            raise ValueError(f"Quotes error: {resp.get('message', resp)}")
        return float(resp["d"][0]["v"]["lp"])

    def get_latest_15m_candle_time(self):
        df = self._add_ema(self._fetch(self.entry_res, days=1))
        return df.index[-2]   # last closed candle timestamp
