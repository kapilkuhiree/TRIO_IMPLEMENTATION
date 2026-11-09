# TRIO — Diwali Muhurat 2026 LIVE runbook (Sunday 8 Nov 2026)

Author: Kapil Kuhire <kapilkuhire89@gmail.com>

One-off NSE Muhurat session. **Everything here is date-locked to 2026-11-08**
and lives outside the normal paper path. Monday's paper session is unchanged.

---

## 1. Session facts (verified 2026-11-07)

| Item | Value |
|---|---|
| Date | Sunday **2026-11-08** |
| F&O window | **14:20 – 15:40 IST** |
| Entries | **14:25 – 15:05** |
| Hard flat | **15:15** |
| Runner stops | at 15:15 (flat), finalizes, exits |
| Watchdog backstop | **15:16** |
| Strategies | `mean_reversion` only (ORB disabled: no 09:15 open) |
| Product | **NRML** |
| Open positions | **1** |
| Max premium / position | **₹20,000** |
| Wallet floor | **₹40,000** available cash |
| Notional sizing capital | ₹3,00,000 (risk 1.5%, exposure 10%) |
| Scan underlyings | NIFTY, BANKNIFTY |
| NIFTY nearest expiry | `09-Nov-2026`, lot **65** |
| BANKNIFTY nearest expiry | `23-Nov-2026`, lot **30** |

State file (outside the repo):

```
%LOCALAPPDATA%\TRIO\options_intraday\muhurat_2026-11-08.json
```

Use the **global** `python` (the repo `.venv` is broken). Run everything from
the repo root:

```powershell
cd "C:\Users\Kapil Kuhire\Downloads\TRIO_IMPLEMENTATION-main"
```

---

## 2. Saturday (today) — preparation

- [ ] **Fund the Zerodha account to at least ₹40,000 available cash.**
      Preflight on 2026-11-07 reported `available cash 0.00` and the live
      runner refuses to start below the floor.
- [ ] Confirm the account has **no NFO holdings and no working NFO orders**
      (preflight proves this; `--flatten` clears them).
- [ ] Run the offline suite (expect **727 passed**):

      ```powershell
      python -m pytest -q --basetemp="$env:TEMP\opencode\pytest-full"
      ```

- [ ] Read `config/options_intraday_muhurat.yaml` once more. Do not edit
      `config/options_intraday.yaml` (normal paper) or the Monday strategy
      list.
- [ ] Decide whether Telegram alerts are wanted. The adapter warns
      `Telegram credentials not set` and continues; it is not required.

---

## 3. Sunday morning — fresh Kite session

Kite access tokens expire daily. **Friday's token will not work on Sunday.**

- [ ] Generate a fresh access token with the standard Kite flow:
      1. Open `https://kite.trade/connect/login?api_key=<your_api_key>&v=3`.
      2. Log in; copy the `request_token` from the redirect URL.
      3. Exchange it for an access token and update
         `ZERODHA_ACCESS_TOKEN` in `.env`.
- [ ] **Never paste the API key, secret, access token, or request token into
      chat, logs, screenshots, or this runbook.**
- [ ] Verify login + account with the read-only preflight:

      ```powershell
      python scripts/muhurat_live.py --preflight
      ```

      Expected: `credentials OK`, a masked `kite profile`, `NFO holdings 0`,
      `open NFO orders 0`, correct lot sizes, 5m candles, and a Zerodha chain.
      `available cash` must be **OK** (≥ ₹40,000).
      `live gate today: BLOCKED` is normal before Sunday.

---

## 4. Sunday 14:00 — start checks

- [ ] Open two PowerShell windows at the repo root.
- [ ] Window 1 — confirm the account is still flat:

      ```powershell
      python scripts/muhurat_live.py --preflight
      ```

- [ ] Optional dry run on the real date (sends no orders):

      ```powershell
      python scripts/muhurat_live.py --dry-run --once
      ```

      It should construct the runtime, show `execution class: live`,
      `scan underlyings: [NIFTY, BANKNIFTY]`, and finalize flat. In dry-run
      the broker refuses every order, so `closed={}` and `remaining=[]`.

---

## 5. Sunday 14:20–14:25 — tiny order test, then live

The exchange session opens at **14:20**. Do the probe only after the open.

- [ ] **Probe (real 1-lot, cancelled immediately).** It buys one lot of the
      CE nearest spot at **50% of last price**, confirms it rests, cancels it,
      and proves the account is unchanged:

      ```powershell
      python scripts/muhurat_live.py --probe-order
      ```

      Type `PROBE` when prompted. Expected: `status OPEN` (or `TRIGGER
      PENDING`), `after CANCELLED`, `holding 0`.
      If it ever fills, it flattens automatically and exits non-zero.
      Do **not** use `--yes` unless you are watching the order book.

- [ ] Start the watchdog in **window 2** (it sleeps until 15:16):

      ```powershell
      python scripts/muhurat_watchdog.py
      ```

      It refuses to act on any date other than 2026-11-08.

- [ ] Start the live runner in **window 1** at ~14:23:

      ```powershell
      python scripts/muhurat_live.py
      ```

      The runner sets its own environment:
      `TRIO_OPTIONS_LIVE=1`, `TRIO_BROKER_PROVIDER=zerodha`,
      `PAPER_TRADING=false`, Zerodha candles and chain. You do **not** need to
      export those variables in the shell.

      Startup must show:
      - `kite login VX9***` (masked)
      - cash ≥ 40,000 with no floor warning
      - `live requested : True`, `execution class : live`
      - `session window : pre` (until 14:25) then `open`
      - `positions : 0`

---

## 6. During the session (14:25–15:15)

- Entries only between **14:25 and 15:05**; one position at a time;
  premium ≤ ₹20,000; NRML.
- Stops: −20% premium; targets: +30%, with profit-lock rungs
  **10 / 15 / 20 / 25%**; after 13:30 the afternoon +10% target fires first.
- Daily loss cap: 1.5% of ₹3,00,000 = **₹4,500** (stops new entries).
- Max 3 entries; 5-minute re-entry cooldown.
- Let the runner manage exits. Do not place manual orders in Kite while it
  owns a position.
- If the runner prints `UNRESOLVED ... flattening and stopping`, or three
  consecutive iteration failures, it flattens and exits with code 2. Verify
  flat in Kite and with `--flatten`.

Emergency stop at any time: **Ctrl+C in window 1** (it finalizes and
flattens), then run `--flatten` to prove flat.

---

## 7. Shutdown (15:15–15:40)

- [ ] 15:15 — the runner hard-flattens, finalizes, and exits.

      Expected: `finalize: closed={...} remaining=[] positions_read=True`

- [ ] 15:16 — the watchdog independently cancels working NFO orders and
      squares off every NFO holding. Expected `account is flat`.
- [ ] After 15:40, prove flat one final time:

      ```powershell
      python scripts/muhurat_live.py --flatten
      ```

      Expected `remaining []`, `positions read True`, exit code 0.
- [ ] Check the Kite app: **Positions = 0**, **Orders = no working NFO
      orders**.
- [ ] Delete the one-off state file once you are satisfied:

      ```powershell
      Remove-Item "$env:LOCALAPPDATA\TRIO\options_intraday\muhurat_2026-11-08.json"
      ```

      Do this only after the account is flat. A stale state file that still
      lists a closed position makes the next start refuse with
      `owned remote position missing`.

---

## 8. If something goes wrong

| Symptom | Action |
|---|---|
| `available cash ... below the Rs 40,000 floor` | Fund the account; re-run preflight. |
| `refusing to start: NFO holdings already present` | `python scripts/muhurat_live.py --flatten`, then restart. |
| `refusing to start: NFO orders are already working` | Same: `--flatten`, then restart. |
| `live gate ... BLOCKED` | Wrong date, `.env` token, or missing `TRIO_OPTIONS_LIVE` (the runner sets it). Re-check preflight. |
| Token/login failure | Regenerate the Kite access token and update `.env`. |
| Position will not close | Watchdog at 15:16, then `--flatten`; the adapter escalates a stuck exit to a protected market order. Last resort: close in the Kite app. |
| Runner process killed | Run `--flatten`; delete the state file only after Kite shows flat. |
| `owned remote position missing` | The local state is stale. Confirm flat in Kite, delete the state file, restart. |

**Do not** leave `TRIO_OPTIONS_LIVE=1` exported in a shell used for other
TRIO commands. The runner sets it only in its own process.

---

## 9. What was added (all opt-in, paper untouched)

- `config/options_intraday_muhurat.yaml` — live overlay for this one date.
- `config/market.yaml` — `special_sessions."2026-11-08"` (14:20/14:25/15:05/15:15/15:40).
- `src/market_clock.py` — per-date special-session support (weekday behaviour unchanged).
- `src/options_intraday.py` — date-locked live gate; per-underlying lot validation.
- `src/options_intraday_runtime.py` — live/paper-aware broker admission.
- `src/broker/zerodha_options.py` — NFO LIMIT/MARKET, NRML/MIS, partial-fill
  cancel+unwind, NFO-scoped flatten, verified-flat reporting.
- `src/options_chain_zerodha.py` — optional nearest-expiry and strike-band
  limits (defaults preserve the old full chain).
- `scripts/muhurat_live.py` — preflight / probe / dry-run / scan / live loop /
  flatten.
- `scripts/muhurat_watchdog.py` — independent 15:16 hard-flat backstop.
- Tests: special-session clock, live gate, per-underlying lots, Zerodha
  adapter, live wiring. Full suite: **727 passed**.
