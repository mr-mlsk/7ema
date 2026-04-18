"""
forward_test.py — 7 EMA Nifty Options Forward Tester (Paper Trading)
=====================================================================
Runs the live bot loop using real Fyers market data.
NO REAL ORDERS ARE PLACED.

STRATEGY (exactly as specified):
  Signal   : 7 EMA retest on 15M with 1H directional bias
  Sizing   : Fixed 1 lot per trade
  SL       : Candle low (BUY CE) / candle high (BUY PE)
  Target   : Entry ± SL distance × current_rr
  Step-down: 1:3 → 1:2 after 6 candles → 1:1 after 12 candles
  Filters  : SL 10–70 pts | Daily loss limit 8%
  Excluded : No VIX | No re-entry cooldown | No clustering
  Fills    : Live Fyers LTP at signal time ± 0.5% slippage
  Charges  : All Indian intraday option charges deducted

Journal: fwdtest_journal_YYYY-MM.csv  (one row per closed trade)
Log:     fwdtest_YYYYMMDD.log

Usage:
  python forward_test.py
Stop with Ctrl+C — open trade closes at current live LTP.
"""

import csv
import sys
import time
import logging
import argparse
from datetime import datetime, date, time as dtime
from pathlib import Path

from fyers_apiv3 import fyersModel

sys.path.insert(0, ".")
from config          import FYERS_CONFIG, TRADING_CONFIG
from signal_engine   import SignalEngine
from option_selector import OptionSelector
from exit_manager    import ExitManager, TradeState
from charges         import compute_charges

# ── Constants ──────────────────────────────────────────────────────────────────
LOT_SIZE       = TRADING_CONFIG["lot_size"]
LOTS           = TRADING_CONFIG["lots"]               # fixed 1 lot
FORCE_EXIT_T   = TRADING_CONFIG["force_exit_at"]      # 15:15
NO_ENTRY_AFTER = TRADING_CONFIG["no_new_entry_after"] # 14:30
MAX_TRADES_DAY = TRADING_CONFIG["max_trades_per_day"] # 3
DAILY_LOSS_LIM = TRADING_CONFIG["daily_loss_limit_pct"]  # 0.08
MIN_SL         = TRADING_CONFIG["min_sl_pts"]         # 10.0
MAX_SL         = TRADING_CONFIG["max_sl_pts"]         # 70.0
INITIAL_CAPITAL= float(TRADING_CONFIG["capital"])     # 50000
SLIPPAGE       = 0.005  # 0.5% market-order fill slippage
MARKET_OPEN    = dtime(9, 15)
MARKET_CLOSE   = dtime(15, 30)

# ── Journal columns ────────────────────────────────────────────────────────────
JOURNAL_COLS = [
    "date", "entry_time", "exit_time",
    "trade_type", "fyers_symbol", "strike", "expiry", "days_to_expiry",
    "1h_bias", "1h_ema7", "15m_ema7",
    "lots", "units",
    "entry_underlying", "sl_px", "sl_distance",
    "target_px", "initial_rr", "final_rr",
    "exit_underlying", "exit_reason", "candles_held",
    "entry_ltp", "entry_fill",
    "exit_ltp",  "exit_fill",
    "gross_pnl",
    "brokerage", "stt", "nse_txn", "sebi_charges",
    "stamp_duty", "ipft", "gst", "total_charges",
    "net_pnl", "net_pnl_pct",
    "running_capital", "result",
]


# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging() -> str:
    log_file = f"fwdtest_{date.today().strftime('%Y%m%d')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    sh = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w",
             encoding="utf-8", errors="replace", closefd=False))
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    return log_file


# ── Journal helpers ────────────────────────────────────────────────────────────

def journal_path() -> str:
    return f"fwdtest_journal_{date.today().strftime('%Y-%m')}.csv"


def ensure_journal(path: str):
    if not Path(path).exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=JOURNAL_COLS).writeheader()
        logging.getLogger(__name__).info(f"Journal created: {path}")


def append_journal(path: str, row: dict):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_COLS,
                       extrasaction="ignore").writerow(row)


def journal_state(path: str, default: float) -> tuple[int, float, float]:
    """Returns (total_trades, cumulative_net_pnl, last_running_capital)."""
    if not Path(path).exists():
        return 0, 0.0, default
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 0, 0.0, default
    net = sum(float(r.get("net_pnl", 0)) for r in rows)
    cap = float(rows[-1].get("running_capital", default))
    return len(rows), round(net, 2), round(cap, 2)


# ── Auth ───────────────────────────────────────────────────────────────────────

def authenticate() -> fyersModel.FyersModel:
    log = logging.getLogger(__name__)
    fyers = fyersModel.FyersModel(
        client_id = FYERS_CONFIG["client_id"],
        is_async  = False,
        token     = FYERS_CONFIG["access_token"],
        log_path  = "",
    )
    p = fyers.get_profile()
    if p.get("s") != "ok":
        log.error(f"Auth failed: {p.get('message')}")
        log.error("Run python auth_token.py and update config.py")
        sys.exit(1)
    log.info(f"Authenticated as: {p['data']['name']}")
    return fyers


# ── Live fills ─────────────────────────────────────────────────────────────────

def get_ltp(fyers: fyersModel.FyersModel, symbol: str) -> float:
    """Fetch live last-traded price."""
    resp = fyers.quotes(data={"symbols": symbol})
    if resp.get("s") != "ok":
        raise ValueError(
            f"LTP fetch failed {symbol}: {resp.get('message')}")
    return float(resp["d"][0]["v"]["lp"])


def buy_fill(ltp: float)  -> float:
    """Simulate market buy: LTP + 0.5% slippage."""
    return round(ltp * (1 + SLIPPAGE), 2)


def sell_fill(ltp: float) -> float:
    """Simulate market sell: LTP - 0.5% slippage."""
    return round(max(0.05, ltp * (1 - SLIPPAGE)), 2)


# ── Trade close ────────────────────────────────────────────────────────────────

def close_trade(
    log,
    trade:         TradeState,
    instrument:    dict,
    entry_bias:    str,
    entry_1h_ema:  float,
    entry_15m_ema: float,
    entry_ltp:     float,
    entry_fill:    float,
    spot:          float,
    reason:        str,
    now:           datetime,
    fyers:         fyersModel.FyersModel,
    journal:       str,
    running_cap:   float,
) -> float:
    """Fetch exit LTP, compute charges, write journal row. Returns net P&L."""
    try:
        exit_ltp = get_ltp(fyers, trade.tradingsymbol)
    except Exception as e:
        log.warning(f"Exit LTP failed ({e}) — using entry fill as fallback")
        exit_ltp = entry_fill

    ex_fill = sell_fill(exit_ltp)

    if trade.direction == "buy":
        und_pts = spot - trade.entry_price_underlying
    else:
        und_pts = trade.entry_price_underlying - spot

    c        = compute_charges(entry_fill, ex_fill, LOT_SIZE, lots=LOTS)
    net_pnl  = c.net_pnl
    new_cap  = round(running_cap + net_pnl, 2)
    result   = "WIN" if net_pnl > 0 else "LOSS"
    ttype    = f"BUY {'CE' if trade.direction == 'buy' else 'PE'}"

    outcome = "PROFIT" if net_pnl > 0 else "LOSS"
    log.info(
        f"\n{'='*62}\n"
        f"  TRADE CLOSED — {outcome} | {reason}\n"
        f"  {ttype} | {trade.tradingsymbol}\n"
        f"  Lots         : {LOTS} ({LOTS * LOT_SIZE} units)\n"
        f"  Final RR     : 1:{trade.current_rr_target}\n"
        f"  Candles held : {trade.candles_since_entry}\n"
        f"  Underlying   : entry={trade.entry_price_underlying:.2f}  "
        f"exit≈{spot:.2f}  ({und_pts:+.1f}pts)\n"
        f"  Entry LTP    : Rs {entry_ltp:.2f}  fill Rs {entry_fill:.2f}\n"
        f"  Exit  LTP    : Rs {exit_ltp:.2f}  fill Rs {ex_fill:.2f}\n"
        f"  Gross P&L    : Rs {c.gross_pnl:+,.2f}\n"
        f"  Charges      : Rs {c.total_charges:.2f}  "
        f"(brok {c.brokerage} + STT {c.stt} + "
        f"NSE {c.nse_txn} + GST {c.gst:.2f})\n"
        f"  Net P&L      : Rs {net_pnl:+,.2f}\n"
        f"  Capital      : Rs {running_cap:,.0f} → Rs {new_cap:,.0f}\n"
        f"{'='*62}")

    append_journal(journal, {
        "date":              trade.entry_time.strftime("%Y-%m-%d"),
        "entry_time":        trade.entry_time.strftime("%Y-%m-%d %H:%M"),
        "exit_time":         now.strftime("%Y-%m-%d %H:%M"),
        "trade_type":        ttype,
        "fyers_symbol":      trade.tradingsymbol,
        "strike":            f"{instrument.get('strike','')} "
                             f"{instrument.get('option_type','')}",
        "expiry":            str(instrument.get("expiry", "")),
        "days_to_expiry":    instrument.get("days_to_expiry", ""),
        "1h_bias":           entry_bias,
        "1h_ema7":           round(entry_1h_ema, 2),
        "15m_ema7":          round(entry_15m_ema, 2),
        "lots":              LOTS,
        "units":             LOTS * LOT_SIZE,
        "entry_underlying":  round(trade.entry_price_underlying, 2),
        "sl_px":             round(trade.sl_underlying, 2),
        "sl_distance":       round(trade.sl_distance, 2),
        "target_px":         round(trade.target_price_underlying, 2),
        "initial_rr":        trade.initial_rr,
        "final_rr":          trade.current_rr_target,
        "exit_underlying":   round(spot, 2),
        "exit_reason":       reason,
        "candles_held":      trade.candles_since_entry,
        "entry_ltp":         entry_ltp,
        "entry_fill":        entry_fill,
        "exit_ltp":          exit_ltp,
        "exit_fill":         ex_fill,
        "gross_pnl":         c.gross_pnl,
        "brokerage":         c.brokerage,
        "stt":               c.stt,
        "nse_txn":           c.nse_txn,
        "sebi_charges":      c.sebi_charges,
        "stamp_duty":        c.stamp_duty,
        "ipft":              c.ipft,
        "gst":               c.gst,
        "total_charges":     c.total_charges,
        "net_pnl":           net_pnl,
        "net_pnl_pct":       c.net_pnl_pct,
        "running_capital":   new_cap,
        "result":            result,
    })
    return net_pnl


# ── Session summary ────────────────────────────────────────────────────────────

def session_summary(log, trades_today: int,
                     running_capital: float, j_path: str):
    n, total_pnl, cap = journal_state(j_path, running_capital)
    wins = 0
    if Path(j_path).exists():
        with open(j_path, "r", encoding="utf-8") as f:
            wins = sum(1 for r in csv.DictReader(f)
                       if r.get("result") == "WIN")
    wr = f"{wins/n*100:.1f}%" if n else "—"
    log.info(
        f"\n{'='*62}\n"
        f"  SESSION SUMMARY\n"
        f"  Today    : {trades_today} trades\n"
        f"  Lifetime : {n} trades | Win rate {wr} | "
        f"Net Rs {total_pnl:+,.0f}\n"
        f"  Capital  : Rs {cap:,.0f}\n"
        f"{'='*62}")


# ── Main loop ──────────────────────────────────────────────────────────────────

def run(initial_capital: float):
    log     = logging.getLogger(__name__)
    j_path  = journal_path()
    ensure_journal(j_path)
    n_hist, hist_pnl, running_capital = journal_state(j_path, initial_capital)

    log.info("=" * 62)
    log.info("  7 EMA NIFTY OPTIONS — FORWARD TEST  [PAPER TRADING]")
    log.info(f"  Capital      : Rs {running_capital:,.0f}  "
             f"({n_hist} prev trades, net Rs {hist_pnl:+,.0f})")
    log.info(f"  Sizing       : Fixed {LOTS} lot ({LOTS * LOT_SIZE} units)")
    log.info(f"  SL filter    : {MIN_SL}–{MAX_SL} pts")
    log.info(f"  Daily limit  : -{DAILY_LOSS_LIM*100:.0f}% of capital")
    log.info(f"  Exit mode    : Step-down 1:3 → 1:2 (6c) → 1:1 (12c)")
    log.info(f"  EOD exit     : {FORCE_EXIT_T} | No entry after {NO_ENTRY_AFTER}")
    log.info(f"  Expiry       : ≥ {TRADING_CONFIG['min_days_to_expiry']} days")
    log.info(f"  Journal      : {j_path}")
    log.info("=" * 62)

    fyers   = authenticate()
    sig_eng = SignalEngine(fyers, TRADING_CONFIG)
    opt_sel = OptionSelector(fyers, TRADING_CONFIG)
    ext_mgr = ExitManager(TRADING_CONFIG)

    opt_sel.reload_symbol_master()

    # ── State ──────────────────────────────────────────────────────────────────
    active:           TradeState | None = None
    active_inst:      dict              = {}
    active_bias:      str               = ""
    active_1h_ema:    float             = 0.0
    active_15m_ema:   float             = 0.0
    active_entry_ltp: float             = 0.0
    active_entry_fill:float             = 0.0

    trades_today      = 0
    day_start_capital = running_capital
    last_sig_candle   = None
    last_candle_time  = None
    today             = date.today()
    spot              = 0.0

    while True:
        try:
            now = datetime.now()
            tod = now.time()

            # Day rollover
            if now.date() != today:
                today             = now.date()
                trades_today      = 0
                day_start_capital = running_capital
                last_sig_candle   = None
                log.info(f"[Day] {today} | Capital: Rs {running_capital:,.0f}")

            # Outside market hours
            if not (MARKET_OPEN <= tod <= MARKET_CLOSE):
                if tod > MARKET_CLOSE:
                    log.info("Market closed for today.")
                    session_summary(log, trades_today,
                                    running_capital, j_path)
                    break
                time.sleep(30)
                continue

            # Fetch live Nifty spot
            try:
                spot = sig_eng.get_current_nifty_price()
            except Exception as e:
                log.warning(f"Spot fetch failed: {e}")
                time.sleep(10)
                continue

            # ── Monitor active trade ───────────────────────────────────────────
            if active:

                # Force exit at 3:15 PM
                if tod >= FORCE_EXIT_T:
                    log.info("3:15 PM — EOD force exit.")
                    net = close_trade(
                        log, active, active_inst,
                        active_bias, active_1h_ema, active_15m_ema,
                        active_entry_ltp, active_entry_fill,
                        spot,
                        "FORCE_EXIT_EOD", now, fyers,
                        j_path, running_capital)
                    running_capital += net
                    active = None
                    trades_today   += 1
                    time.sleep(60)
                    continue

                # New 15M candle → step-down check
                try:
                    latest = sig_eng.get_latest_15m_candle_time()
                    if latest != last_candle_time:
                        last_candle_time = latest
                        active = ext_mgr.on_new_candle(active)
                        log.info(active.status_line(spot))
                except Exception as e:
                    log.warning(f"Candle update error: {e}")

                # SL or target
                reason = ext_mgr.check_exit(active, spot)
                if reason:
                    net = close_trade(
                        log, active, active_inst,
                        active_bias, active_1h_ema, active_15m_ema,
                        active_entry_ltp, active_entry_fill,
                        spot,
                        reason, now, fyers,
                        j_path, running_capital)
                    running_capital += net
                    active = None
                    trades_today   += 1
                    log.info(
                        f"Trades today: {trades_today}/{MAX_TRADES_DAY} | "
                        f"Capital: Rs {running_capital:,.0f}")

            # ── Look for new entry ─────────────────────────────────────────────
            else:
                if tod >= NO_ENTRY_AFTER:
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue
                if trades_today >= MAX_TRADES_DAY:
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                # Daily loss limit
                if (running_capital - day_start_capital
                        < -(day_start_capital * DAILY_LOSS_LIM)):
                    log.info(
                        f"[DAILY LIMIT] Down Rs "
                        f"{abs(running_capital - day_start_capital):,.0f} "
                        f"({abs(running_capital - day_start_capital)/day_start_capital*100:.1f}%) "
                        f"today — no more entries.")
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                # Hourly bias
                bias = sig_eng.get_hourly_bias()
                if bias == "neutral":
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                # 15M signal
                signal = sig_eng.get_entry_signal(bias)
                if not signal or signal["candle_time"] == last_sig_candle:
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                # Mark this candle immediately — prevents re-firing
                # regardless of which filter rejects it or which step fails
                last_sig_candle = signal["candle_time"]

                sl_dist   = signal["sl_distance"]
                direction = signal["direction"]

                # SL distance filter
                if not (MIN_SL <= sl_dist <= MAX_SL):
                    log.info(
                        f"[SKIP] SL {sl_dist:.1f}pts out of range "
                        f"({MIN_SL}–{MAX_SL}pts)")
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                # Option instrument
                try:
                    instrument = opt_sel.get_option_instrument(
                        direction, spot)
                except ValueError as e:
                    log.error(f"Instrument lookup failed: {e}")
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                # Live entry LTP
                try:
                    entry_ltp = get_ltp(fyers, instrument["fyers_symbol"])
                except Exception as e:
                    log.error(f"Entry LTP fetch failed: {e}")
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                en_fill    = buy_fill(entry_ltp)
                total_cost = en_fill * LOTS * LOT_SIZE

                if total_cost > running_capital:
                    log.warning(
                        f"[SKIP] Option cost Rs {total_cost:,.0f} > "
                        f"capital Rs {running_capital:,.0f}")
                    time.sleep(TRADING_CONFIG["poll_interval"])
                    continue

                # Open trade
                active = TradeState(
                    direction              = direction,
                    entry_price_underlying = signal["entry_price"],
                    sl_underlying          = signal["sl"],
                    sl_distance            = sl_dist,
                    initial_rr             = TRADING_CONFIG["initial_rr"],
                    tradingsymbol          = instrument["fyers_symbol"],
                    entry_premium          = en_fill,
                    entry_order_id         = f"FT_{now.strftime('%H%M%S')}",
                    lots                   = LOTS,
                    entry_time             = now,
                )

                active_inst        = instrument
                active_bias        = bias
                active_1h_ema      = signal.get("ema7_at_entry", 0.0)
                active_15m_ema     = signal.get("ema7_at_entry", 0.0)
                active_entry_ltp   = entry_ltp
                active_entry_fill  = en_fill
                last_candle_time   = signal["candle_time"]

                ttype = f"BUY {'CE' if direction == 'buy' else 'PE'}"
                log.info(
                    f"\n{'─'*62}\n"
                    f"  TRADE ENTERED — {ttype}\n"
                    f"  Symbol     : {instrument['fyers_symbol']}\n"
                    f"  Strike     : {instrument['strike']} "
                    f"{instrument['option_type']}\n"
                    f"  Expiry     : {instrument['expiry']} "
                    f"({instrument['days_to_expiry']} days)\n"
                    f"  Lots       : {LOTS} ({LOTS * LOT_SIZE} units)\n"
                    f"  Entry LTP  : Rs {entry_ltp:.2f} [LIVE]\n"
                    f"  Entry fill : Rs {en_fill:.2f} (+0.5% slippage)\n"
                    f"  Cost       : Rs {total_cost:,.0f}\n"
                    f"  Underlying : {signal['entry_price']:.2f}  "
                    f"SL={signal['sl']:.2f}  ({sl_dist:.1f}pts)\n"
                    f"  Target 1:3 : {active.target_price_underlying:.2f}\n"
                    f"  Step-down  : 1:2 after 6c | 1:1 after 12c\n"
                    f"  1H bias    : {bias.upper()}\n"
                    f"  Capital    : Rs {running_capital:,.0f}\n"
                    f"{'─'*62}")

            time.sleep(TRADING_CONFIG["poll_interval"])

        except KeyboardInterrupt:
            log.info("\nStopped by user (Ctrl+C).")
            if active:
                log.info("Closing paper trade at current LTP...")
                try:
                    net = close_trade(
                        log, active, active_inst,
                        active_bias, active_1h_ema, active_15m_ema,
                        active_entry_ltp, active_entry_fill,
                        spot,
                        "MANUAL_STOP", datetime.now(), fyers,
                        j_path, running_capital)
                    running_capital += net
                except Exception as e:
                    log.error(f"Could not close trade: {e}")
            session_summary(log, trades_today, running_capital, j_path)
            break

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            log.info("Sleeping 30s before retry...")
            time.sleep(30)


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="7 EMA Nifty Options Forward Tester — paper trading")
    p.add_argument("--capital", type=float, default=INITIAL_CAPITAL,
                   help=f"Starting capital (default {INITIAL_CAPITAL:,.0f})")
    return p.parse_args()


if __name__ == "__main__":
    log_file = setup_logging()
    logging.getLogger(__name__).info(f"Log: {log_file}")
    args = parse_args()
    run(args.capital)
