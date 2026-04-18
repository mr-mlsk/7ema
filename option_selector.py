"""
option_selector.py — Option Strike & Expiry Selector
=====================================================
Downloads Fyers NSE F&O symbol master CSV and selects the correct
CE/PE contract for each signal.

EXPIRY LOGIC (updated):
  Instead of hardcoding "next week Thursday", this reads the actual
  available expiry dates from the Fyers symbol master and picks the
  nearest one that has at least MIN_DAYS_TO_EXPIRY days remaining.

  Why this matters:
    - Guarantees minimum time value at entry
    - No gamma risk near expiry (last 3-5 days, delta swings wildly)
    - No accelerated theta decay (last week loses 70% of time value)
    - Works correctly around NSE holidays (some Thursdays → Wednesday expiry)
    - Naturally selects 2nd nearest expiry when nearest is too close

  Example (MIN_DAYS_TO_EXPIRY = 7):

    Today = Mon Jan 13  |  Available: Jan 16, Jan 23, Jan 30
      Jan 16 - Jan 13 = 3 days  → SKIP (< 7)
      Jan 23 - Jan 13 = 10 days → SELECT ✓

    Today = Thu Jan 16  |  Available: Jan 16, Jan 23, Jan 30
      Jan 16 - Jan 16 = 0 days  → SKIP
      Jan 23 - Jan 16 = 7 days  → SELECT ✓  (exactly 7, still valid)

    Today = Fri Jan 17  |  Available: Jan 23, Jan 30
      Jan 23 - Jan 17 = 6 days  → SKIP (< 7)
      Jan 30 - Jan 17 = 13 days → SELECT ✓  (jumps to 3rd calendar expiry)

Fyers symbol format:  NSE:NIFTY{YY}{MON}{DD}{STRIKE}{TYPE}
Example:              NSE:NIFTY25APR2324500CE
"""

import io
import logging
from datetime import date, timedelta

import pandas as pd
import requests
from fyers_apiv3 import fyersModel

logger = logging.getLogger(__name__)

SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"

SYMBOL_MASTER_COLS = [
    "fytoken", "symbol_details", "exchange_instrument_type",
    "minimum_lot_size", "tick_size", "isin", "trading_session",
    "last_update_time", "expiry_date", "symbol_ticker",
    "exchange", "segment", "scrip_code", "underlying_symbol",
    "close_price", "strike_price", "option_type", "underlying_fytoken",
    "reserved_col1", "reserved_col2",
]


class OptionSelector:
    def __init__(self, fyers: fyersModel.FyersModel, config: dict):
        self.fyers             = fyers
        self.config            = config
        self.strike_step       = config["strike_step"]
        self.strike_offset     = config["strike_offset"]
        self.min_days_to_expiry= config.get("min_days_to_expiry", 7)
        self._sym_df           = None

    # ── Private ────────────────────────────────────────────────────────────────

    def _load(self):
        if self._sym_df is not None:
            return
        logger.info("Downloading Fyers NSE F&O symbol master...")
        resp = requests.get(SYMBOL_MASTER_URL, timeout=30)
        resp.raise_for_status()

        # ── Step 1: Read raw CSV with no assumptions ───────────────────────────
        raw = pd.read_csv(
            io.StringIO(resp.text),
            header=None,
            dtype=str,
        )
        n_cols = len(raw.columns)
        logger.info(
            f"[SymbolMaster] CSV: {len(raw)} rows × {n_cols} cols "
            f"(expected {len(SYMBOL_MASTER_COLS)} cols)")

        # ── Step 2: Detect if row 0 is a header row ────────────────────────────
        # If Fyers added a header, skip it so data rows start at 1
        first_row = raw.iloc[0].astype(str).str.lower().tolist()
        has_header = any(v in first_row for v in
                         ["fytoken", "symbol_details", "expiry_date",
                          "strike_price", "option_type"])
        if has_header:
            raw = raw.iloc[1:].reset_index(drop=True)
            logger.info("[SymbolMaster] Header row detected and skipped")

        # ── Step 3: Assign column names up to what we know ────────────────────
        n = len(raw.columns)
        known = SYMBOL_MASTER_COLS[:min(n, len(SYMBOL_MASTER_COLS))]
        extras = [f"col_{i}" for i in range(len(known), n)]
        raw.columns = known + extras

        # ── Step 4: Auto-detect the underlying symbol column ──────────────────
        # The underlying_symbol column contains SHORT values like "NIFTY",
        # "NIFTY50", "NIFTY 50" — NOT full option symbols like "NIFTY25APR...CE"
        # We search for columns where values match short NIFTY (len <= 10)
        # and are NOT the full option symbol string.
        # This avoids picking symbol_details (col 1) which has long strings.
        best_col   = None
        best_count = 0

        for col in raw.columns:
            col_vals = raw[col].astype(str).str.strip().str.upper()
            # Only count SHORT values (underlying ticker, not option symbol)
            short_nifty = (
                col_vals.str.match(r"^NIFTY(?!.*BANK)") &
                (col_vals.str.len() <= 10)
            ).sum()
            if short_nifty > best_count:
                best_count = short_nifty
                best_col   = col

        # Fallback: if short-value search failed, try col named underlying_symbol
        if best_col is None or best_count == 0:
            if "underlying_symbol" in raw.columns:
                best_col   = "underlying_symbol"
                col_vals   = raw[best_col].astype(str).str.strip().str.upper()
                best_count = col_vals.str.match(
                    r"^NIFTY(?!.*BANK)").sum()

        if best_col is None or best_count == 0:
            sample = {str(c): str(raw.iloc[0][c])[:40]
                      for c in list(raw.columns)[:10]}
            logger.error(
                f"[SymbolMaster] Could not find NIFTY underlying column. "
                f"First row (cols 0-9): {sample}")
            raise ValueError(
                "Symbol master: no NIFTY underlying column found.")

        logger.info(
            f"[SymbolMaster] Underlying column = '{best_col}' "
            f"({best_count} NIFTY rows)")

        # ── Step 5: Filter to NIFTY (not NIFTYBANK / BANKNIFTY) ───────────────
        # Use the detected column directly by position to avoid duplicate-
        # column issues if "underlying_symbol" already exists under another name
        u    = raw[best_col].astype(str).str.strip().str.upper()
        mask = u.str.match(r"^NIFTY(?!.*BANK)")
        df   = raw[mask].copy()

        # Ensure the filter column is named "underlying_symbol" in df
        if best_col != "underlying_symbol":
            # Drop any existing underlying_symbol to avoid duplicates
            if "underlying_symbol" in df.columns:
                df = df.drop(columns=["underlying_symbol"])
            df = df.rename(columns={best_col: "underlying_symbol"})

        # ── Step 6: Locate and parse expiry_date ──────────────────────────────
        # Find which column has date-like values if "expiry_date" is misaligned
        if "expiry_date" not in df.columns or                 pd.to_datetime(df["expiry_date"], errors="coerce").isna().all():
            for col in df.columns:
                parsed = pd.to_datetime(
                    df[col], errors="coerce", dayfirst=True)
                valid  = parsed.notna().sum()
                if valid > len(df) * 0.8:
                    df = df.rename(columns={col: "expiry_date"})
                    logger.info(
                        f"[SymbolMaster] expiry_date found in col '{col}'")
                    break

        df["expiry_date"] = pd.to_datetime(
            df["expiry_date"],
            errors="coerce",
            dayfirst=True,
        ).dt.date

        # ── Step 7: Parse option_type ──────────────────────────────────────────
        if "option_type" in df.columns:
            df["option_type"] = (df["option_type"].fillna("")
                                   .str.strip().str.upper())
        else:
            # Find the column that only contains CE/PE
            for col in df.columns:
                vals = df[col].astype(str).str.strip().str.upper().unique()
                if set(vals).issubset({"CE", "PE", "NAN", ""}):
                    df = df.rename(columns={col: "option_type"})
                    df["option_type"] = df["option_type"].str.strip().str.upper()
                    logger.info(
                        f"[SymbolMaster] option_type found in col '{col}'")
                    break

        df = df[df["option_type"].isin(["CE", "PE"])].copy()

        # ── Step 8: Parse strike_price ────────────────────────────────────────
        if "strike_price" in df.columns:
            df["strike_price"] = pd.to_numeric(
                df["strike_price"], errors="coerce")
        else:
            # Find a numeric column with reasonable strike values (15000-30000)
            for col in df.columns:
                num = pd.to_numeric(df[col], errors="coerce")
                if num.between(5000, 50000).sum() > len(df) * 0.5:
                    df = df.rename(columns={col: "strike_price"})
                    df["strike_price"] = pd.to_numeric(
                        df["strike_price"], errors="coerce")
                    logger.info(
                        f"[SymbolMaster] strike_price found in col '{col}'")
                    break

        # ── Step 9: Locate symbol_ticker (Fyers symbol string) ────────────────
        # Find the column whose values look like NSE:NIFTYYYMMDD...CE/PE
        if "symbol_ticker" not in df.columns:
            for col in df.columns:
                sample_vals = df[col].dropna().astype(str).head(20)
                if sample_vals.str.contains("NIFTY.*(?:CE|PE)$",
                                             regex=True).sum() >= 10:
                    df = df.rename(columns={col: "symbol_ticker"})
                    logger.info(
                        f"[SymbolMaster] symbol_ticker found in col '{col}'")
                    break

        df.dropna(subset=["expiry_date", "strike_price"], inplace=True)

        if len(df) == 0:
            raise ValueError(
                "Symbol master: 0 valid NIFTY CE/PE rows after parsing. "
                "Check expiry_date and strike_price columns.")

        self._sym_df = df
        logger.info(
            f"Symbol master loaded: {len(df)} NIFTY F&O instruments.")
        self._log_available_expiries()

    def _log_available_expiries(self):
        """Log all upcoming expiry dates with days remaining — for verification."""
        today     = date.today()
        expiries  = sorted(self._sym_df["expiry_date"].dropna().unique())
        upcoming  = [e for e in expiries if e >= today][:8]
        lines     = []
        for e in upcoming:
            days = (e - today).days
            flag = (f" ← MIN {self.min_days_to_expiry}d filter"
                    if days < self.min_days_to_expiry else "")
            lines.append(f"    {e}  ({days:>2} days){flag}")
        logger.info(
            f"[Expiry] Upcoming Nifty expiries "
            f"(min {self.min_days_to_expiry} days to expiry required):\n"
            + "\n".join(lines))

    def _atm(self, spot: float) -> int:
        return int(round(spot / self.strike_step) * self.strike_step)

    def _select_expiry(self, as_of: date | None = None) -> date:
        """
        Select the nearest available expiry date that has at least
        min_days_to_expiry calendar days remaining from as_of (default today).

        This reads actual dates from the Fyers symbol master — no arithmetic
        assumptions about "next Thursday". Works correctly around holidays.

        Raises ValueError if no valid expiry found (should not happen in
        normal market hours — symbol master always has several weeks loaded).
        """
        today    = as_of or date.today()
        min_date = today + timedelta(days=self.min_days_to_expiry)

        expiries = sorted(self._sym_df["expiry_date"].dropna().unique())
        valid    = [e for e in expiries if e >= min_date]

        if not valid:
            raise ValueError(
                f"No expiry found with >= {self.min_days_to_expiry} days "
                f"remaining from {today}. Reload symbol master.")

        selected = valid[0]
        days_away = (selected - today).days
        logger.info(
            f"[Expiry] Selected: {selected}  "
            f"({days_away} days from today)  "
            f"[min {self.min_days_to_expiry} days required]")
        return selected

    # ── Public ─────────────────────────────────────────────────────────────────

    def get_option_instrument(self, direction: str, spot: float,
                               as_of: date | None = None) -> dict:
        """
        Select the option instrument for a given signal direction and spot.

        direction = 'buy'  → BUY CE at ATM + strike_offset
        direction = 'sell' → BUY PE at ATM - strike_offset
        as_of     = date to evaluate expiry from (default today)

        Returns dict: fyers_symbol, strike, option_type, expiry, days_to_expiry
        """
        self._load()

        today  = as_of or date.today()
        atm    = self._atm(spot)
        strike = (atm + self.strike_offset) if direction == "buy" \
                 else (atm - self.strike_offset)
        otype  = "CE" if direction == "buy" else "PE"
        expiry = self._select_expiry(as_of=today)

        mask = (
            (self._sym_df["strike_price"] == float(strike)) &
            (self._sym_df["option_type"]  == otype) &
            (self._sym_df["expiry_date"]  == expiry)
        )
        rows = self._sym_df[mask]

        if rows.empty:
            raise ValueError(
                f"No symbol found: NIFTY {strike} {otype} expiry={expiry}. "
                f"Strike may not exist — check strike_step in config.")

        symbol     = str(rows.iloc[0]["symbol_ticker"]).strip()
        days_left  = (expiry - today).days
        result = {
            "fyers_symbol":    symbol,
            "strike":          int(strike),
            "option_type":     otype,
            "expiry":          expiry,
            "days_to_expiry":  days_left,
            "atm":             atm,
        }
        logger.info(
            f"[Option] {symbol} | "
            f"ATM={atm}  Strike={strike}  {otype} | "
            f"Expiry={expiry}  ({days_left} days)")
        return result

    def get_option_ltp(self, fyers_symbol: str) -> float:
        resp = self.fyers.quotes(data={"symbols": fyers_symbol})
        if resp.get("s") != "ok":
            raise ValueError(
                f"LTP error for {fyers_symbol}: {resp.get('message')}")
        return float(resp["d"][0]["v"]["lp"])

    def reload_symbol_master(self):
        """Re-download symbol master — call at bot startup each day."""
        self._sym_df = None
        self._load()

    def available_expiries(self, min_days: int | None = None) -> list[date]:
        """
        Return all upcoming expiry dates with at least min_days remaining.
        Useful for logging or manual inspection.
        """
        self._load()
        today    = date.today()
        md       = min_days if min_days is not None else self.min_days_to_expiry
        expiries = sorted(self._sym_df["expiry_date"].dropna().unique())
        return [e for e in expiries
                if e >= today + timedelta(days=md)]
