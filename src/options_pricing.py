"""
TRIO — Options Pricing (Black-Scholes + Greeks + IV)
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

European options (NIFTY/BANKNIFTY are European cash-settled). Used when the
live chain quote is missing/stale — e.g. NSE blocks the runner IP. Pure
math, no network, no API keys.

Conventions: S spot, K strike, T years to expiry, r risk-free (config),
sigma annualized vol. Prices in index points; multiply by lot for rupees.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import math
from typing import Dict, Optional

from src.utils import get_logger, load_config

logger = get_logger("options_pricing")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _rf_rate() -> float:
    try:
        return float(load_config().get("options", {}).get("risk_free_rate", 0.065))
    except Exception:
        return 0.065


def bs_price(spot: float, strike: float, t_years: float, sigma: float,
             kind: str = "CE", r: Optional[float] = None) -> float:
    """European call/put price. Returns intrinsic floor when degenerate."""
    kind = (kind or "CE").upper()
    r = _rf_rate() if r is None else float(r)
    try:
        S, K, T, v = float(spot), float(strike), float(t_years), float(sigma)
    except (TypeError, ValueError):
        return 0.0
    if S <= 0 or K <= 0 or v <= 0 or T <= 0:
        # Degenerate: intrinsic value
        return max(S - K, 0.0) if kind == "CE" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * v * v) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    disc = math.exp(-r * T)
    if kind == "CE":
        return max(S * _norm_cdf(d1) - K * disc * _norm_cdf(d2), 0.0)
    return max(K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1), 0.0)


def greeks(spot: float, strike: float, t_years: float, sigma: float,
           kind: str = "CE", r: Optional[float] = None) -> Dict[str, float]:
    """delta/gamma/theta(per day)/vega(per 1% vol). Zeroed when degenerate."""
    kind = (kind or "CE").upper()
    r = _rf_rate() if r is None else float(r)
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    try:
        S, K, T, v = float(spot), float(strike), float(t_years), float(sigma)
    except (TypeError, ValueError):
        return out
    if S <= 0 or K <= 0 or v <= 0 or T <= 0:
        out["delta"] = 1.0 if (kind == "CE" and S > K) else (
            -1.0 if (kind == "PE" and S < K) else 0.0)
        return out
    d1 = (math.log(S / K) + (r + 0.5 * v * v) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    disc = math.exp(-r * T)
    out["delta"] = _norm_cdf(d1) if kind == "CE" else _norm_cdf(d1) - 1.0
    out["gamma"] = _norm_pdf(d1) / (S * v * math.sqrt(T))
    term1 = -(S * _norm_pdf(d1) * v) / (2 * math.sqrt(T))
    if kind == "CE":
        term2 = r * K * disc * _norm_cdf(d2)
    else:
        term2 = -r * K * disc * _norm_cdf(-d2)
    out["theta"] = (term1 - term2) / 365.0
    out["vega"] = S * _norm_pdf(d1) * math.sqrt(T) / 100.0
    return {k: round(v, 4) for k, v in out.items()}


def implied_vol(mkt_price: float, spot: float, strike: float,
                t_years: float, kind: str = "CE",
                r: Optional[float] = None,
                tol: float = 1e-4, max_iter: int = 60) -> Optional[float]:
    """Bisection IV from a market price. None when not bracketable."""
    kind = (kind or "CE").upper()
    r = _rf_rate() if r is None else float(r)
    try:
        px = float(mkt_price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    lo, hi = 0.01, 5.0
    plo = bs_price(spot, strike, t_years, lo, kind, r)
    phi = bs_price(spot, strike, t_years, hi, kind, r)
    if not (plo <= px <= phi):
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        pmid = bs_price(spot, strike, t_years, mid, kind, r)
        if abs(pmid - px) < tol:
            return round(mid, 4)
        if pmid < px:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 4)


def spread_metrics(long_px: float, short_px: float, width: float,
                   lots: int = 1, lot_size: int = 50) -> Dict[str, float]:
    """Debit-spread economics. Returns net_debit, maxLoss, maxGain, RR."""
    try:
        net = max(float(long_px) - float(short_px), 0.0)
        w = max(float(width), 0.0)
        max_loss = net * lots * lot_size
        max_gain = max(w - net, 0.0) * lots * lot_size
        rr = (max_gain / max_loss) if max_loss > 0 else 0.0
        return {"net_debit": round(net, 2), "maxLoss": round(max_loss, 2),
                "maxGain": round(max_gain, 2), "RR": round(rr, 2)}
    except (TypeError, ValueError):
        return {"net_debit": 0.0, "maxLoss": 0.0, "maxGain": 0.0, "RR": 0.0}


def breakeven_call_spread(long_strike: float, net_debit: float) -> float:
    """Long-strike + debit (bull call)."""
    return float(long_strike) + float(net_debit)


def breakeven_put_spread(long_strike: float, net_debit: float) -> float:
    """Long-strike - debit (bear put)."""
    return float(long_strike) - float(net_debit)


def fill_premium(row: Dict[str, float], side: str = "BUY",
                 slippage_ticks: float = 0.5) -> Dict[str, float]:
    """Executable fill premium per spec: BUY at ask + slippage, SELL at bid.

    When fill_model is "mid" (legacy), callers should use mid=(bid+ask)/2
    directly. When "bidask", use this. Falls back to LTP then BS when the
    chain quote is missing — never raises, never returns 0 for a valid row
    without a fallback the caller can detect.

    Returns dict with premium, src (bidask|ltp|sim), bid, ask, oi, volume.
    """
    tick = 0.05  # NIFTY index option tick
    slip = float(slippage_ticks or 0.0) * tick
    # row here is expected to be {"bid":..,"ask":..,"ltp":..,"premium":..}
    bid = float(row.get("bid") or row.get("bidPrice") or 0.0)
    ask = float(row.get("ask") or row.get("askPrice") or 0.0)
    ltp = float(row.get("ltp") or row.get("lastPrice") or 0.0)
    oi = int(row.get("oi") or row.get("openInterest") or 0)
    vol = int(row.get("volume") or row.get("vol") or 0)
    if side == "BUY" and ask > 0:
        px, src = round(ask + slip, 2), "bidask"
    elif side == "SELL" and bid > 0:
        # SELL collects bid: more bid is better for the seller, slippage
        # is against the seller (lower fill).
        px, src = round(max(bid - slip, 0.05), 2), "bidask"
    elif ltp > 0:
        px, src = round(ltp, 2), "ltp"
    elif float(row.get("premium") or 0.0) > 0:
        px, src = round(float(row["premium"]), 2), "sim"
    else:
        px, src = 0.0, "none"
    return {"premium": px, "src": src, "bid": bid, "ask": ask,
            "oi": oi, "volume": vol}


def fill_leg_premium(leg_quote: Dict[str, float], side: str = "BUY",
                     slippage_ticks: Optional[float] = None) -> float:
    """Apply spec §6 slippage to an already-quoted leg (mid/ltp/sim).

    Leg_quote is the _leg_quote output {premium, bid, ask, src, ...}.
    When fill_model is bidask, replace mid with executable side price.
    """
    try:
        ticks = float(slippage_ticks) if slippage_ticks is not None else \
            float(load_config().get("options", {}).get(
                "slippage_ticks", 0.5))
    except Exception:
        ticks = 0.5
    slip = ticks * 0.05
    bid = float(leg_quote.get("bid") or 0.0)
    ask = float(leg_quote.get("ask") or 0.0)
    if side == "BUY" and ask > 0:
        return round(ask + slip, 2)
    if side == "SELL" and bid > 0:
        return round(max(bid - slip, 0.05), 2)
    return float(leg_quote.get("premium") or 0.0)


def round_trip_cost(strategy: str, entry_debit: float, exit_value: float,
                    lots: int = 1, lot_size: Optional[int] = None,
                    n_orders: Optional[int] = None) -> float:
    """Round-trip rupee cost for one options trade (spec section 3).

    Deliberately mirrors scripts/options_backtest._costs_for so the live
    paper ledger and the backtest agree on P&L: brokerage x orders + STT on
    the sold premium only.
      - naked long (1 leg):           2 orders, STT on the exit sale.
      - debit/credit spread (2 legs): 4 orders, STT on the entry short leg
        and the exit long leg (approximated as half the net premium each).
    Config keys: options.brokerage_per_order (default 20),
    options.stt_rate_sold (default 0.0015), options.lot_size (default 50).
    """
    try:
        ocfg = load_config().get("options", {}) or {}
    except Exception:
        ocfg = {}
    try:
        brok_per = float(ocfg.get("brokerage_per_order", 20.0) or 0.0)
    except (TypeError, ValueError):
        brok_per = 20.0
    try:
        stt_rate = float(ocfg.get("stt_rate_sold", 0.0015) or 0.0)
    except (TypeError, ValueError):
        stt_rate = 0.0015
    if lot_size is None:
        try:
            lot_size = int(ocfg.get("lot_size", 50))
        except (TypeError, ValueError):
            lot_size = 50
    is_long = str(strategy) in ("long-call", "long-put")
    orders = int(n_orders) if n_orders is not None else (2 if is_long else 4)
    brok = brok_per * orders
    notional = int(lots) * int(lot_size)
    if is_long:
        stt = float(exit_value) * notional * stt_rate
    else:
        stt = float(entry_debit) * 0.5 * notional * stt_rate
        stt += float(exit_value) * 0.5 * notional * stt_rate
    return round(brok + stt, 2)
