# Entry cap and clock validation (2026-11-20)

Question: the 10-entry cap ran out at 12:29 on 2026-11-20 and refused 325 later
signals while NIFTY rose another 90 points. The 2026-11-17 session had the
opposite problem: 26 entries, and the eight after 12:30 lost Rs 9,252. Two
days, two opposite signs. Which rule survives 96 sessions?

Script: `scripts/validate_entry_cap.py` (offline; reads the cached 5m frames).
Tests: `tests/test_entry_cap_validation.py`.

## Method

- Window: 2026-07-06 to 2026-11-20, **96 complete NIFTY sessions**.
  2026-11-19 is excluded because the cached frame holds only 27 bars for it.
- Model: ORB + ORB-Retest candidate streams sharing one daily-entry counter,
  max 2 open positions, one per underlying, ORB loss-only 300s cooldown,
  Rs 18,000 premium cap and 0.7% risk cap for sizing.
- Pricing: synthetic Black-Scholes at the **actual DTE** per session, 14% IV
  (18% was demonstrably too rich against the option archive). Results are
  also shown at 18% and 22%.
- Only the daily cap and the last-entry clock change between variants.

## Variants — NIFTY, both sleeves, all sessions

| Variant | Trades | Win% | Net Rs | PF | Max DD | Win days |
|---|---:|---:|---:|---:|---:|---:|
| current (cap 10, 14:30) | 241 | 60.6% | 357,281 | 2.30 | -28,752 | 50/96 |
| cap 14 | 247 | 61.1% | 381,681 | 2.37 | -28,752 | 50/96 |
| cutoff 12:30, no cap | 150 | 61.3% | 228,756 | 2.20 | -19,987 | 46/96 |
| cutoff 12:30 + cap 14 | 150 | 61.3% | 228,756 | 2.20 | -19,987 | 46/96 |
| no cap, 14:30 | 248 | 61.3% | 386,738 | 2.39 | -28,752 | 50/96 |

## Cap sweep (no clock cutoff)

| Cap | 8 | 10 | 12 | 14 | 16 | 20 | none |
|---|---:|---:|---:|---:|---:|---:|---:|
| Net Rs | 341,523 | 357,281 | 379,960 | **381,681** | 386,738 | 386,738 | 386,738 |
| PF | 2.26 | 2.30 | 2.38 | 2.37 | 2.39 | 2.39 | 2.39 |
| Max DD | -28,752 | -28,752 | -28,752 | -28,752 | -28,752 | -28,752 | -28,752 |

The curve is monotonic up to 16 and then flat. Cap 14 captures ~97% of the
uncapped net with no extra drawdown, so it keeps a backstop without rationing
a normal day.

## Clock sweep (cap 14)

| Last entry | 12:00 | 12:30 | 13:00 | 13:30 | 14:00 | 14:30 |
|---|---:|---:|---:|---:|---:|---:|
| Net Rs | 205,690 | 228,756 | 234,025 | 292,421 | 324,257 | **381,681** |
| PF | 2.23 | 2.20 | 2.05 | 2.19 | 2.26 | 2.37 |
| Max DD | -19,987 | -19,987 | -20,179 | -24,916 | -24,752 | -28,752 |

Later is better at every step. Entries after 12:30 are not a leak in this
sample: 98 trades, 61.2% winners, **+Rs 157,982**.

Concentration check on those 98 late trades:

- by month: Jul +15,561, Aug -6,277, Sep +43,152, Oct +79,602, Nov +25,944
- top 5 trades = Rs 43,077, i.e. 27% of the late total (not one-day luck)
- post-expiry days +26,696, other days +131,286

## Post-expiry scope (E+1/E+2)

| Variant | Trades | Net Rs | PF | Win days |
|---|---:|---:|---:|---:|
| current (cap 10) | 76 | 72,756 | 2.28 | 21/40 |
| cap 14 | 76 | 72,756 | 2.28 | 21/40 |
| cutoff 12:30 | 46 | 46,060 | 2.15 | 19/40 |

The cap never binds on post-expiry days (76 trades under both), and the
cutoff removes Rs 26,700 of profit. Cycles: 12/20 positive without the
cutoff, 11/20 with it.

## Robustness

IV sensitivity, NIFTY both sleeves, all sessions, net Rs:

| IV | current | cutoff + cap 14 | delta |
|---|---:|---:|---:|
| 14% | 357,281 | 228,756 | -128,525 |
| 18% | 196,071 | 118,650 | -77,421 |
| 22% | 145,793 | 109,085 | -36,708 |

The cutoff loses at every IV. The cap-14 gain is smaller but positive at all
three (e.g. +24,400 at 14%).

## Decision

- **Raise `max_daily_entries` 10 -> 14.** Supported.
- **Do not add a 12:30 entry cutoff.** The 94-session prior and the 20-Nov
  single day both point the other way; only 17-Nov supported it. The cutoff's
  one benefit is a smaller max drawdown (-19,987 vs -28,752), which is not
  worth Rs 128,525 of net.
- **No per-contract cap.** Already rejected separately: capping 2026-11-20's
  25550 CE at 3 entries would have turned +Rs 5,020 into -Rs 42.

Reproduce:

```
python scripts/validate_entry_cap.py --json-out output/entry_cap_validation.json
```
