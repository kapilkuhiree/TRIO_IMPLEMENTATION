"""Broad index discovery maps to exact Kite spot names, never proxy stocks."""

import pytest

from src.zerodha_market_data import ZerodhaMarketData


@pytest.mark.parametrize("symbol,expected", [
    ("^NSEI", "NIFTY 50"), ("NIFTY", "NIFTY 50"),
    ("^NSEBANK", "NIFTY BANK"), ("BANKNIFTY", "NIFTY BANK"),
    ("NIFTY_FIN_SERVICE.NS", "NIFTY FIN SERVICE"), ("FINNIFTY", "NIFTY FIN SERVICE"),
    ("^NSMIDCP", "NIFTY NEXT 50"), ("NIFTYNXT50", "NIFTY NEXT 50"),
    ("NIFTY_MID_SELECT.NS", "NIFTY MID SELECT"), ("MIDCPNIFTY", "NIFTY MID SELECT"),
    ("RELIANCE.NS", "RELIANCE"), ("BANKNIFTYLTD.NS", "BANKNIFTYLTD"),
])
def test_exact_spot_name(symbol, expected):
    assert ZerodhaMarketData._equity_symbol(symbol) == expected
