"""TRIO - Diwali Muhurat 2026 one-off LIVE session runner.

Author: Kapil Kuhire <kapilkuhire89@gmail.com>

This launcher is deliberately separate from the normal paper session.  It:

  * loads ``.env`` (Zerodha credentials) without printing any secret;
  * forces the Zerodha data + chain providers (yfinance has no Sunday feed);
  * overlays ``config/options_intraday_muhurat.yaml`` (mean-reversion only,
    NRML, one position, Rs 20,000 premium cap, Rs 40,000 wallet floor);
  * refuses to construct the runtime unless the date-locked live gate passes;
  * runs a 10-second manage/scan loop and is flat by 15:15 IST.

Safety modes
------------
``--preflight``   read-only account/contract/chain/data checks, no runtime.
``--flatten``     cancel every open order and flatten every NFO holding.
``--probe-order`` place one far-away 1-lot LIMIT, confirm it is open, cancel
                  it, and prove the account is unchanged.  This is the only
                  path that sends an order outside the strategy runtime.
``--dry-run``     the full live loop with a broker that refuses to send
                  orders.  Safe on the session date.
``--scan``        one read-only discovery pass (no runtime, no orders) using
                  ``--as-of`` so it can be exercised on a weekday.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_DATE = "2026-11-08"
CONFIG_PATH = ROOT / "config" / "options_intraday_muhurat.yaml"
NORMAL_CONFIG_PATH = ROOT / "config" / "options_intraday.yaml"


def _default_state_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / ".trio"
    return root / "TRIO" / "options_intraday" / "muhurat_2026-11-08.json"


def _load_env(env_file: Optional[str]) -> Path:
    candidates = [Path(env_file).expanduser()] if env_file else [
        ROOT / ".env",
        ROOT.parent / ".env",
    ]
    for path in candidates:
        if path.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(path)
            except ImportError:
                _load_env_fallback(path)
            return path
    return candidates[0]


def _load_env_fallback(path: Path) -> None:
    """Minimal .env reader; never logs values."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


def _force_market_environment() -> None:
    os.environ["TRADING_MODE"] = "options_intraday"
    os.environ["TRIO_MARKET_DATA_PROVIDER"] = "zerodha"
    os.environ["TRIO_OPTIONS_CHAIN_PROVIDER"] = "zerodha"


def _force_live_environment() -> None:
    os.environ["TRIO_OPTIONS_LIVE"] = "1"
    os.environ["TRIO_BROKER_PROVIDER"] = "zerodha"
    os.environ["PAPER_TRADING"] = "false"


def _force_paper_environment() -> None:
    """MegaBull paper: no live gate, but still a remote broker."""
    os.environ["TRIO_OPTIONS_LIVE"] = "0"
    os.environ["TRIO_BROKER_PROVIDER"] = "megabull"
    os.environ["PAPER_TRADING"] = "true"
    os.environ["TRIO_REMOTE_REQUIRED"] = "true"


def _muhurat_overlay() -> Dict[str, Any]:
    import yaml
    if not CONFIG_PATH.exists():
        raise SystemExit(f"missing overlay: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    mode = loaded.get("options_intraday", loaded)
    if not isinstance(mode, dict):
        raise SystemExit("muhurat overlay is malformed")
    return deepcopy(mode)


def _normal_overlay() -> Dict[str, Any]:
    """Normal weekday options overlay (the validated ORB + MR book)."""
    import yaml
    if not NORMAL_CONFIG_PATH.exists():
        raise SystemExit(f"missing overlay: {NORMAL_CONFIG_PATH}")
    with NORMAL_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    mode = loaded.get("options_intraday", loaded)
    if not isinstance(mode, dict):
        raise SystemExit("options_intraday overlay is malformed")
    return deepcopy(mode)


def _effective_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Base config + the selected mode section (no global mutation)."""
    from src.utils import load_config

    cfg = load_config()
    weekday_live = getattr(args, "weekday_live", None)
    mode = _normal_overlay() if weekday_live else _muhurat_overlay()

    # Restrict the option chain to the two nearest expiries.  A full NFO chain
    # is thousands of quotes; two expiries keeps a 10-second scan inside
    # Kite's quote rate limit while still covering the contract the planner
    # can select.
    def zerodha_chain(symbol: str, use_cache: bool = False) -> Any:
        from src.options_chain_zerodha import fetch_zerodha_chain
        # The planner always selects the nearest future expiry and a
        # near-the-money strike.  Quoting one expiry inside a +/-10% strike
        # band keeps a 10-second live loop inside Kite's quote rate limit.
        return fetch_zerodha_chain(symbol, raise_errors=True, max_expiries=1,
                                   max_strike_distance_pct=0.10)

    mode["chain_provider"] = zerodha_chain
    if weekday_live:
        # Explicit, date-locked weekday live session on Zerodha.  The normal
        # overlay keeps the validated ORB + mean-reversion book, two slots and
        # the Rs 18k premium cap; only the venue and the date lock change.
        mode["execution_class"] = "live"
        mode["PAPER_TRADING"] = False
        mode["live_session_date"] = str(weekday_live)
    elif getattr(args, "broker", "zerodha") == "megabull":
        # MegaBull is remote paper: the live date gate does not apply, but the
        # runtime still validates remote_paper + PAPER_TRADING=true.
        mode["execution_class"] = "remote_paper"
        mode["PAPER_TRADING"] = True
    if args.capital is not None:
        mode["capital"] = float(args.capital)
    if args.state:
        mode["state_path"] = str(Path(args.state).expanduser())
    if args.underlyings:
        selected = [item.strip().upper() for item in args.underlyings.split(",")
                    if item.strip()]
        if selected:
            mode["underlyings"] = selected
            mode["scan_underlyings"] = selected
            os.environ["TRIO_OPTIONS_SCAN_UNDERLYINGS"] = ",".join(selected)
    if args.live_date:
        mode["live_session_date"] = str(args.live_date)
    if weekday_live and not mode.get("scan_underlyings"):
        # Parity with the normal paper launcher (scripts/session.py): NIFTY
        # only unless the operator names a wider list explicitly.  Kept in the
        # config (not the environment) so importing/building configs cannot
        # leak scan scope into another process or test.
        selected = list(mode.get("underlyings") or ["NIFTY"])
        mode["scan_underlyings"] = ["NIFTY"] if "NIFTY" in selected else selected[:1]
    cfg["options_intraday"] = mode
    return cfg


def _state_path(args: argparse.Namespace) -> Path:
    if args.state:
        return Path(args.state).expanduser()
    if getattr(args, "weekday_live", None):
        return _default_state_path().parent / f"live_{args.weekday_live}.json"
    return _default_state_path()


WEEKDAY_CONFIRM = "WEEKDAY-LIVE"


def _nfo_segments(market_data: Any) -> List[str]:
    profile = market_data.profile() or {}
    return sorted({str(item).upper() for item in (profile.get("exchanges") or [])})


def _weekday_guards(args: argparse.Namespace, market_data: Any) -> None:
    """Hard preconditions for an explicit weekday live session."""
    date_text = str(args.weekday_live)
    try:
        declared = datetime.fromisoformat(date_text).date()
    except ValueError as exc:
        raise SystemExit(
            f"--weekday-live needs an ISO date, got {date_text!r}") from exc
    today = datetime.now(IST).date()
    if declared != today:
        raise SystemExit(
            f"weekday live is date-locked to {declared}; today is {today}")
    if args.dry_run:
        return
    if str(args.confirm or "").strip().upper() != WEEKDAY_CONFIRM:
        raise SystemExit(
            "refusing to send real NFO orders without "
            f"--confirm {WEEKDAY_CONFIRM}")
    exchanges = set(_nfo_segments(market_data))
    if "NFO" not in exchanges:
        raise SystemExit(
            "refusing to start: Kite account has no NFO segment "
            f"({', '.join(exchanges) or '-'}); enable F&O on NFO, not BFO")


class _MegaBullRunnerAdapter:
    """Bridge MegaBullOptionsBroker to the runner's small broker surface."""

    provider_name = "megabull_options"

    def __init__(self, mega: Any, options: Any):
        self._inner = options
        self.mega = mega
        self.strict_options_only = True
        # Keep live references: close_spread mutates these dicts in place.
        self.positions = options.positions
        self.spreads = options.spreads

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def refresh_positions(self) -> bool:
        try:
            self.mega.refresh_positions()
        except Exception:
            pass
        return True

    def option_instruments(self) -> List[Dict[str, Any]]:
        return self.mega.option_instruments()

    def flatten_all(self, reason: str = "manual-flatten") -> Dict[str, Any]:
        closed: Dict[str, Any] = {}
        failed: List[str] = []
        for spread_id in list(self.spreads):
            position = self.positions.get(spread_id)
            mid = float(getattr(position, "current_price", 0.0) or 0.0) \
                if position is not None else 0.0
            try:
                record = self._inner.close_spread(spread_id, mid, reason)
            except Exception as exc:
                print(f"megabull close {spread_id} raised: {exc}")
                record = {}
            if record:
                closed[spread_id] = True
            else:
                failed.append(spread_id)
        return {
            "cancelled": [],
            "closed": closed,
            "remaining": list(self.positions),
            "positions_ok": True,
            "failed": failed,
        }


def _broker(args: argparse.Namespace, restrict: bool = True):
    from src.zerodha_market_data import ZerodhaMarketData

    selected: List[str] = []
    if restrict:
        selected = [item.strip().upper()
                    for item in (args.underlyings or "").split(",")
                    if item.strip()]
        if not selected:
            selected = [str(item).upper() for item in
                        (_muhurat_overlay().get("underlyings") or ["NIFTY", "BANKNIFTY"])]
    market_data = ZerodhaMarketData.from_env()

    if getattr(args, "broker", "zerodha") == "megabull":
        from src.broker.megabull import MegaBullBroker
        from src.broker.megabull_options import MegaBullOptionsBroker

        mega = MegaBullBroker(
            api_key=os.environ.get("MEGABULL_API_KEY", ""),
            initial_capital=500000.0,
        )
        options = MegaBullOptionsBroker(mega, lot_size=65,
                                        initial_capital=500000.0)
        return market_data, _MegaBullRunnerAdapter(mega, options)

    from src.broker.zerodha_options import ZerodhaOptionsBroker

    broker = ZerodhaOptionsBroker(
        market_data,
        product="NRML",
        order_timeout_seconds=8.0,
        exit_timeout_seconds=5.0,
        dry_run=bool(args.dry_run),
        tag="TRIO-MUHURAT",
        underlyings=selected,
    )
    return market_data, broker


def _mask(value: str) -> str:
    text = str(value or "")
    return (text[:3] + "***") if text else "(missing)"


def _print_header(title: str) -> None:
    print("=" * 68)
    print(title)
    print("=" * 68)


def _expiry_key(text: str) -> datetime:
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.max


def _open_nfo_orders(broker: Any) -> List[Dict[str, Any]]:
    try:
        rows = broker.kite.orders() or []
    except Exception as exc:
        raise SystemExit(f"cannot read order book: {exc}") from exc
    open_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("exchange") or "").upper() != "NFO":
            continue
        status = str(row.get("status") or "").upper()
        if status in {"OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED",
                      "VALIDATION PENDING", "MODIFY PENDING", "AMO REQ RECEIVED"}:
            open_rows.append(row)
    return open_rows


def _nearest_expiries(rows: List[Dict[str, Any]], underlying: str) -> List[str]:
    values = {str(row.get("expiry") or "") for row in rows
              if str(row.get("name") or "").upper() == underlying}
    return sorted((value for value in values if value), key=_expiry_key)


def _preflight(args: argparse.Namespace) -> int:
    from src.data_fetcher import fetch_market_data
    from src.market_clock import market_clock_from_config
    from src.options_chain_zerodha import fetch_zerodha_chain

    _force_market_environment()
    cfg = _effective_config(args)
    mode = cfg["options_intraday"]
    clock = market_clock_from_config(cfg)
    _print_header("TRIO WEEKDAY LIVE PREFLIGHT (read-only)"
                  if getattr(args, "weekday_live", None)
                  else "TRIO MUHURAT LIVE PREFLIGHT (read-only)")

    print(f"session date      : {mode.get('live_session_date')}")
    print(f"today (IST)       : {datetime.now(IST).date().isoformat()}")
    if getattr(args, "weekday_live", None):
        declared = str(args.weekday_live)
        today = datetime.now(IST).date().isoformat()
        print(f"weekday live date : {declared} "
              f"({'OK' if declared == today else 'NOT TODAY'})")
        if declared != today:
            print("WEEKDAY LIVE BLOCKED: the date lock must equal today.")
            return 2
    print(f"market clock      : valid={clock.valid} error={clock.error or '-'}")
    print(f"special session   : {clock.special_sessions.get(SESSION_DATE, '-')}")
    print(f"scan underlyings  : {mode.get('scan_underlyings') or mode.get('underlyings')}")
    print(f"strategies        : {mode.get('strategies')}")
    if getattr(args, "broker", "zerodha") == "megabull":
        print("broker            : MegaBull paper (virtual)")
        print("product           : MKT (MegaBull paper)")
    else:
        print("broker            : Zerodha live (NFO)")
        print("product           : NRML (adapter)")

    required = ["ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_ACCESS_TOKEN"]
    if getattr(args, "broker", "zerodha") == "megabull":
        required.append("MEGABULL_API_KEY")
    missing = [key for key in required if not os.environ.get(key)]
    print(f"credentials       : {'OK' if not missing else 'MISSING ' + ','.join(missing)}")
    if missing:
        return 2

    try:
        from src.options_intraday import validate_options_intraday_risk_config
        ok, reason = validate_options_intraday_risk_config(
            mode, live=getattr(args, "broker", "zerodha") != "megabull")
        print(f"risk config       : {'OK' if ok else 'INVALID: ' + reason}")
        if not ok:
            return 2
    except Exception as exc:
        print(f"risk config       : FAILED: {exc}")
        return 2

    try:
        market_data, broker = _broker(args)
        profile = market_data.profile()
        print(f"kite profile      : {_mask(str(profile.get('user_id') or ''))}")
        segments = profile.get("exchanges") or []
        print(f"kite segments     : {', '.join(segments) if segments else '-'} "
              f"(NFO required for F&O options)")
        if getattr(args, "weekday_live", None) and \
                "NFO" not in {str(item).upper() for item in segments}:
            print(f"WEEKDAY LIVE BLOCKED: no NFO segment in {segments}; "
                  "enable F&O on NFO (not BFO).")
            return 2
    except Exception as exc:
        print(f"kite login        : FAILED: {exc}")
        return 2

    try:
        balance = broker.get_balance()
        floor = float(mode.get("min_wallet") or 0.0)
        available = float(balance.get("available") or 0.0)
        if getattr(args, "broker", "zerodha") == "megabull":
            print(f"megabull cash     : {available:,.2f} (virtual paper)")
        else:
            funded = available >= floor
            print(f"available cash    : {available:,.2f} (floor {floor:,.2f}) "
                  f"{'OK' if funded else 'BELOW FLOOR'}")
            if not funded:
                print("  WARNING: fund the account before the session; the live "
                      "runner will refuse to start below the floor.")
        if not broker.refresh_positions():
            print("account check     : FAILED: positions could not be read "
                  "(cannot prove the account is flat)")
            return 2
        holdings = sorted(broker.positions)
        print(f"NFO holdings      : {len(holdings)}"
              + (f" {holdings}" if holdings else " (flat)"))
        if getattr(args, "broker", "zerodha") == "megabull":
            open_orders = []
            print("open NFO orders   : skipped (MegaBull paper)")
        else:
            open_orders = _open_nfo_orders(broker)
            print(f"open NFO orders   : {len(open_orders)}")
        if holdings or open_orders:
            print("PREFLIGHT BLOCKED: start flat with no working orders "
                  "(run --flatten to cancel and square off).")
            return 2
    except SystemExit:
        raise
    except Exception as exc:
        print(f"account check     : FAILED: {exc}")
        return 2

    specs = mode.get("contract_specs") or {}
    underlyings = [str(item).upper() for item in
                   (mode.get("scan_underlyings") or ["NIFTY", "BANKNIFTY"])]
    try:
        from src.options_live_scan import listing_contracts
        contracts = listing_contracts(broker.option_instruments(), underlyings)
    except Exception as exc:
        print(f"\noption metadata   : FAILED: {exc}")
        return 2
    for underlying in underlyings:
        own = {key: contract for key, contract in contracts.items()
               if key[0] == underlying}
        expiries = sorted({key[1] for key in own}, key=lambda value: value)
        print(f"\n[{underlying}] option rows={len(own)} "
              f"expiries={len(expiries)}")
        if not expiries:
            print("  FAILED: no listed options")
            return 2
        nearest = expiries[0].isoformat()
        expiry_rows = [contract for key, contract in own.items()
                       if key[1] == expiries[0]]
        lots = sorted({int(row.get("lot_size") or 0) for row in expiry_rows})
        expected = int((specs.get(underlying) or {}).get("lot_size") or 0)
        print(f"  nearest expiry : {nearest}")
        print(f"  lot sizes      : {lots} (config {expected or '-'})")
        if expected and lots != [expected]:
            print("  FAILED: configured lot size does not match the live listing")
            return 2
        strikes = sorted({float(key[2]) for key in own if key[1] == expiries[0]})
        print(f"  strikes        : {len(strikes)}"
              + (f" ({strikes[0]:.0f}..{strikes[-1]:.0f})" if strikes else ""))

        market_symbol = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}.get(
            underlying, underlying)
        try:
            frame = fetch_market_data(market_symbol, "5m", period="5d")
            frame = getattr(frame, "ohlcv", frame)
            index = getattr(frame, "index", None)
            latest = index[-1] if index is not None and len(index) else "?"
            close = float(frame["Close"].iloc[-1]) if len(frame) else 0.0
            print(f"  5m candles     : rows={len(frame)} last={latest} close={close:.2f}")
            if len(frame) == 0:
                return 2
        except Exception as exc:
            print(f"  5m candles     : FAILED: {exc}")
            return 2

        try:
            chain = fetch_zerodha_chain(underlying, raise_errors=True,
                                        max_expiries=2)
            expiries_out = chain.get("expiries") or []
            newest = None
            for row in (chain.get("by_expiry", {}) or {}).get(
                    expiries_out[0] if expiries_out else "", []) or []:
                for kind in ("CE", "PE"):
                    stamp = (row.get(kind) or {}).get("quote_timestamp")
                    if stamp and (newest is None or stamp > newest):
                        newest = stamp
            print(f"  zerodha chain  : spot={float(chain.get('underlying') or 0):.2f} "
                  f"expiries={expiries_out} quote={newest or '-'}")
        except Exception as exc:
            print(f"  zerodha chain  : FAILED: {exc}")
            return 2

    try:
        from src.options_intraday import validate_options_mode_environment
        saved = dict(os.environ)
        paper = getattr(args, "broker", "zerodha") == "megabull"
        if paper:
            os.environ["TRIO_OPTIONS_LIVE"] = "0"
            os.environ["TRIO_BROKER_PROVIDER"] = "megabull"
            os.environ["PAPER_TRADING"] = "true"
            os.environ["TRIO_REMOTE_REQUIRED"] = "true"
        else:
            os.environ["TRIO_OPTIONS_LIVE"] = "1"
            os.environ["TRIO_BROKER_PROVIDER"] = "zerodha"
            os.environ["PAPER_TRADING"] = "false"
        try:
            ok, reason = validate_options_mode_environment(cfg)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        label = "paper gate today  " if paper else "live gate today   "
        print(f"\n{label}: {'PASS' if ok else 'BLOCKED: ' + reason}")
    except Exception as exc:
        print(f"\nlive gate today   : BLOCKED: {exc}")
    return 0


def _scan(args: argparse.Namespace) -> int:
    """One read-only discovery pass; never constructs the runtime."""
    from src.options_live_scan import build_live_options_scan

    _force_market_environment()
    cfg = _effective_config(args)
    # This pass is read-only, so it must not need the live date gate.  The
    # runtime's paper validation is only a schema check here.
    cfg["options_intraday"]["execution_class"] = "remote_paper"
    cfg["options_intraday"]["PAPER_TRADING"] = True
    os.environ.pop("TRIO_OPTIONS_LIVE", None)
    now = None
    if args.as_of:
        now = datetime.fromisoformat(args.as_of)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)

    market_data, broker = _broker(args)
    result = build_live_options_scan(
        cfg, now=now, instrument_rows=broker.option_instruments())
    diagnostics = result.get("diagnostics") or {}
    _print_header("TRIO MUHURAT READ-ONLY SCAN")
    print({key: diagnostics.get(key, 0) for key in (
        "requested", "data_ok", "no_signal", "not_listed",
        "contract_rejection", "fetch_errors", "selected")})
    for rejection in diagnostics.get("rejections", []) or []:
        print(f"  reject {rejection.get('underlying')}: "
              f"{rejection.get('stage')} -> {rejection.get('reason')}")
    for snapshot in result.get("snapshots", []) or []:
        for candidate in snapshot.get("option_candidates", []) or []:
            legs = (candidate.get("option_legs") or {}).get("legs") or [{}]
            leg = legs[0]
            print(f"  candidate {candidate.get('option_underlying')} "
                  f"{leg.get('kind')} {leg.get('strike')} "
                  f"ask={leg.get('ask')} strategy={candidate.get('strategy')} "
                  f"rank={candidate.get('rank')}")
    return 0


def _flatten(args: argparse.Namespace) -> int:
    _force_market_environment()
    if args.dry_run:
        raise SystemExit("--flatten cannot be combined with --dry-run")
    # Unrestricted instrument metadata: a stray NFO holding outside the
    # NIFTY/BANKNIFTY universe must still be closable.
    market_data, broker = _broker(args, restrict=False)
    _print_header("TRIO MUHURAT FLATTEN")
    result = broker.flatten_all(reason="manual-muhurat-flatten")
    print(f"cancelled orders : {result.get('cancelled')}")
    print(f"closed symbols   : {result.get('closed')}")
    print(f"remaining        : {result.get('remaining')}")
    print(f"positions read   : {result.get('positions_ok')}")
    return 0 if not result.get("remaining") and result.get("positions_ok") else 2


def _underlying_spot(market_data: Any, underlying: str) -> float:
    spot_symbol = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}.get(
        underlying, underlying)
    try:
        payload = market_data.quote("NSE", [spot_symbol]) or {}
        return float((payload.get(f"NSE:{spot_symbol}") or {}).get("last_price") or 0.0)
    except Exception:
        return 0.0


def _probe_order(args: argparse.Namespace) -> int:
    """Place one far-from-market 1-lot LIMIT, prove it is open, then cancel."""
    if getattr(args, "broker", "zerodha") == "megabull":
        raise SystemExit("--probe-order is Zerodha-only; use --once for MegaBull")
    _force_market_environment()
    market_data, broker = _broker(args)
    if args.dry_run:
        raise SystemExit("--probe-order cannot be combined with --dry-run")
    underlying = (args.underlyings or "NIFTY").split(",")[0].strip().upper()
    rows = [row for row in broker.option_instruments()
            if str(row.get("name")).upper() == underlying
            and str(row.get("instrument_type")) == "CE"]
    expiries = sorted({str(row.get("expiry")) for row in rows}, key=_expiry_key)
    if not expiries:
        raise SystemExit(f"no CE listing for {underlying}")
    nearest = expiries[0]
    candidates = [row for row in rows if str(row.get("expiry")) == nearest]
    if not candidates:
        raise SystemExit("no candidate contract")
    # Prefer the strike nearest spot; the old lowest-strike choice picked a
    # deep ITM contract whose "half price" limit is not a realistic probe.
    spot = _underlying_spot(market_data, underlying)
    if spot > 0:
        spot_row = min(candidates,
                       key=lambda row: abs(float(row.get("strike") or 0.0) - spot))
    else:
        strike_values = sorted(float(row.get("strike") or 0.0) for row in candidates)
        middle = strike_values[len(strike_values) // 2]
        spot_row = min(candidates,
                       key=lambda row: abs(float(row.get("strike") or 0.0) - middle))
    lot = int(spot_row.get("lot_size") or 0)
    symbol = spot_row["tradingsymbol"]
    quote = market_data.option_quote(spot_row) or {}
    last = float(quote.get("last_price") or 0.0)
    if last <= 0 or lot <= 0:
        raise SystemExit("cannot price the probe contract")
    price = max(0.05, round((last * 0.5) / 0.05) * 0.05)
    if price >= last:
        raise SystemExit(f"probe limit {price:.2f} is not below the last price "
                         f"{last:.2f}; refusing to risk a fill")

    print(f"probe  : BUY {symbol} 1 lot ({lot}) LIMIT {price:.2f} "
          f"(spot {spot:.2f}, option last {last:.2f}) -- should rest far "
          "below market")
    if not args.yes:
        answer = input("Type PROBE to send this real 1-lot order: ").strip()
        if answer != "PROBE":
            print("aborted")
            return 1
    order_id = broker._place(side="BUY", symbol=symbol, quantity=lot,
                             order_type="LIMIT", price=price)
    print(f"order  : {order_id}")
    time.sleep(2.0)
    state = broker._order_row(order_id)
    status = str(state.get("status") or "").upper()
    print(f"status : {status}")
    broker._cancel_quiet(order_id)
    time.sleep(1.0)
    final = broker._order_row(order_id)
    print(f"after  : {str(final.get('status') or '').upper()}")
    if not broker.refresh_positions():
        print("holding: UNVERIFIED (positions endpoint failed)")
        return 2
    net = broker._symbol_quantity(symbol)
    print(f"holding: {net}")
    if net:
        print("unexpected fill: flattening")
        broker._flatten_symbol(symbol, "probe-unwind")
        if not broker.refresh_positions():
            return 2
        net = broker._symbol_quantity(symbol)
    return 0 if net == 0 else 2


class OptionsOnlyTrader:
    """Minimal trader shim for direct OptionsIntradayRuntime construction."""

    def __init__(self, cfg: Dict[str, Any], broker: Any, state_path: Path,
                 execution_class: str = "live"):
        self.cfg = cfg
        self.broker = broker
        self.opt_broker = broker
        self.trading_mode = "options_intraday"
        self.execution_class = execution_class
        self.options_only = True
        self.options_intraday_state_path = str(state_path)

    def ensure_option_broker(self, cfg: Dict[str, Any]) -> Any:
        return self.opt_broker

    def get_completed_bars(self, underlying: str, now: Any = None) -> List[Any]:
        return []


def _finalize(runtime: Any, broker: Any, label: str) -> int:
    try:
        runtime.finalize()
    except Exception as exc:
        print(f"{label}: runtime finalize raised: {exc}")
    try:
        result = broker.flatten_all(reason="muhurat-finalize")
    except Exception as exc:
        print(f"{label}: broker flatten raised: {exc}")
        return 2
    remaining = result.get("remaining") or []
    verified = result.get("positions_ok") is not False
    print(f"{label}: closed={result.get('closed')} remaining={remaining} "
          f"positions_read={result.get('positions_ok')}")
    return 0 if not remaining and verified else 2


def _run(args: argparse.Namespace) -> int:
    from src.options_intraday import options_live_requested
    from src.options_intraday_runtime import (
        OptionsIntradayPolicyError,
        OptionsIntradayRuntime,
        OptionsIntradayUnresolved,
    )

    _force_market_environment()
    if getattr(args, "broker", "zerodha") == "megabull":
        _force_paper_environment()
    else:
        _force_live_environment()
    if getattr(args, "weekday_live", None):
        # Match scripts/session.py so the weekday live book is the same one
        # that has been running in paper (capital, target, cadence, chain).
        os.environ.setdefault("TRIO_OPTIONS_CAPITAL", "1000000")
        os.environ.setdefault("TRIO_OPTIONS_TARGET_FRACTION", "0.25")
        os.environ.setdefault("TRIO_OPTIONS_SCAN_INTERVAL", "10")
        os.environ["TRIO_OPTIONS_CHAIN_PROVIDER"] = "auto"
    cfg = _effective_config(args)
    if args.live_date and not args.dry_run:
        raise SystemExit("--live-date is only available with --dry-run/--scan")

    state_path = _state_path(args)
    if args.state:
        os.environ["TRIO_OPTIONS_INTRADAY_STATE_PATH"] = str(state_path)
    market_data, broker = _broker(args)
    if getattr(args, "weekday_live", None):
        _weekday_guards(args, market_data)

    # Hard preconditions before the runtime is allowed to exist.
    profile = market_data.profile()
    print(f"kite login       : {_mask(str(profile.get('user_id') or ''))}")
    balance = broker.get_balance()
    floor = float(cfg["options_intraday"].get("min_wallet") or 0.0)
    available = float(balance.get("available") or 0.0)
    if getattr(args, "broker", "zerodha") == "megabull":
        print(f"megabull cash    : {available:,.2f} (virtual paper)")
    elif available < floor:
        if args.dry_run:
            print(f"WARNING: available cash {available:,.2f} is below the "
                  f"Rs {floor:,.0f} floor; dry-run continues, entries will "
                  "be blocked by the wallet gate")
        else:
            raise SystemExit(
                f"available cash {available:,.2f} is below the "
                f"Rs {floor:,.0f} wallet floor")
    if not broker.refresh_positions():
        raise SystemExit("refusing to start: positions could not be read "
                         "(cannot prove the account is flat)")
    if broker.positions:
        raise SystemExit(
            f"refusing to start: holdings already present {sorted(broker.positions)}"
            " (run --flatten)")
    if getattr(args, "broker", "zerodha") != "megabull" and _open_nfo_orders(broker):
        raise SystemExit("refusing to start: NFO orders are already working "
                         "(run --flatten)")

    execution_class = ("remote_paper"
                       if getattr(args, "broker", "zerodha") == "megabull"
                       else "live")
    trader = OptionsOnlyTrader(cfg, broker, state_path,
                               execution_class=execution_class)
    try:
        runtime = OptionsIntradayRuntime(trader, cfg)
    except OptionsIntradayPolicyError as exc:
        raise SystemExit(f"live runtime refused: {exc}") from exc
    runtime.management_interval_seconds = max(5.0, float(args.interval))
    runtime.options_intraday_state_path = str(state_path)
    try:
        os.environ["TRIO_OPTIONS_INTRADAY_STATE_PATH"] = str(state_path)
    except Exception:
        pass

    print(f"live requested   : {options_live_requested()}")
    print(f"execution class  : {runtime.execution_class}")
    print(f"state file       : {state_path}")
    print(f"dry run          : {bool(args.dry_run)}")
    print(f"scan underlyings : {runtime.mode_cfg.get('scan_underlyings')}")
    print(f"positions        : {len(runtime._state.get('positions') or {})}")

    clock = runtime._clock()
    print(f"session window   : {clock.session_state(datetime.now(IST))} "
          f"(entry {clock.entry_start}-{clock.entry_cutoff}, "
          f"flat {clock.hard_flat}, close {clock.close})")

    def iterate(now: datetime) -> None:
        if not clock.valid:
            raise SystemExit(f"invalid market clock: {clock.error}")
        if clock.is_hard_flat(now):
            return
        if runtime._state.get("positions"):
            runtime.tick(now)
        elif clock.can_enter(now):
            runtime.scan_and_execute(now)
        else:
            runtime.tick(now)

    exit_code = 0
    try:
        if args.once:
            iterate(datetime.now(IST))
        else:
            failures = 0
            while True:
                now = datetime.now(IST)
                if not clock.valid:
                    raise SystemExit(f"invalid market clock: {clock.error}")
                if clock.is_closed(now):
                    print(f"{now.isoformat()}: exchange session closed")
                    break
                if clock.is_hard_flat(now):
                    print(f"{now.isoformat()}: hard flat")
                    break
                try:
                    iterate(now)
                    failures = 0
                except OptionsIntradayUnresolved as exc:
                    print(f"{now.isoformat()}: UNRESOLVED {exc}; "
                          "flattening and stopping")
                    exit_code = 2
                    break
                except Exception as exc:
                    # A transient quote/chain failure must not end the one-off
                    # session immediately, but three in a row is a real fault.
                    failures += 1
                    print(f"{now.isoformat()}: iteration failed "
                          f"({failures}/3): {type(exc).__name__}: {exc}")
                    if failures >= 3:
                        print("three consecutive failures: flattening and stopping")
                        exit_code = 2
                        break
                time.sleep(max(1.0, float(args.interval)))
    except KeyboardInterrupt:
        print("\ninterrupted: flattening")
        exit_code = 2
    finally:
        final = _finalize(runtime, broker, "finalize")
        exit_code = exit_code or final
        try:
            from src.alerts import send_options_telegram
            send_options_telegram(
                f"TRIO Muhurat session ended. Flat={final == 0}. "
                f"State={state_path}")
        except Exception:
            pass
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TRIO Diwali Muhurat 2026 one-off live runner")
    parser.add_argument("--env-file", help="explicit .env path")
    parser.add_argument("--preflight", action="store_true",
                        help="read-only account/contract/data checks")
    parser.add_argument("--flatten", action="store_true",
                        help="cancel working orders and square off NFO holdings")
    parser.add_argument("--probe-order", action="store_true",
                        help="one far 1-lot LIMIT then cancel (real order)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the probe confirmation prompt")
    parser.add_argument("--dry-run", action="store_true",
                        help="full loop with orders refused by the adapter")
    parser.add_argument("--scan", action="store_true",
                        help="one read-only discovery pass")
    parser.add_argument("--as-of", help="ISO timestamp for --scan")
    parser.add_argument("--once", action="store_true",
                        help="one live iteration, then flatten")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--capital", type=float,
                        help="override notional sizing capital")
    parser.add_argument("--state", help="options runtime state file")
    parser.add_argument("--underlyings", help="comma-separated scan list")
    parser.add_argument("--live-date",
                        help="override the date lock for --dry-run/--scan only")
    parser.add_argument("--broker", choices=("zerodha", "megabull"),
                        default="zerodha",
                        help="execution venue: zerodha (live) or megabull (paper)")
    parser.add_argument("--weekday-live", metavar="YYYY-MM-DD",
                        help="normal weekday live session on Zerodha; the date "
                             "must equal today (requires NFO segment + funds)")
    parser.add_argument("--confirm", metavar="PHRASE",
                        help="typed confirmation required to send real NFO "
                             "orders in --weekday-live mode (WEEKDAY-LIVE)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    env_path = _load_env(args.env_file)
    print(f"env file         : {env_path if env_path.exists() else 'not found'}")
    if args.live_date and not (args.dry_run or args.scan):
        print("--live-date requires --dry-run or --scan")
        return 2
    if args.weekday_live and args.broker == "megabull":
        print("--weekday-live is Zerodha-only; drop --broker megabull")
        return 2
    if args.weekday_live and args.live_date:
        print("--weekday-live replaces --live-date")
        return 2
    if args.flatten:
        return _flatten(args)
    if args.probe_order:
        return _probe_order(args)
    if args.preflight:
        return _preflight(args)
    if args.scan:
        return _scan(args)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
