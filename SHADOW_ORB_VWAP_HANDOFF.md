# Shadow ORB + True-VWAP Handoff

Status: **shadow only, forward measurement in progress.**
Live sleeves (`orb`, `orb_retest`) are unchanged. Nothing here places,
sizes, blocks or alters an order.

## Why this exists

The live `orb` sleeve makes decisions on the **NIFTY spot index**, whose
candles report `Volume = 0`. The scan-level "VWAP" therefore fell back to an
equal-weighted average of `(H+L+C)/3` — that is not a VWAP.

`src/options_live_scan.py:177-185`:

```python
if vol <= 0:
    # Index feeds (^NSEI/^NSEBANK) report Volume=0 on yfinance.
    # Fall back to an equal-weighted typical price so VWAP remains
    # defined instead of rejecting every candidate.
    pv += typical
    volume += 1.0
```

`strategies/orb_retest_strategy.py:446-460` is worse: it never reads volume at
all. And `strategies/orb_strategy.py:295` fails **open** (`if vwap and ...`).

On 2026-11-11 all four live entries had `Volume = 0` on every one of the 75
index bars, so every "VWAP" printed that day was a session mean.

## What this sleeve measures

* **Same instrument.** ORB and VWAP are both computed from the nearest
  **NIFTY futures** series, which carries genuine volume.
* **True VWAP.** `Sum(typical * volume) / Sum(volume)`, per session.
* **Fail closed.** Zero volume, no bars, or coverage below 90% produces
  `status=data_issue` and **no signal**. There is no fallback.
* **Five variants.**
  * `A` ORB only
  * `B` ORB + true VWAP
  * `C` ORB + VWAP + retest
  * `D` ORB + VWAP + retest + EMA20
  * `E` ORB + VWAP, **one entry per leg, re-arm allowed** (max 2 per session)
* **Geometry.** Premium-equivalent stop of 80 underlying points (≈ −20% of a
  ₹200 ATM option at delta 0.5), configurable `--stop-points`, target `R`
  configurable `--reward-risk` (default 2.0).
* **Costs.** 2.05 underlying points per round trip (₹66.5 ÷ ₹32.5/point).
* **One trade per session** for A-D; E allows a new entry only after price
  closes back inside the opening range (a real re-arm). 2-close confirmation,
  entry floor 10:00, cutoff 14:30, hard flat 15:10.

## Files

| path | role |
|---|---|
| `src/shadow_orb_vwap.py` | VWAP contract, signal engine, futures adapter, ledger, promotion gate |
| `scripts/shadow_orb_vwap.py` | CLI: `--date`, `--backfill`, `--report` |
| `tests/test_shadow_orb_vwap.py` | 27 tests |
| `output/shadow_orb_vwap.jsonl` | append-only ledger |
| `src/live_vwap_audit.py` | read-only live-entry classifier, ledger, summary |
| `scripts/audit_live_vwap.py` | CLI: `--date`, `--cumulative`, `--report` |
| `tests/test_live_vwap_audit.py` | 19 tests |
| `output/live_vwap_audit.jsonl` | append-only audit ledger |

## Daily operation

Run once after the 15:10 flat:

```powershell
python scripts\shadow_orb_vwap.py --date 2026-11-12
python scripts\audit_live_vwap.py --date 2026-11-12
```

The first appends one record per variant (A-E) with `source="forward"` — key
`(day, variant, source)`. The second classifies each live entry against the
corrected rules — key `(day, position_id, entry_ts)`. Both are idempotent.

A forward run into the real ledger refuses a session whose last bar is before
the **15:10 flat** — a midday run cannot freeze a provisional stop into the
gate. `--allow-partial` overrides it, only for a throwaway `--ledger` probe.

### Automated (installed 2026-11-12)

`TRIO-Shadow-Daily` (Windows Task Scheduler, Mon-Fri **15:20 IST**) runs
`run_shadow_daily.cmd`, which executes both commands above for the current IST
date and appends to `output/shadow_daily.log`. It only reads market data and
writes the ledger; it cannot place an order.

```powershell
Get-ScheduledTask -TaskName "TRIO-Shadow-Daily" | Get-ScheduledTaskInfo
Get-Content output\shadow_daily.log -Tail 40
```

Variant E is the per-leg rule, so its records carry `trades: [...]` instead of
a single `trade`; the report and `summarise` flatten both shapes.

To seed E against the historical futures series without touching A-D:

```powershell
python scripts\shadow_orb_vwap.py --backfill --variant E
```

Review:

```powershell
python scripts\shadow_orb_vwap.py --report
python scripts\audit_live_vwap.py --report
```

## Live-entry audit (read-only)

`scripts/audit_live_vwap.py` answers a different question from the shadow
sleeve: not *what would the corrected rules have traded*, but *what would they
have said about each trade the live book actually took*.

It reads `output/trade_log.jsonl` **and** the live runtime's own state file
(`%LOCALAPPDATA%\TRIO\options_intraday\live_<date>.json`), loads the same NIFTY
futures series the shadow uses, recomputes the opening range and the cumulative
true VWAP as of each entry, and labels it:

| class | meaning |
|---|---|
| `survives` | live entry is exactly the corrected first entry for the direction |
| `late_entry` | first live entry on the leg, but after the corrected signal (delay recorded) |
| `duplicate_entry` | repeat of an earlier live entry on the same leg |
| `second_setup` | price re-armed (closed back inside the range) and broke out again; the one-trade-per-session rule would skip it |
| `vwap_blocked` | range confirmed, close on the wrong side of true VWAP |
| `basis_mismatch` | futures range not confirmed, spot range was |
| `no_confirmation` | no two-close confirmation on either series |
| `outside_window` | before 10:00 or after 14:30 |
| `data_issue` | no futures session, or volume too sparse for a true VWAP |

The `basis` column in the report counts `spot_confirms=True, orb_confirms=False`
on **every** entry, including ones primarily classified as a repeat — the
primary class would otherwise hide it.

It never imports the runtime, never opens a broker connection, writes only to
its own ledger, and nothing in the live path imports it.

The live runtime keeps its own book: until 2026-11-12 it wrote closed legs only
to `live_<date>.json`, so the audit saw no live entries at all. The state reader
now maps each `closed[]` record to an audit entry. A closed leg carries
`entry_ts` (added to the runtime on 2026-11-12); for older state files the entry
time is recovered from the surviving `intents` record or, failing that, the
runtime's `ENTRY BUY` log line. If none of those exist the leg is **reported,
not guessed** — the exit time is never substituted for the entry time.

```powershell
python scripts\audit_live_vwap.py --date 2026-11-12          # trade log + state
python scripts\audit_live_vwap.py --date 2026-11-12 --no-state  # log only
```

### Result so far (NIFTY only, 5 sessions, 27 live entries)

BANKNIFTY entries are excluded: auditing them against NIFTY futures levels is
meaningless. 2026-11-11 is also no longer in the cumulative run — after the
date roll the broker's futures series ends 2026-11-09, so the 11-11 record is
kept from the single-day run and tabulated separately.

| date | live | surv | late | dup | set2 | vwap | basis | noConf | window | live P&L |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-03 | 2 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | +15,330 |
| 2026-11-04 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | +7,015 |
| 2026-11-06 | 14 | 0 | 1 | 13 | 0 | 0 | 0 | 0 | 0 | +6,111 |
| 2026-11-09 | 7 | 0 | 1 | 5 | 1 | 0 | 1 | 0 | 0 | −7,793 |
| 2026-11-12 | 2 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | −2,785 |
| **total** | **27** | **0** | **4** | **18** | **3** | **0** | **1** | **2** | **0** | **+17,879** |
| 2026-11-11* | 4 | 0 | 1 | 2 | 1 | 0 | 1 | 0 | 0 | +1,447 |

\* single-day run, before the futures series rolled off.

Three findings that matter for the promotion decision:

1. **78% of live entries (21/27) are repeats or re-arms of the same leg.**
   11-06 alone took 14 entries on one breakout. This is the defect the
   corrected one-entry rule addresses, and it is much larger than the VWAP
   issue.
2. **The true VWAP blocked 0 of 27 live entries** — the same result as the
   19-entry shadow backfill. Deleting the equal-weight fallback is therefore
   likely to be behaviour-neutral, which makes it a safe promotion item.
   Separately, 1 of the 27 entries confirmed on spot while futures stayed
   inside its range; on 11-11 that was the 14:09 entry, a duplicate *and* a
   basis mismatch that still made +1,442.
3. **The live book's first entry is always 5+ minutes late** (5/5 sessions),
   because it waits for its own confirmation after the corrected bar. That
   lateness is measurable but has not been costly by itself.

### The cap that matters is per leg, not per day

The repeat defect is not "too many trades" — it is "too many attempts on the
same leg". Capping attempts per leg (a new leg starts when price closes back
inside the opening range and breaks out again) on the 25-entry sample:

| rule | entries | net | worst day |
|---|---|---|---|
| one entry per leg, re-arm allowed | 7 | +20,725 | −1,532 |
| two per leg | 10 | +19,087 | −3,096 |
| three per leg | 12 | +15,978 | −6,126 |
| live, uncapped | 25 | +20,663 | −7,793 |

One entry per leg produced the same P&L as the uncapped book with **7 entries
instead of 25** and a worst day of −1,532 instead of −7,793. The cap costs
+6,198 on 11-06 (the trend day) and saves −4,546 on 11-09 (the loss day), which
nets to roughly nothing on P&L and a large reduction in tail risk.

This is in-sample and reuses the live entry timing, so it measures the *cap*,
not the corrected engine. Seven entries is far too few to promote; it is a
candidate to shadow, not a conclusion.

### What the audit does not prove

Duplicates are frequent, but on this sample they were **not net harmful**.
Comparing the live book with the corrected one-trade engine on the same days:

| date | live entries | live P&L | corrected entry | corrected P&L* |
|---|---|---|---|---|
| 2026-11-03 | 2 | +15,330 | 10:20 long → 15:10 | +2,566 |
| 2026-11-04 | 2 | +7,015 | no signal | 0 |
| 2026-11-06 | 14 | +6,111 | 10:00 long → 13:15 | +5,133 |
| 2026-11-09 | 7 | −7,793 | 10:40 short → 11:15 stop | −2,667 |
| **total** | **25** | **+20,663** | | **+5,032** |

\* corrected P&L is variant A/B points × ₹32.5 (delta 0.5 × lot 65), not a
premium replay — comparable only in direction, not to the rupee.

Three caveats that must survive into the promotion decision:

* **11-04 is the strongest counter-example.** The live book made +7,015 on two
  entries that the corrected rules reject outright (no two-close confirmation
  on futures or spot). The corrected engine's answer for that day is *do
  nothing*. Any promotion case has to explain why that day is not the norm.
* The one-trade cap is **expensive on strong trend days**. On 11-03 the live
  book compounded the move across two entries; the corrected engine took one
  and held, for +2,566 instead of +15,330.
* The corrected rules earned their keep on 11-09, the losing day: −2,667
  instead of −7,793. One day is not evidence either way.
* This is 4 comparable sessions, mostly trending. The duplicate-rate finding is
  strong; the profitability comparison is not.

So the honest position is: the audit proves the *repeat-entry* defect and the
*non-binding* VWAP, and it does **not** prove the corrected rules are more
profitable. That is what the 10 forward shadow sessions are for.

## Backfill baseline (2026-10-13 → 2026-11-11, 20 sessions)

Source is `backfill`: context only, **never** counted toward promotion.

| variant | trades | wins | win% | net pts | net ₹/lot | maxDD | PF |
|---|---|---|---|---|---|---|---|
| A | 19 | 11 | 57.9% | +769.9 | +25,020 | −252.3 | 2.32 |
| B | 19 | 11 | 57.9% | +769.9 | +25,020 | −252.3 | 2.32 |
| C | 12 | 6 | 50.0% | +76.4 | +2,483 | −410.2 | 1.16 |
| D | 11 | 6 | 54.5% | +158.4 | +5,150 | −328.2 | 1.39 |
| E | 22 | 12 | 54.5% | +763.7 | +24,820 | −328.2 | 2.02 |

**E is a containment rule, not an alpha source.** On corrected bars it trades
almost exactly like A (22 vs 19 trades, +763.7 vs +769.9 points) with a
*deeper* drawdown, because 2026-11-09 re-armed and E took the second leg for
another −82.05. Its value shows up only against the live book's behaviour: the
same "one entry per leg" rule applied to the 25 real live entries produced
+20,725 with 7 entries and a worst day of −1,532, versus +20,663 with 25
entries and −7,793. The two numbers cover different periods and different entry
timing, so they are not comparable — E's case rests on tail risk, not P&L.

### Read this before believing it

1. **VWAP blocked 0 of 19 entries.** B is identical to A. A genuine,
   volume-backed VWAP still never binds on a close-confirmed ORB — the two
   consecutive closes beyond the range already imply the VWAP side.
2. **The result is stop-sensitive.** Variant A, 2R target:

   | stop pts | win% | net pts | maxDD |
   |---|---|---|---|
   | 30 | 42.1% | +111.1 | −70.2 |
   | 40 | 36.8% | +41.1 | −132.3 |
   | 60 | 47.4% | +456.8 | −192.3 |
   | 80 | 57.9% | +769.8 | −252.3 |
   | 120 | 57.9% | +593.9 | −448.8 |
   | 150 | 68.4% | +955.0 | −185.8 |

   Non-monotonic. That is 19 trades and one regime, not an edge curve.
3. **Drawdown exceeds the promotion limit** at the live-equivalent 80-point
   stop (−252 pts vs a 150-pt gate). The edge, if it exists, is wider than the
   current risk budget allows on one lot.
4. **Futures P&L is optimistic for options.** Premium is modelled as
   `delta × points`; theta and IV decay are not, and they hurt long-premium
   intraday positions.

## Promotion gate (pre-registered)

A variant becomes eligible only when **all** of these hold on
`source="forward"` records:

| check | threshold |
|---|---|
| forward sessions | ≥ 10 |
| trades | ≥ 8 |
| net points | > 0 |
| max drawdown | ≤ 150 points |

Backfilled sessions never satisfy the gate. `E` was added before any forward
session was recorded, so it is judged on the same gate as A-D; its backfill
drawdown (−328.2) already fails the 150-point limit, which is a geometry
signal, not a reason to widen the gate.

## What to do on 2026-11-12

0. **Before 09:18, confirm only one session process can run.** The scheduled
   task `TRIO-Session-Trigger` is `Ready` and fires at 09:18 Mon-Fri,
   dispatching the cloud workflow's `run_high_frequency_index_session.py`
   (NIFTY+BANKNIFTY, 3 open, 4 trades, cutoff 15:15). It does not share the
   local `run_session_10s.bat` single-process guard, so both can trade the
   same account with different risk parameters:

   ```powershell
   Disable-ScheduledTask -TaskName 'TRIO-Session-Trigger'
   ```

1. Let the live `orb` sleeve run unchanged.
2. After 15:10, run:

   ```powershell
   python scripts\shadow_orb_vwap.py --date 2026-11-12
   python scripts\audit_live_vwap.py --date 2026-11-12
   ```

3. Record the A-E shadow trades next to the live entries for the same day.
4. Repeat for 10 sessions.

The shadow run decides the gate. The audit does not feed the gate; it
attributes each live entry and builds the evidence for a safe promotion.

Only if the gate passes should `src/session_vwap.py` be promoted into the live
path, `options_live_scan.py` lose its equal-weight fallback, and
`max_daily_entries` drop to 2. The audit's 0/25 VWAP-blocked result is the
supporting evidence that removing the fallback will not change live entries.

## Known limitations

* Only ~20 forward-testable sessions exist per futures contract, because
  expired series are dropped from the broker's instrument dump.
* **The audit ledger decays.** The futures series is fetched from a rolling
  ~30-day window. After the 2026-11-12 date roll the series ended 2026-11-09
  and 2026-11-11 fell out of the cumulative run. Persisting the fetched bars
  locally is the only way to keep an auditable history.
* One trade per session by design; the live sleeve currently allows 4+.
* The stop is a fixed point amount; NIFTY premium stop is a percentage, so the
  equivalent points move with premium.
* No partial exits and no profit-lock ladder in the shadow (full position,
  single target).
