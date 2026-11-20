# TRIO — weekday LIVE options runbook (Zerodha NFO)

Normal trading-day live session on Zerodha. This is the **same book** that runs
in paper (`config/options_intraday.yaml`): ORB + mean reversion, 2 positions,
Rs 18,000 premium cap, Rs 20,000 wallet floor. The only differences from paper
are the venue (`execution_class=live`, `PAPER_TRADING=false`) and an explicit
date lock.

---

## 1. Hard prerequisites (all must be true)

| # | Requirement | Check |
|---|---|---|
| 1 | **NFO segment active** on the Kite account | `--preflight` shows `NFO` in `kite segments` |
| 2 | **Available cash ≥ ₹20,000** | `--preflight` shows `available cash ... OK` |
| 3 | **Fresh Kite access token** (tokens expire daily) | `python scripts/kite_token_refresh.py` |
| 4 | **Flat start** — no NFO holdings, no working NFO orders | `--preflight` shows `NFO holdings 0`, `open NFO orders 0` |
| 5 | Market data + chain reachable | `--preflight` shows 5m candles and a Zerodha chain |

Notes:

- Enabling **BFO (BSE F&O) does not help**. Index options need **NFO**.
- Options are bought with **NRML**, not MIS. The runtime is flat by **15:10**
  (config `hard_flat`), so there is no MIS auto-square-off race.
- A missing token blocks even paper runs, because candles and the chain come
  from Kite.

If prerequisites 1 or 2 fail, stop. The runner will refuse, but check first.

---

## 2. Preflight (read-only, no runtime)

```powershell
python scripts/weekday_live.py --preflight --weekday-live 2026-11-09
```

- The date in `--weekday-live` **must equal today (IST)** or the run is refused.
- The preflight is read-only. `WEEKDAY LIVE BLOCKED: no NFO segment ...` is a
  hard stop, not a warning.
- `strategies` should read `['orb', 'mean_reversion']` and
  `scan underlyings ['NIFTY']` (parity with the paper launcher).

---

## 3. Dry run (still sends no orders)

```powershell
python scripts/weekday_live.py --dry-run --weekday-live 2026-11-09 --once
```

Expected:

```
execution class  : live
session window   : ... (entry 09:20-15:00, flat 15:10, close 15:30)
finalize: closed={} remaining=[] positions_read=True
```

Dry run skips the typed confirmation because the adapter refuses every order.

---

## 4. Live start

```powershell
python scripts/weekday_live.py --weekday-live 2026-11-09 --confirm WEEKDAY-LIVE --interval 10
```

Safety model:

- date lock must equal today;
- `--confirm WEEKDAY-LIVE` is required for real orders;
- NFO segment, wallet floor and flat-start checks run before the runtime exists;
- state file is per date: `%LOCALAPPDATA%\TRIO\options_intraday\live_<date>.json`;
- the loop stops itself at the hard flat (15:10) and finalizes flat.

Optional narrower universe (default is NIFTY only):

```powershell
python scripts/weekday_live.py --weekday-live 2026-11-09 --confirm WEEKDAY-LIVE --underlyings NIFTY,BANKNIFTY
```

## 5. Emergency flatten

```powershell
python scripts/weekday_live.py --flatten
```

Cancels working NFO orders and squares off every NFO holding, regardless of the
strategy's state file.

---

## 6. Current blockers

- Kite segments: `MF, BSE, NSE, BFO` — **NFO missing**.
- Available cash: below the Rs 20,000 floor.

Until both are fixed, weekday live is refused by design. MegaBull paper
(`python scripts/session.py`) is unaffected and needs neither.
