"""
signal_engine.py — 7 EMA Signal Engine + ADX Momentum Filter
=============================================================
Fetches live Fyers OHLC candles, computes 7 EMA for bias and entry,
and computes Wilder's ADX(14) on 15M candles for momentum confirmation.

Methods used by forward_test.py:
  get_hourly_bias()            -> 'bullish' | 'bearish' | 'neutral'
  get_entry_signal(bias)       -> signal dict or None
  get_adx(direction)           -> (pass, adx, plus_di, minus_di)
  get_current_nifty_price()    -> float
  get_latest_15m_candle_time() -> timestamp
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
from fyers_apiv3 import fyersModel

logger = logging.getLogger(__name__)

CHUNK_DAYS = 85


class SignalEngine:
    def __init__(self, fyers: fyersModel.FyersModel, config: dict):
        self.fyers       = fyers
        self.config      = config
        self.ema_period  = config["ema_period"]
        self.bias_res    = config["bias_resolution"]
        self.entry_res   = config["entry_resolution"]
        self.symbol      = config["index_symbol"]
        self.adx_period  = config.get("adx_period",  14)
        self.adx_min     = config.get("adx_min",     25)
        self.adx_enabled = config.get("adx_enabled", True)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _fetch(self, resolution: str, days: int = 10) -> pd.DataFrame:
        """Fetch OHLC candles from Fyers with chunking."""
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
        df["ema7"] = df["close"].ewm(span=self.ema_period, adjust=False).mean()
        return df

    @staticmethod
    def _wilder(series: pd.Series, period: int) -> pd.Series:
        """
        Wilder's smoothing method.
        First value = simple average of first `period` values.
        Subsequent  = prev * (period-1)/period + current * 1/period
        """
        result = [float("nan")] * len(series)
        vals   = series.values
        if period - 1 >= len(vals):
            return pd.Series(result, index=series.index)
        result[period - 1] = sum(vals[:period]) / period
        for i in range(period, len(vals)):
            result[i] = result[i-1] * (period-1)/period + vals[i]/period
        return pd.Series(result, index=series.index)

    def _add_adx(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute Wilder's ADX(period), +DI, -DI on an OHLC dataframe.

        Algorithm:
          TR     = max(high-low, |high-prev_close|, |low-prev_close|)
          +DM    = high-prev_high  if > (prev_low-low) and > 0, else 0
          -DM    = prev_low-low    if > (high-prev_high) and > 0, else 0
          +DI    = 100 * Wilder(+DM) / Wilder(TR)
          -DI    = 100 * Wilder(-DM) / Wilder(TR)
          DX     = 100 * |+DI - -DI| / (+DI + -DI)
          ADX    = Wilder(DX)
        """
        p  = self.adx_period
        df = df.copy()

        prev_close = df["close"].shift(1)
        prev_high  = df["high"].shift(1)
        prev_low   = df["low"].shift(1)

        # True Range
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)

        # Raw directional movement
        up   = df["high"] - prev_high
        down = prev_low   - df["low"]

        plus_dm  = ((up > down) & (up > 0)).astype(float) * up
        minus_dm = ((down > up) & (down > 0)).astype(float) * down

        # Wilder smoothed
        s_tr  = self._wilder(tr,       p)
        s_pdm = self._wilder(plus_dm,  p)
        s_ndm = self._wilder(minus_dm, p)

        # DI lines — guard zero division
        pdi = s_pdm.div(s_tr.replace(0, float("nan"))).fillna(0) * 100
        ndi = s_ndm.div(s_tr.replace(0, float("nan"))).fillna(0) * 100

        # DX
        di_sum = pdi + ndi
        dx = ((pdi - ndi).abs()
              .div(di_sum.replace(0, float("nan")))
              .fillna(0) * 100)

        df["adx"]      = self._wilder(dx, p).round(2)
        df["plus_di"]  = pdi.round(2)
        df["minus_di"] = ndi.round(2)
        return df

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_hourly_bias(self) -> str:
        """
        Directional bias from last CLOSED 1H candle.
        Returns 'bullish' | 'bearish' | 'neutral'.
        """
        try:
            df   = self._add_ema(self._fetch(self.bias_res, days=10))
            last = df.iloc[-2]          # iloc[-1] is still forming
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
        15M retest signal — Berlin Mindset 7 EMA rule.

        BULLISH (bias='bullish'):
          c_n2 closed above EMA7              (prior candle confirms trend)
          c_n1 low  <= EMA7 AND close > EMA7  (retest and rejection)
          SL = c_n1 low

        BEARISH (bias='bearish'):
          c_n2 closed below EMA7
          c_n1 high >= EMA7 AND close < EMA7
          SL = c_n1 high
        """
        try:
            df = self._add_ema(self._fetch(self.entry_res, days=3))
            if len(df) < 4:
                return None

            c_n2 = df.iloc[-3]
            c_n1 = df.iloc[-2]      # last fully closed candle

            if bias == "bullish":
                if (c_n2["close"] > c_n2["ema7"]
                        and c_n1["low"]   <= c_n1["ema7"]
                        and c_n1["close"] >  c_n1["ema7"]):
                    dist = c_n1["close"] - c_n1["low"]
                    if dist > 0:
                        logger.info(
                            f"[SIGNAL] BULLISH | "
                            f"entry={c_n1['close']:.2f} "
                            f"sl={c_n1['low']:.2f} "
                            f"dist={dist:.2f}pts")
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
                            f"[SIGNAL] BEARISH | "
                            f"entry={c_n1['close']:.2f} "
                            f"sl={c_n1['high']:.2f} "
                            f"dist={dist:.2f}pts")
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

    def get_adx(self, direction: str) -> tuple[bool, float, float, float]:
        """
        Fetch 15M candles, compute Wilder's ADX(14), return momentum check.

        Returns: (adx_pass, adx_value, plus_di, minus_di)

        Pass conditions:
          ADX >= adx_min  AND  +DI > -DI   → BUY CE confirmed
          ADX >= adx_min  AND  -DI > +DI   → BUY PE confirmed

        Fail conditions:
          ADX < adx_min                    → choppy/sideways, skip
          DI direction misaligned          → momentum against signal

        Fail-open on error: if ADX data cannot be fetched or computed,
        returns True so a temporary data issue doesn't block all trades.
        """
        if not self.adx_enabled:
            return True, 0.0, 0.0, 0.0

        try:
            # Need period*2 candles minimum for Wilder to warm up fully
            # 10 days of 15M gives ~260 candles — sufficient
            df  = self._add_adx(self._fetch(self.entry_res, days=10))
            row = df.iloc[-2]   # last fully closed candle

            adx = float(row.get("adx",      0))
            pdi = float(row.get("plus_di",  0))
            ndi = float(row.get("minus_di", 0))

            if pd.isna(adx) or adx == 0:
                logger.warning("[ADX] Not computed yet — fail-open")
                return True, adx, pdi, ndi

            if adx < self.adx_min:
                logger.info(
                    f"[ADX] SKIP — ADX={adx:.1f} < {self.adx_min} "
                    f"(choppy, no trend)")
                return False, adx, pdi, ndi

            if direction == "buy" and pdi <= ndi:
                logger.info(
                    f"[ADX] SKIP — BUY CE: "
                    f"+DI({pdi:.1f}) ≤ -DI({ndi:.1f}) "
                    f"(bearish momentum)")
                return False, adx, pdi, ndi

            if direction == "sell" and ndi <= pdi:
                logger.info(
                    f"[ADX] SKIP — BUY PE: "
                    f"-DI({ndi:.1f}) ≤ +DI({pdi:.1f}) "
                    f"(bullish momentum)")
                return False, adx, pdi, ndi

            logger.info(
                f"[ADX] PASS — ADX={adx:.1f} "
                f"+DI={pdi:.1f} -DI={ndi:.1f}")
            return True, adx, pdi, ndi

        except Exception as ex:
            logger.warning(f"[ADX] Error: {ex} — fail-open")
            return True, 0.0, 0.0, 0.0

    def get_current_nifty_price(self) -> float:
        """Return live Nifty spot price."""
        resp = self.fyers.quotes(data={"symbols": self.symbol})
        if resp.get("s") != "ok":
            raise ValueError(f"Quotes error: {resp.get('message', resp)}")
        return float(resp["d"][0]["v"]["lp"])

    def get_latest_15m_candle_time(self):
        """Return the timestamp of the last fully closed 15M candle."""
        df = self._add_ema(self._fetch(self.entry_res, days=1))
        return df.index[-2]
