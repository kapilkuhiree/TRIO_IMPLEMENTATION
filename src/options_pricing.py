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
