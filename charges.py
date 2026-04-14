"""
charges.py — Nifty Options Intraday Charge Calculator (FY 2025-26)
===================================================================
Per round-trip (BUY + SELL), all % on PREMIUM TURNOVER not notional.

  Brokerage   Rs 20 flat x 2 orders
  STT         0.0625% on SELL side premium turnover
  NSE txn     0.053%  on total premium turnover
  SEBI        0.0001% on total premium turnover
  Stamp duty  0.003%  on BUY  side premium turnover
  IPFT        0.0001% on total premium turnover
  GST         18% on (brokerage + NSE txn + SEBI)
"""

from dataclasses import dataclass


@dataclass
class ChargeBreakdown:
    entry_premium:  float
    exit_premium:   float
    lot_size:       int
    lots:           int
    buy_turnover:   float
    sell_turnover:  float
    gross_pnl:      float
    brokerage:      float
    stt:            float
    nse_txn:        float
    sebi_charges:   float
    stamp_duty:     float
    ipft:           float
    gst:            float
    total_charges:  float
    net_pnl:        float
    net_pnl_pct:    float

    def summary(self) -> str:
        return "\n".join([
            f"  {'Entry prem':<22} Rs {self.entry_premium:.2f} x "
            f"{self.lot_size}u x {self.lots}lot = "
            f"Rs {self.buy_turnover:,.2f}",
            f"  {'Exit prem':<22} Rs {self.exit_premium:.2f} x "
            f"{self.lot_size}u x {self.lots}lot = "
            f"Rs {self.sell_turnover:,.2f}",
            f"  {'Gross P&L':<22} Rs {self.gross_pnl:+,.2f}",
            f"  {'-'*52}",
            f"  {'Brokerage (x2)':<22} Rs {self.brokerage:.2f}",
            f"  {'STT (sell side)':<22} Rs {self.stt:.2f}",
            f"  {'NSE txn charge':<22} Rs {self.nse_txn:.2f}",
            f"  {'SEBI charges':<22} Rs {self.sebi_charges:.2f}",
            f"  {'Stamp duty (buy)':<22} Rs {self.stamp_duty:.2f}",
            f"  {'IPFT':<22} Rs {self.ipft:.2f}",
            f"  {'GST @ 18%':<22} Rs {self.gst:.2f}",
            f"  {'-'*52}",
            f"  {'Total charges':<22} Rs {self.total_charges:.2f}",
            f"  {'Net P&L':<22} Rs {self.net_pnl:+,.2f} "
            f"({self.net_pnl_pct:+.2f}% on capital deployed)",
        ])


def compute_charges(
    entry_premium:       float,
    exit_premium:        float,
    lot_size:            int   = 75,
    lots:                int   = 1,
    brokerage_per_order: float = 20.0,
) -> ChargeBreakdown:
    units    = lot_size * lots
    buy_to   = round(entry_premium * units, 2)
    sell_to  = round(exit_premium  * units, 2)
    total_to = buy_to + sell_to
    gross    = round((exit_premium - entry_premium) * units, 2)

    brok  = round(brokerage_per_order * 2, 2)
    stt   = round(sell_to  * 0.000625, 2)
    nse   = round(total_to * 0.00053,  2)
    sebi  = round(total_to * 0.000001, 2)
    stamp = round(buy_to   * 0.00003,  2)
    ipft  = round(total_to * 0.000001, 2)
    gst   = round((brok + nse + sebi) * 0.18, 2)
    total = round(brok + stt + nse + sebi + stamp + ipft + gst, 2)
    net   = round(gross - total, 2)
    pct   = round(net / buy_to * 100, 3) if buy_to else 0.0

    return ChargeBreakdown(
        entry_premium=entry_premium, exit_premium=exit_premium,
        lot_size=lot_size, lots=lots,
        buy_turnover=buy_to, sell_turnover=sell_to, gross_pnl=gross,
        brokerage=brok, stt=stt, nse_txn=nse, sebi_charges=sebi,
        stamp_duty=stamp, ipft=ipft, gst=gst,
        total_charges=total, net_pnl=net, net_pnl_pct=pct,
    )
