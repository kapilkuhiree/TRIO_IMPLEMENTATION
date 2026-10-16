# TRIO options strategy research log

**Research date:** 2026-10-15
**Status:** evidence insufficient for a production strategy winner

## Data coverage gate

The required Zerodha credentials were not available to this execution
environment:

```text
ZERODHA_API_KEY: absent
ZERODHA_ACCESS_TOKEN: absent
```

The read-only smoke command therefore stopped before authentication:

```text
python scripts/zerodha_readonly_smoke.py
→ ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required
→ orders_placed: 0
```

Repository inspection found no bundled option-premium CSV, Parquet, database,
or populated expired-contract archive. An incremental SQLite archive is now
implemented in `src/zerodha_option_archive.py`, but it is empty until a
credentialed collection run captures active contracts. The existing Zerodha
adapter can resolve currently listed NFO instruments and request historical
candles for a known instrument token, but it has no expired-contract catalogue
or retrospective expired-option history downloader.

**Verified genuine Zerodha option-candle coverage: none.** Historical option
P&L is therefore not claimed. The production runtime continues to emit
`NOT_READY_FOR_OPTIONS_PNL` for this reason.

## Exploratory model-only iterations

These runs are explicitly **not historical option backtests**. They use daily
NIFTY underlying candles and Black-Scholes repricing. They are useful only for
relative sensitivity checks and cannot establish option profitability,
slippage, liquidity, or out-of-sample validity.

The harness was corrected to use the configured 2026 NIFTY lot size of 65.
It includes ₹20 per order and the configured 0.15% sold-premium STT estimate,
but does not model genuine bid/ask candles or exchange execution.

### 60-day exploratory sweep, 18% volatility, 3 DTE

| Variant | Model expression | Trades | Win rate | Model net P&L | PF |
|---|---|---:|---:|---:|---:|
| D | ATM long CE/PE, delta proxy 0.50 | 9 | 66.7% | ₹7,535 | 1.98 |
| A | ITM long CE/PE, delta proxy 0.65 | 9 | 66.7% | ₹7,331 | 1.91 |
| K | wider debit spread | 9 | 66.7% | ₹4,677 | 1.69 |
| B | ATM debit spread | 9 | 66.7% | ₹2,848 | 1.63 |
| C | ITM debit spread | 9 | 66.7% | ₹2,643 | 1.54 |
| L | narrow debit spread | 9 | 66.7% | ₹1,226 | 1.32 |

### Sensitivity checks for the two leading model variants

| Case | D model net P&L | D PF | A model net P&L | A PF |
|---|---:|---:|---:|---:|
| 15% vol, 3 DTE | ₹8,115 | 2.08 | ₹7,872 | 1.99 |
| 18% vol, 3 DTE | ₹7,535 | 1.98 | ₹7,331 | 1.91 |
| 22% vol, 3 DTE | ₹6,988 | 1.88 | ₹6,819 | 1.83 |
| 18% vol, 5 DTE | ₹6,788 | 1.87 | ₹6,627 | 1.82 |

### Longer exploratory window

Using the same model over 180 requested days produced only 25 model trades:

| Variant | Trades | Win rate | Model net P&L | PF |
|---|---:|---:|---:|---:|
| D | 25 | 52.0% | ₹5,992 | 1.16 |
| A | 25 | 52.0% | ₹5,284 | 1.14 |
| K | 25 | 52.0% | -₹59 | 1.00 |
| B | 25 | 52.0% | -₹1,868 | 0.91 |
| C | 25 | 52.0% | -₹2,577 | 0.89 |
| L | 25 | 52.0% | -₹4,486 | 0.75 |

The longer run materially weakens the short-window result. Maximum drawdown,
daily consistency, and a genuine out-of-sample option-premium split are not
available from this model-only harness, so no variant is promoted as a
validated winner.

## Strategy coverage

- ORB, pullback, and momentum signal boundaries already exist in the strict
  planner and remain available for current-session planning.
- VWAP continuation is not implemented as a production strategy boundary.
- EMA/ADX logic exists in the broader strategy code, but the available
  backtest path does not connect it to genuine option-premium candles.
- A hybrid cannot be evidence-selected without real option candles,
  timestamped contract identity, bid/ask, OI, volume, and historical lot/expiry
  data.

## Decision

Variant D is only the strongest **model-relative** result in these runs. It is
not selected for production because the evidence is synthetic, the sample is
small, and the longer window is weak. The strict production options mode keeps
its existing long-only CE/PE signal pipeline, completed-bar validation,
current-chain contract selection, 20% premium stop, 30% premium target, risk
limits, duplicate prevention, fresh-bid exits, and 15:10 hard-flat.

## Safety work completed with this research

- Plan filename/payload date mismatches are rejected.
- Strict options sessions reject a non-current session date.
- `--allow-stale-plan` is ignored in strict options mode.
- Strict runtime rejects a previous-session plan before calling the broker.
- Strict runtime rejects a previous-session signal before calling the broker.
- No live order endpoint was called.

## Required follow-up before strategy selection

Run the read-only Zerodha coverage probe in an environment with valid daily
credentials, then archive genuine NIFTY and BANKNIFTY option-premium candles
with contract identity, exchange timestamps, bid/ask, volume, OI, expiry, and
historical lot size. Only then rerun ORB, VWAP, EMA/ADX pullback, momentum,
and hybrid variants with chronological out-of-sample testing.
