"""
TRIO — Screener tests.
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

The screener ranks sized signals and returns only the best. Tests use
hand-built signals so no network is needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators import IndicatorReading, TechnicalReadings
from src.signal_engine import TradeSignal
from src.screener import rank_signal, resolve_basket, BASKETS


def _sig(action="BUY", confidence=80, entry=100.0, stop=96.0,
         pullback=False):
    s = TradeSignal(symbol="TEST", action=action, entry_price=entry,
                    stop_loss=stop, target=106.0, position_size=10,
                    confidence=confidence,
                    reasoning=["pullback"] if pullback else ["composite"])
    return s


def _readings(atr=2.0, pullback=False):
    return TechnicalReadings(
        symbol="TEST",
        indicators={"atr_14": IndicatorReading(value=atr, signal="info")},
        summary={},
        pullback_detected=pullback,
    )


def test_rank_prefers_pullback_long():
    pull = rank_signal(_sig("BUY", 80, 100.0, 96.0), _readings(2.0, True))
    other = rank_signal(_sig("BUY", 80, 100.0, 96.0), _readings(2.0, False))
    assert pull.rank > other.rank
    assert pull.setup_name == "pullback-long"


def test_rank_zero_when_stop_too_tight():
    # Entry 0.1 away from stop with ATR 2.0 -> edge 0.05 < 0.25 floor.
    cand = rank_signal(_sig("BUY", 95, 100.0, 99.9), _readings(2.0, True))
    assert cand.rank == 0.0
    assert cand.setup_name == "too-tight"


def test_rank_scales_with_edge():
    wide = rank_signal(_sig("BUY", 80, 100.0, 94.0), _readings(2.0, True))
    narrow = rank_signal(_sig("BUY", 80, 100.0, 98.0), _readings(2.0, True))
    assert wide.rank > narrow.rank


def test_composite_short_classified_from_lowercase_macd():
    """Classifier regression (2026-10-06): the live engine emits
    reasoning lines like 'macd: bearish (value=...)' — lowercase. The
    old literal-'MACD' string match demoted 83% of SELL signals to
    'other'. Classification must come from the indicator readings."""
    from types import SimpleNamespace

    sig = TradeSignal(symbol="HDFCBANK.NS", action="SELL",
                      entry_price=709.0, stop_loss=716.44, target=698.46,
                      position_size=28, confidence=84,
                      reasoning=["2/16 technical indicators bullish "
                                 "(tech_score=-0.69)",
                                 "  sma_9: bearish (value=710.69)",
                                 "  macd: bearish (value=0.17)"])
    rd = TechnicalReadings(
        symbol="HDFCBANK.NS",
        indicators={
            "atr_14": IndicatorReading(value=2.5, signal="info"),
            "macd": IndicatorReading(value=-0.5, signal="bearish",
                                     extra={"signal_line": 0.3}),
        },
        summary={},
        pullback_detected=False,
    )
    cand = rank_signal(sig, rd)
    assert cand.setup_name == "composite-short"
    assert cand.rank > rank_signal(sig, TechnicalReadings(
        symbol="X", indicators={"atr_14": IndicatorReading(
            value=2.5, signal="info")}, summary={})).rank


def test_resolve_basket_name():
    syms = resolve_basket("nifty50")
    assert len(syms) == 50
    assert "RELIANCE.NS" in syms
    assert syms == BASKETS["nifty50"]  # returns a copy, order preserved


def test_resolve_basket_list_passthrough():
    assert resolve_basket(["A", "B"]) == ["A", "B"]


def test_candidate_to_dict():
    cand = rank_signal(_sig("BUY", 80, 100.0, 96.0), _readings(2.0, True))
    d = cand.to_dict()
    assert d["rank"] == cand.rank
    assert d["edge_atr"] == 2.0
    assert d["setup_name"] == "pullback-long"
    assert d["symbol"] == "TEST"
