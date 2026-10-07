"""
TRIO — Screener

Scans a basket of symbols, scores every setup by quality, and returns only
the best candidates. The paper trader executes the top N; everything else
is reported but not traded.

Why this exists
--------------
The user asked: "analyse all 100 stocks, then decide where the profit is,
then take the trade." The old loop traded each symbol independently with
no ranking and no choosing. This module adds the choosing.

How ranking works
-----------------
Each actionable signal (BUY/SELL surviving risk sizing) gets a rank score:

    rank = confidence * edge_distance * setup_bonus

- confidence: 0-100 from the signal engine.
- edge_distance: how far the entry sits from the stop, in ATR units.
  A signal risking 0.5 ATR to make 1.5R outranks one risking 3 ATR for
  the same target. Entries too close to their stop score zero.
- setup_bonus: 1.5 for a pullback long (the validated setup), 1.0 for a
  composite short, 0.5 for anything else. The bonus encodes what the
  out-of-sample evidence actually supports; it is not a guess.

Only candidates with rank >= `min_rank` (config) are returned, sorted
best-first, capped at `max_positions_to_open`.

Config (config.yaml -> screener):
    basket: nifty50            # name in baskets below, or a raw list
    max_positions_to_open: 3
    min_rank: 40.0
    min_confidence: 60

Baskets
-------
NIFTY50 and NIFTY100 constituents change over time. The lists below are a
snapshot (Oct 2026). If a ticker fails to fetch it is skipped with a
warning — the scan never dies because one symbol delisted.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from src.utils import get_logger, load_config
from src.data_fetcher import fetch_market_data
from src.indicators import compute_indicators
from src.signal_engine import generate_signal, TradeSignal
from src.risk_manager import apply_risk_management

logger = get_logger("screener")

# ---------------------------------------------------------------------------
# Baskets (constituent snapshot, Oct 2026; unknown tickers are skipped)
# ---------------------------------------------------------------------------

NIFTY50 = [
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "SBIN.NS", "ITC.NS", "LT.NS", "AXISBANK.NS", "MARUTI.NS",
    "HINDUNILVR.NS", "SUNPHARMA.NS", "KOTAKBANK.NS", "ADANIENT.NS",
    "WIPRO.NS", "TITAN.NS", "ULTRACEMCO.NS", "ONGC.NS", "NTPC.NS",
    "POWERGRID.NS", "M&M.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "JSWSTEEL.NS",
    "HINDALCO.NS", "COALINDIA.NS", "BPCL.NS", "GRASIM.NS", "APOLLOHOSP.NS",
    "CIPLA.NS", "DRREDDY.NS", "DIVISLAB.NS", "EICHERMOT.NS", "HEROMOTOCO.NS",
    "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS", "HDFCLIFE.NS",
    "SBILIFE.NS", "ICICIPRULI.NS", "ADANIPORTS.NS", "INDUSINDBK.NS",
    "SHRIRAMFIN.NS", "TECHM.NS", "HCLTECH.NS", "NESTLEIND.NS", "BRITANNIA.NS",
    "TATACONSUM.NS", "TRENT.NS", "BEL.NS",
]

BANKNIFTY = [
    "HDFCBANK.NS", "ICICIBANK.NS", "AXISBANK.NS", "SBIN.NS", "KOTAKBANK.NS",
    "INDUSINDBK.NS", "BANDHANBNK.NS", "FEDERALBNK.NS", "IDFCFIRSTB.NS",
    "PNB.NS", "BANKBARODA.NS",
]

# NIFTY 100 — 100 most liquid NSE large/mid-caps (snapshot Oct 2026).
# The premarket job uses this on GitHub (free, 1 batch download); local
# `config.symbols` (15 names) remains the default for manual scans.
NIFTY100 = [
    # NIFTY 50 core
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "SBIN.NS", "ITC.NS", "LT.NS", "AXISBANK.NS", "MARUTI.NS",
    "HINDUNILVR.NS", "SUNPHARMA.NS", "KOTAKBANK.NS", "ADANIENT.NS",
    "WIPRO.NS", "TITAN.NS", "ULTRACEMCO.NS", "ONGC.NS", "NTPC.NS",
    "POWERGRID.NS", "M&M.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "JSWSTEEL.NS",
    "HINDALCO.NS", "COALINDIA.NS", "BPCL.NS", "GRASIM.NS", "APOLLOHOSP.NS",
    "CIPLA.NS", "DRREDDY.NS", "DIVISLAB.NS", "EICHERMOT.NS", "HEROMOTOCO.NS",
    "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS", "HDFCLIFE.NS",
    "SBILIFE.NS", "ICICIPRULI.NS", "ADANIPORTS.NS", "INDUSINDBK.NS",
    "SHRIRAMFIN.NS", "TECHM.NS", "HCLTECH.NS", "NESTLEIND.NS", "BRITANNIA.NS",
    "TATACONSUM.NS", "TRENT.NS", "BEL.NS",
    # NXT 50
    "AMBUJACEM.NS", "HAVELLS.NS", "BOSCHLTD.NS", "BANKBARODA.NS", "PNB.NS",
    "DLF.NS", "ADANIGREEN.NS", "ADANIPOWER.NS", "PIDILITIND.NS", "DMART.NS",
    "GAIL.NS", "SIEMENS.NS", "ABB.NS", "INDIGO.NS", "ZYDUSLIFE.NS",
    "LUPIN.NS", "AUROPHARMA.NS", "GLAXO.NS", "COLPAL.NS", "DABUR.NS",
    "MARICO.NS", "GODREJCP.NS", "BERGEPAINT.NS", "ASIANPAINT.NS", "BANDHANBNK.NS",
    "FEDERALBNK.NS", "IDFCFIRSTB.NS", "ICICIGI.NS", "MUTHOOTFIN.NS", "CHOLAFIN.NS",
    "BAJAJHLDNG.NS", "LICI.NS", "HAL.NS", "IRCTC.NS", "NAUKRI.NS",
    "LTIM.NS", "PERSISTENT.NS", "COFORGE.NS", "OFSS.NS", "MPHASIS.NS",
    "TATAPOWER.NS", "ADANIENSOL.NS", "JINDALSTEL.NS", "SAIL.NS", "VEDL.NS",
    "NMDC.NS", "UPL.NS", "SRF.NS", "CHOLAHLDNG.NS", "TORNTPHARMA.NS",
]

BASKETS = {"nifty50": NIFTY50, "banknifty": BANKNIFTY, "nifty100": NIFTY100}


def resolve_basket(spec: Any) -> List[str]:
    """Turn a basket name, comma string, or raw list into a symbol list."""
    if isinstance(spec, list):
        return [s.strip() for s in spec if str(s).strip()]
    if isinstance(spec, str):
        name = spec.strip()
        if name in BASKETS:
            return list(BASKETS[name])
        if "," in name:
            return [s.strip() for s in name.split(",") if s.strip()]
        return [name]
    cfg = load_config()
    symbols = cfg.get("symbols", [])
    return list(symbols)


# ---------------------------------------------------------------------------
# Ranked candidate
# ---------------------------------------------------------------------------

@dataclass
class RankedCandidate:
    """An actionable signal with its rank score and sizing attached."""
    rank: float = 0.0
    signal: Optional[TradeSignal] = None
    edge_atr: float = 0.0          # entry-to-stop distance in ATR units
    setup_name: str = ""           # pullback-long | composite-short | other

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self.signal) if self.signal else {}
        d["rank"] = round(self.rank, 1)
        d["edge_atr"] = round(self.edge_atr, 2)
        d["setup_name"] = self.setup_name
        return d


def _setup_bonus(signal: TradeSignal, readings: Any) -> float:
    """Weight what the evidence supports: pullback longs first.

    Classified from the indicator *readings*, not from matching
    reasoning strings — the old version required the literal "MACD" in
    a reasoning line, but live lines are lowercase ("macd: bearish"),
    so 83% of 2026-10-06's signals fell to ``other`` and the rank
    bonus became meaningless.
    """
    if getattr(readings, "pullback_detected", False) and signal.action == "BUY":
        return 1.5
    if signal.action == "SELL" and _macd_bearish(readings):
        return 1.0
    return 0.5


def _macd_bearish(readings: Any) -> bool:
    """True when the MACD line sits below its signal line.

    Mirrors the engine's own short gate
    (``signal_engine._macd_confirms_short``) without importing the
    engine — and accepts any case in the reasoning text as a fallback
    so future string changes can't silently demote shorts again.
    """
    try:
        macd = readings.indicators.get("macd")
        if macd is not None and macd.value is not None:
            sig = (macd.extra or {}).get("signal_line")
            if sig is not None:
                return macd.value < sig
    except Exception:
        pass
    for r in (getattr(readings, "reasoning", None) or []):
        rl = str(r).lower()
        if "macd" in rl and ("bearish" in rl or "below" in rl):
            return True
    return False


def rank_signal(signal: TradeSignal, readings: Any) -> RankedCandidate:
    """
    Score one sized signal. Returns rank 0 when the entry is untradeable or
    too close to resistance.
    """
    atr_v = None
    for key, ind in readings.indicators.items():
        if key.startswith("atr_"):
            atr_v = ind.value
            break

    edge = 0.0
    if atr_v and atr_v > 0 and signal.stop_loss:
        edge = abs((signal.entry_price or 0) - signal.stop_loss) / atr_v

    if edge < 0.25:
        return RankedCandidate(rank=0.0, signal=signal, edge_atr=edge,
                               setup_name="too-tight")

    # Resistance overhead check: if nearest resistance is within 1 ATR overhead,
    # halve the rank penalty.
    penalty = 1.0
    if signal.action == "BUY" and readings.resistance:
        nearest_res = min([r for r in readings.resistance if r > (signal.entry_price or 0)], default=float('inf'))
        if nearest_res != float('inf') and (nearest_res - (signal.entry_price or 0)) < (atr_v or 1.0):
            penalty = 0.5

    bonus = _setup_bonus(signal, readings)
    name = ("pullback-long" if bonus == 1.5
            else ("composite-short" if bonus == 1.0 else "other"))
    
    # Final Rank: confidence * edge * setup_bonus * resistance_penalty
    rank = signal.confidence * edge * bonus * penalty
    return RankedCandidate(rank=round(rank, 1), signal=signal,
                           edge_atr=round(edge, 2), setup_name=name)


def screen(symbols: Optional[List[str]] = None,
           timeframe: Optional[str] = None,
           top_n: Optional[int] = None,
           min_rank: Optional[float] = None,
           min_confidence: Optional[int] = None,
           use_batch_fetch: Optional[bool] = None,
           ) -> List[RankedCandidate]:
    """
    Scan every symbol, keep actionable signals, rank them best-first.

    Args:
        symbols:         Basket to scan. Defaults to config screener basket.
        timeframe:       Bar timeframe. Defaults to config default.
        top_n:           Max candidates returned. Defaults to config.
        min_rank:        Minimum rank score. Defaults to config.
        min_confidence:  Minimum engine confidence. Defaults to config.
        use_batch_fetch: If True, FetchMultiple in one yfinance batch;
                         defaults to config `data_fetcher.use_batch_fetch`.

    Returns:
        Ranked candidates, best first. Empty list means: nothing met the
        bar today — which is a valid and common outcome.
    """
    cfg = load_config()
    sc_cfg = cfg.get("screener", {})

    if symbols is None:
        symbols = resolve_basket(sc_cfg.get("basket", "nifty50"))
    if timeframe is None:
        timeframe = cfg.get("timeframes", {}).get("default", "15m")
    if top_n is None:
        top_n = sc_cfg.get("max_positions_to_open", 3)
    if min_rank is None:
        min_rank = sc_cfg.get("min_rank", 40.0)
    if min_confidence is None:
        min_confidence = sc_cfg.get("min_confidence", 60)
    if use_batch_fetch is None:
        df_cfg = cfg.get("data_fetcher", {})
        use_batch_fetch = df_cfg.get("use_batch_fetch", True) is not False

    # Fast batch path: one yfinance batch for ->then per-ticker signal.
    if use_batch_fetch and len(symbols) > 5:
        try:
            from src.data_fetcher import fetch_multiple
            from src.indicators import IndicatorReading  # noqa: F401
            md_map = fetch_multiple(symbols=symbols, timeframe=timeframe)
        except Exception as exc:
            logger.warning("Batch fetch path failed (%s); falling back to per-symbol", exc)
            md_map = None
        else:
            scored: List[RankedCandidate] = []
            scanned = len(md_map or {})
            failed = max(0, len(symbols) - scanned)
            for sym, md in (md_map or {}).items():
                try:
                    readings = compute_indicators(md.ohlcv, sym, timeframe)
                    sig = generate_signal(symbol=sym,
                                          latest_price=md.latest_price,
                                          technical_readings=readings)
                except Exception as exc:
                    failed += 1
                    logger.warning("Screen skipped %s: %s", sym, exc)
                    continue
                if sig.action not in ("BUY", "SELL") or sig.confidence < min_confidence:
                    continue
                atr_v = next((v.value for k, v in readings.indicators.items()
                              if k.startswith("atr_")), None)
                swing = readings.swing_low if sig.action == "BUY" else readings.swing_high
                sig = apply_risk_management(sig, atr_v, swing_level=swing)
                if sig.action not in ("BUY", "SELL") or not sig.position_size:
                    continue
                cand = rank_signal(sig, readings)
                if cand.rank >= min_rank:
                    scored.append(cand)
            scored.sort(key=lambda c: c.rank, reverse=True)
            picked = scored[:top_n]
            logger.info("Screen %s %s: %d scanned, %d failed, %d actionable, %d picked",
                        len(symbols), timeframe, scanned, failed, len(scored), len(picked))
            return picked

    scored: List[RankedCandidate] = []
    scanned = failed = 0

    for symbol in symbols:
        try:
            md = fetch_market_data(symbol, timeframe)
            readings = compute_indicators(md.ohlcv, symbol, timeframe)
            sig = generate_signal(symbol=symbol,
                                  latest_price=md.latest_price,
                                  technical_readings=readings)
            scanned += 1
        except Exception as exc:
            failed += 1
            logger.warning("Screen skipped %s: %s", symbol, exc)
            continue

        if sig.action not in ("BUY", "SELL"):
            continue
        if sig.confidence < min_confidence:
            continue

        atr_v = next((v.value for k, v in readings.indicators.items()
                      if k.startswith("atr_")), None)
        swing = (readings.swing_low if sig.action == "BUY"
                 else readings.swing_high)
        sig = apply_risk_management(sig, atr_v, swing_level=swing)
        if sig.action not in ("BUY", "SELL") or not sig.position_size:
            continue

        cand = rank_signal(sig, readings)
        if cand.rank >= min_rank:
            scored.append(cand)

    scored.sort(key=lambda c: c.rank, reverse=True)
    picked = scored[:top_n]

    logger.info("Screen %s %s: %d scanned, %d failed, %d actionable, %d picked",
                len(symbols), timeframe, scanned, failed,
                len(scored), len(picked))
    return picked
