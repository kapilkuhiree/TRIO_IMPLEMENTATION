# Shadow short-premium sleeve — handoff

**Status: shadow-only pilot. 5 sessions recorded. Not approved for paper or live.**

## 1. Why this exists

The live ORB book already wins **68.8% of its 16 real trades**, but its
break-even win rate is **68.3%** and it nets **+₹20 per trade** (+₹326 total).
It is a zero-edge system, not a losing one.

The cause is exit geometry, not entry quality:

| | value |
| --- | --- |
| real wins | 11, avg **+₹1,519** |
| real losses | 5, avg **−₹3,277** |
| payoff ratio | **0.46** |
| win rate | 68.8% |
| break-even win rate | 68.3% |
| expectancy | **+₹20 / trade** |

The `profit_lock_rungs: [0.10, 0.15, 0.20, 0.25]` ladder ratchets the stop to
+10–25% of entry premium, so winners exit at ~0.5–0.75R while a stop costs
−20% of premium plus slippage. Win rate and payoff move in opposite
directions, so their product stays near zero.

> Note (2026-11-16): the shipped ladder now starts at 5%
> (`[0.05, 0.08, 0.12, 0.15, 0.20, 0.25]`) so at least one rung sits below the
> reduced afternoon target. The measurements above were taken with the old
> 10% first rung and are retained as the pre-change baseline.

Tightening the ladder raises the win rate and lowers the payoff in lockstep.
That is why an 80–90% win-rate target cannot fix the P&L (see
`HIGH_WINRATE_RESEARCH.md`).

## 2. The only structure that can produce mostly-green days

Selling defined-risk premium. Most spreads expire worthless, so the win rate
is structurally high; the question is whether the credit pays for the
occasional full-width loss.

Pilot on **real archived NIFTY option premiums** (not index points), one trade
per session, 09:45 → 15:05, bull put spread 1.5% OTM, 100 wide, ₹2 round-trip
cost for both legs:

| day | expiry | short | credit | exit | net ₹ |
| --- | --- | --- | --- | --- | --- |
| 2026-11-04 | 2026-11-17 | 23,550 | 22.05 | 19.45 | +39 |
| 2026-11-05 | 2026-11-17 | 24,000 | 18.15 | 13.10 | +198 |
| 2026-11-06 | 2026-11-17 | 24,200 | 21.70 | 10.75 | +582 |
| 2026-11-09 | 2026-11-17 | 24,550 | 24.45 | 19.95 | +162 |
| 2026-11-11 | 2026-11-17 | 25,000 | 20.05 | 36.10 | **−1,173** |

- **Green days: 4/5 (80%)**
- Net: **−2.95 points = −₹192 / lot**
- Worst day **−₹1,173**; best **+₹582**; profit factor **0.84**
- Break-even win rate at this geometry: **78.7%**; realised 80.0%

**Read it honestly:** 80% green days, negative net. One bad day erased six
green days. At ₹2 cost the credit is roughly fair; the edge, if any, is too
small to see in five sessions.

Cost sensitivity on the same five sessions: ₹0 → +₹458, ₹1 → +₹133,
₹2 → −₹192, ₹3 → −₹517. The sleeve only works if two-leg cost stays near
₹65–130 per lot.

## 3. What is built

| file | purpose |
| --- | --- |
| `src/shadow_short_premium.py` | pure planning + settlement + archive replay + idempotent JSONL ledger |
| `scripts/shadow_short_premium.py` | `--date`, `--backfill`, `--report` CLI |
| `tests/test_shadow_short_premium.py` | 15 tests (strike selection, credit guards, P&L math, ledger) |
| `output/shadow_short_premium.jsonl` | the ledger (5 records) |

**Nothing in the live runtime imports this module.** It places no orders, sizes
nothing, and cannot block or alter an order.

### Run

```powershell
python scripts/shadow_short_premium.py --date 2026-11-11
python scripts/shadow_short_premium.py --backfill
python scripts/shadow_short_premium.py --report
```

Re-running is safe: records are keyed by
`(day, strategy, expiry, short_strike, long_strike)` and duplicates are skipped.

## 4. Promotion criteria (do not skip)

Record 20–30 sessions, then require **all** of:

1. green days ≥ 70%;
2. net > 0 after ₹2/leg round-trip cost;
3. profit factor ≥ 1.2;
4. worst day ≤ 3× average credit (the tail must not eat the year);
5. no single session loss > 2× the average green-day gain.

If any fails, the sleeve dies. Five sessions cannot pass or fail it.

## 5. Data limitation

The option archive only has dense two-leg coverage from **2026-11-04**. Earlier
sessions (Oct 12 – Nov 3) rarely contain a 1.5%-OTM strike at 09:45, so the
rule cannot be expressed. The ledger will grow by one record per session as
the archive grows.

## 6. Hard rules

- Shadow only. No order path, no position sizing, no risk-cap interaction.
- Defined risk: one short put, one long put, same expiry, fixed width.
- Do not tune OTM/width/stop on five sessions.
- Revisit only at ≥ 20 recorded sessions.
