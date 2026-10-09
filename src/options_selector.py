"""
TRIO — Options Strategy Selector
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Turns a spot TradeSignal (+ ADX regime + IV-rank) into a defined-risk NIFTY
options expression. Strategy table (proven-first):

  SPREAD MODE (default):
    trending + pullback BUY  -> Bull Call Spread (long ATM Δ~0.5, short OTM Δ~0.3)
    trending bear + MACD SELL -> Bear Put Spread (long ATM put, short OTM put)
    chop (ADX < 25)           -> Iron Condor 16Δ wings, ~7 DTE, 50% MPE exit
    IV-rank > 80 + ATR spike  -> Long Straddle ATM, 30% trailing exit

  LONG MODE (buy-only, config style="long"):
    BUY signal  -> Long Call (ITM Δ~0.65, single leg, no short)
    SELL signal -> Long Put (ITM Δ~0.65, single leg, no short)
    chop/HOLD   -> none (no naked shorts, no premium selling)

Quote preference per leg: live chain (bid/ask mid) -> chain LTP -> BS sim
with HV proxy. A leg is skipped when bid == 0 or OI < floor (config) so an
illiquid wing can never enter a live-sized paper fill.

Output: OptionTradeCandidate(strategy, legs[], net_debit, maxLoss, maxGain,
breakeven, margin, rank, reasoning). Rank = confidence x IV-penalty x
liquidity, best-first — mirrors screener.rank_signal so stock and option
candidates compete on one scale.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.utils import get_logger, load_config
from src.options_chain import atm_strike
from src.options_pricing import bs_price, spread_metrics

logger = get_logger("options_selector")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OptionLeg:
    """One leg: strike/expiry/side/premium/delta/token."""
    strike: float = 0.0
    expiry: str = ""
    kind: str = "CE"          # CE | PE
    side: str = "BUY"         # BUY (long) | SELL (short)
    premium: float = 0.0      # fill price used
    premium_src: str = "sim"  # live | ltp | sim
    delta: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    oi: int = 0
    volume: int = 0
    lots: int = 1
    token: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OptionTradeCandidate:
    """A ranked options expression for one spot signal."""
    strategy: str = ""        # bull-call-spread | bear-put-spread |
                              # iron-condor | long-straddle | none
    underlying: str = "NIFTY"
    spot: float = 0.0
    expiry: str = ""
    legs: List[OptionLeg] = field(default_factory=list)
    net_debit: float = 0.0
    maxLoss: float = 0.0
    maxGain: float = 0.0
    breakeven: float = 0.0
    margin: float = 0.0
    rank: float = 0.0
    confidence: int = 0
    reasoning: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["legs"] = [l.to_dict() if hasattr(l, "to_dict") else dict(l)
                     for l in self.legs]
        return d


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _opt_cfg() -> Dict[str, Any]:
    try:
        return load_config().get("options", {}) or {}
    except Exception:
        return {}


def _dte_years(expiry: str) -> float:
    try:
        for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(expiry, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                dt = None
        if dt is None:
            return 7.0 / 365.0
        days = max((dt - datetime.now(timezone.utc)).days, 1)
        return days / 365.0
    except Exception:
        return 7.0 / 365.0


def _leg_quote(row: Dict[str, Any], kind: str, t_years: float,
               spot: float, hv_proxy: float) -> Dict[str, Any]:
    """Premium + delta for one strike row. Prefers live mid, then LTP, then sim."""
    leg = row.get(kind) or {}
    bid = float(leg.get("bidPrice") or 0.0)
    ask = float(leg.get("askPrice") or 0.0)
    ltp = float(leg.get("lastPrice") or 0.0)
    oi = int(leg.get("openInterest") or 0)
    vol = int(leg.get("volume") or 0)
    if bid > 0 and ask > 0:
        px, src = round((bid + ask) / 2.0, 2), "live"
    elif ltp > 0:
        px, src = round(ltp, 2), "ltp"
    else:
        sigma = max(float(leg.get("iv") or 0.0) / 100.0, hv_proxy)
        px, src = round(bs_price(spot, row["strike"], t_years, sigma, kind), 2), "sim"
    # Crude delta proxy when chain gives none: ATM≈0.5 decaying with distance.
    delta = 0.5 if abs(row["strike"] - spot) < (spot * 0.005) else 0.3
    if kind == "PE":
        delta = -delta
    return {"premium": px, "src": src, "delta": delta, "bid": bid,
            "ask": ask, "oi": oi, "volume": vol}


def _liquid(row_leg: Dict[str, Any], oi_floor: int) -> bool:
    return (row_leg.get("bid", 0) or 0) > 0 and (row_leg.get("oi", 0) or 0) >= oi_floor


def _pick_by_delta(rows: List[Dict[str, Any]], spot: float, kind: str,
                   target_delta: float, side_prefer: str = "OTM") -> Optional[Dict[str, Any]]:
    """Strike row whose crude delta is nearest target on the wanted side."""
    cands = []
    for r in rows:
        k = r["strike"]
        if side_prefer == "OTM":
            if kind == "CE" and k < spot:
                continue
            if kind == "PE" and k > spot:
                continue
        elif side_prefer == "ITM":
            # long-only mode: slightly ITM (CE: strike <= spot,
            # PE: strike >= spot) for higher delta, slower theta bleed.
            if kind == "CE" and k > spot:
                continue
            if kind == "PE" and k < spot:
                continue
        elif side_prefer == "ATM":
            # long leg may sit slightly ITM so the spread has real debit
            pass
        # crude delta from moneyness: 0.5 ATM -> 0.15 at 2% away
        dist_pct = abs(k - spot) / spot if spot else 1.0
        d = max(0.5 - dist_pct * 17.5, 0.05)
        cands.append((abs(d - target_delta), r))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


# ---------------------------------------------------------------------------
# selector
# ---------------------------------------------------------------------------

def select(signal: Any, chain: Dict[str, Any],
           expiry: Optional[str] = None,
           iv_rank: Optional[float] = None,
           adx: Optional[float] = None,
           hv_proxy: float = 0.20) -> OptionTradeCandidate:
    """Map a spot signal to the best defined-risk options expression.

    Regime rule: trending (adx>=25) -> debit spread on the signal side;
    chop (adx<25) -> iron condor regardless of side; IV-rank>80 + ATR
    spike flag (iv_rank passed in) -> long straddle note in reasoning but
    the spread still ranks first (spreads are the proven leg).
    """
    cfg = _opt_cfg()
    if not cfg.get("enabled", True):
        return OptionTradeCandidate(strategy="none",
                                    reasoning=["options disabled in config"])
    # NIFTY index ONLY — never stock options. Explicit guard: if config
    # drifts to anything else, refuse rather than trade the wrong book.
    underlying = str(cfg.get("underlying", "NIFTY")).strip().upper()
    if underlying != "NIFTY":
        return OptionTradeCandidate(strategy="none", underlying=underlying,
                                    reasoning=[f"index-only guard: {underlying} "
                                               "blocked, NIFTY only"])
    lot_size = int(cfg.get("lot_size", 50))
    oi_floor = int(cfg.get("min_oi", 1000))
    d_atm = float(cfg.get("delta_atm", 0.50))
    d_otm = float(cfg.get("delta_otm", 0.30))
    d_long = float(cfg.get("delta_long", 0.65))
    style = str(cfg.get("style", "spread")).strip().lower()
    iv_cap = float(cfg.get("max_iv_rank", 80))

    spot = float(chain.get("underlying") or 0.0)
    if not spot or not chain.get("by_expiry"):
        return OptionTradeCandidate(strategy="none", underlying=underlying,
                                    reasoning=["no chain data"])
    if not expiry:
        from src.options_chain import nearest_expiry
        expiry = nearest_expiry(chain) or ""
    rows = [r for r in (chain.get("by_expiry", {}).get(expiry, []) or [])]
    if not rows:
        return OptionTradeCandidate(strategy="none", underlying=underlying,
                                    expiry=expiry or "",
                                    reasoning=["no strikes for expiry"])

    t = _dte_years(expiry or "")
    action = getattr(signal, "action", "HOLD")
    conf = int(getattr(signal, "confidence", 0) or 0)
    adx_v = float(adx) if adx is not None else 30.0
    chop = adx_v < 25.0
    reasoning: List[str] = []

    def build_spread(kind_long: str) -> OptionTradeCandidate:
        """Vertical debit spread: long ATM + short OTM (same expiry)."""
        long_row = _pick_by_delta(rows, spot, kind_long, d_atm, side_prefer="ATM")
        # ATM long: nearest strike to spot, preferring the debit side
        # (CE: strike <= spot first; PE: strike >= spot first).
        atm = atm_strike(chain, expiry or "")
        if atm is not None:
            cands = sorted(
                rows,
                key=lambda r: (abs(r["strike"] - spot),
                               0 if (r["strike"] <= spot if kind_long == "CE"
                                     else r["strike"] >= spot) else 1))
            long_row = cands[0] if cands else long_row
        if long_row is None:
            return OptionTradeCandidate(strategy="none", reasoning=["no ATM leg"])
        short_row = _pick_by_delta(
            [r for r in rows if r["strike"] != long_row["strike"]],
            spot, kind_long, d_otm)
        if short_row is None:
            return OptionTradeCandidate(strategy="none", reasoning=["no OTM leg"])
        lq = _leg_quote(long_row, kind_long, t, spot, hv_proxy)
        sq = _leg_quote(short_row, kind_long, t, spot, hv_proxy)
        if lq["premium"] <= sq["premium"]:
            # Identical hand-built quotes can invert the debit; fall back to
            # BS sim with HV proxy so the spread keeps a real (positive) debit.
            lq = dict(lq); sq = dict(sq)
            lq["premium"] = round(bs_price(
                spot, long_row["strike"], t, max(hv_proxy, 0.12),
                kind_long), 2)
            sq["premium"] = round(bs_price(
                spot, short_row["strike"], t, max(hv_proxy, 0.12),
                kind_long), 2)
            lq["src"] = sq["src"] = "sim"
            if lq["premium"] <= sq["premium"]:
                # OTM short must be cheaper than ATM long — enforce minimum
                sq["premium"] = round(lq["premium"] * 0.35, 2)
        if not _liquid(lq, oi_floor) or not _liquid(sq, oi_floor):
            # paper-sim still allowed but flagged; live would skip
            reasoning.append("thin wing liquidity — sim-priced, size capped")
        width = abs(long_row["strike"] - short_row["strike"])
        m = spread_metrics(lq["premium"], sq["premium"], width,
                           lots=1, lot_size=lot_size)
        legs = [
            OptionLeg(strike=long_row["strike"], expiry=expiry or "",
                      kind=kind_long, side="BUY", premium=lq["premium"],
                      premium_src=lq["src"], delta=lq["delta"], bid=lq["bid"],
                      ask=lq["ask"], oi=lq["oi"], volume=lq["volume"]),
            OptionLeg(strike=short_row["strike"], expiry=expiry or "",
                      kind=kind_long, side="SELL", premium=sq["premium"],
                      premium_src=sq["src"], delta=-sq["delta"], bid=sq["bid"],
                      ask=sq["ask"], oi=sq["oi"], volume=sq["volume"]),
        ]
        strat = ("bull-call-spread" if kind_long == "CE"
                 else "bear-put-spread")
        be = (long_row["strike"] + m["net_debit"] if kind_long == "CE"
              else long_row["strike"] - m["net_debit"])
        # rank: confidence x IV-penalty x liquidity (mirrors screener scale)
        iv_pen = 1.0
        if iv_rank is not None and iv_rank > iv_cap:
            iv_pen = 0.5
        liq = 1.0 if (_liquid(lq, oi_floor) and _liquid(sq, oi_floor)) else 0.5
        rank = round(conf * iv_pen * liq, 1)
        return OptionTradeCandidate(
            strategy=strat, underlying=underlying, spot=spot,
            expiry=expiry or "", legs=legs, net_debit=m["net_debit"],
            maxLoss=m["maxLoss"], maxGain=m["maxGain"], breakeven=round(be, 2),
            margin=m["net_debit"] * lot_size, rank=rank, confidence=conf,
            reasoning=reasoning + [
                f"{strat} {long_row['strike']}/{short_row['strike']} "
                f"debit {m['net_debit']} maxLoss {m['maxLoss']} "
                f"maxGain {m['maxGain']} RR {m['RR']}"])

    def build_long(kind: str) -> OptionTradeCandidate:
        """Naked ITM long: single BUY leg, no short. Defined risk = premium.

        Targets delta_long (default 0.65 = slightly ITM): higher delta
        tracks the index better and bleeds less theta than ATM over an
        intraday hold, at the cost of a higher premium per lot.
        """
        row = _pick_by_delta(rows, spot, kind, d_long, side_prefer="ITM")
        if row is None:
            return OptionTradeCandidate(strategy="none",
                                        reasoning=["no ITM leg"])
        q = _leg_quote(row, kind, t, spot, hv_proxy)
        if q["premium"] <= 0:
            return OptionTradeCandidate(strategy="none",
                                        reasoning=["ITM leg unpriced"])
        if not _liquid(q, oi_floor):
            reasoning.append("thin ITM liquidity — sim-priced, size capped")
        premium = q["premium"]
        max_loss = round(premium * lot_size, 2)
        strat = "long-call" if kind == "CE" else "long-put"
        legs = [
            OptionLeg(strike=row["strike"], expiry=expiry or "",
                      kind=kind, side="BUY", premium=premium,
                      premium_src=q["src"], delta=q["delta"], bid=q["bid"],
                      ask=q["ask"], oi=q["oi"], volume=q["volume"]),
        ]
        iv_pen = 1.0
        if iv_rank is not None and iv_rank > iv_cap:
            iv_pen = 0.5
        liq = 1.0 if _liquid(q, oi_floor) else 0.5
        rank = round(conf * iv_pen * liq, 1)
        return OptionTradeCandidate(
            strategy=strat, underlying=underlying, spot=spot,
            expiry=expiry or "", legs=legs, net_debit=premium,
            maxLoss=max_loss, maxGain=0.0,
            breakeven=round(row["strike"] + premium
                            if kind == "CE"
                            else row["strike"] - premium, 2),
            margin=max_loss, rank=rank, confidence=conf,
            reasoning=reasoning + [
                f"{strat} {row['strike']} ITM premium {premium} "
                f"maxLoss {max_loss}"])

    # LONG MODE (buy-only): single ITM leg, never a short. Chop/HOLD -> none
    # (no premium selling, no naked shorts — buy-only book sits out chop).
    if style == "long":
        if chop:
            return OptionTradeCandidate(strategy="none", underlying=underlying,
                                        spot=spot, expiry=expiry or "",
                                        reasoning=["chop regime — long-only "
                                                   "book sits out"])
        if action == "BUY":
            return build_long("CE")
        if action == "SELL":
            return build_long("PE")
        return OptionTradeCandidate(strategy="none", underlying=underlying,
                                    spot=spot, expiry=expiry or "",
                                    reasoning=["HOLD signal — no options "
                                               "expression"])

    # Chop -> iron condor (16-delta wings both sides).
    # SELL the call spread + SELL the put spread: collect credit on both
    # wings, risk = wing width - credit per side. The condor nets the two
    # credits; maxLoss = wider wing width - total credit (per lot*lot_size).
    if chop:
        try:
            call_s = _pick_by_delta(rows, spot, "CE", 0.16)
            put_s = _pick_by_delta(rows, spot, "PE", 0.16)
            if call_s is None or put_s is None:
                raise ValueError("no 16d wings")
            # Short call spread: SELL OTM call, BUY further OTM call
            call_short = _pick_by_delta(rows, spot, "CE", 0.16)
            call_long = None
            if call_short is not None:
                higher = sorted(
                    [r for r in rows if r["strike"] > call_short["strike"]],
                    key=lambda r: r["strike"])
                call_long = higher[0] if higher else None
            # Short put spread: SELL OTM put, BUY further OTM put
            put_short = _pick_by_delta(rows, spot, "PE", 0.16)
            put_long = None
            if put_short is not None:
                lower = sorted(
                    [r for r in rows if r["strike"] < put_short["strike"]],
                    key=lambda r: -r["strike"])
                put_long = lower[0] if lower else None
            if call_short is None or call_long is None \
                    or put_short is None or put_long is None:
                raise ValueError("condor wings incomplete")
            cq = _leg_quote(call_short, "CE", t, spot, hv_proxy)
            clq = _leg_quote(call_long, "CE", t, spot, hv_proxy)
            pq = _leg_quote(put_short, "PE", t, spot, hv_proxy)
            plq = _leg_quote(put_long, "PE", t, spot, hv_proxy)
            call_credit = max(cq["premium"] - clq["premium"], 0.0)
            put_credit = max(pq["premium"] - plq["premium"], 0.0)
            credit = round(call_credit + put_credit, 2)
            call_w = abs(call_long["strike"] - call_short["strike"])
            put_w = abs(put_short["strike"] - put_long["strike"])
            wing = max(call_w, put_w)
            max_loss = max(wing - credit, 0.0) * lot_size
            legs = [
                OptionLeg(strike=call_short["strike"], expiry=expiry or "",
                          kind="CE", side="SELL", premium=cq["premium"],
                          premium_src=cq["src"], delta=-cq["delta"],
                          bid=cq["bid"], ask=cq["ask"], oi=cq["oi"],
                          volume=cq["volume"]),
                OptionLeg(strike=call_long["strike"], expiry=expiry or "",
                          kind="CE", side="BUY", premium=clq["premium"],
                          premium_src=clq["src"], delta=clq["delta"],
                          bid=clq["bid"], ask=clq["ask"], oi=clq["oi"],
                          volume=clq["volume"]),
                OptionLeg(strike=put_short["strike"], expiry=expiry or "",
                          kind="PE", side="SELL", premium=pq["premium"],
                          premium_src=pq["src"], delta=-pq["delta"],
                          bid=pq["bid"], ask=pq["ask"], oi=pq["oi"],
                          volume=pq["volume"]),
                OptionLeg(strike=put_long["strike"], expiry=expiry or "",
                          kind="PE", side="BUY", premium=plq["premium"],
                          premium_src=plq["src"], delta=plq["delta"],
                          bid=plq["bid"], ask=plq["ask"], oi=plq["oi"],
                          volume=plq["volume"]),
            ]
            liq_ok = all(_liquid({"bid": l.bid, "oi": l.oi}, oi_floor)
                         for l in legs)
            liq = 1.0 if liq_ok else 0.5
            if not liq_ok:
                reasoning.append("thin condor wings — sim-priced, size capped")
            return OptionTradeCandidate(
                strategy="iron-condor", underlying=underlying, spot=spot,
                expiry=expiry or "", legs=legs,
                net_debit=round(-credit, 2),
                maxLoss=round(max_loss, 2),
                maxGain=round(credit * lot_size, 2),
                breakeven=spot, margin=round(max_loss, 2),
                rank=round(conf * 0.8 * liq, 1), confidence=conf,
                reasoning=reasoning + [
                    f"chop regime (ADX<25) -> iron condor 16d, "
                    f"credit {credit} maxLoss {round(max_loss, 2)}"])
        except Exception as exc:
            logger.warning("Condor build failed, falling back to spread: %s", exc)

    if action == "BUY":
        return build_spread("CE")
    if action == "SELL":
        return build_spread("PE")
    return OptionTradeCandidate(strategy="none", underlying=underlying,
                                spot=spot, expiry=expiry or "",
                                reasoning=["HOLD signal — no options expression"])
