"""Refresh the Kite access token in .env, then report segments and cash.

Run from the repo root:

    python scripts/kite_token_refresh.py

It prints a Kite login URL, asks for the request_token from the redirect,
exchanges it for an access token, updates ZERODHA_ACCESS_TOKEN in .env
(backing up the previous file), and then prints:

    - exchanges enabled (NFO is required for NSE F&O)
    - equity cash / live balance

The API secret and the access token are never printed.
"""

import os
import re
import shutil
import sys
from pathlib import Path

ENV_PATH = Path(".env")
CONFIG_PATH = Path("config/options_intraday.yaml")
DEFAULT_MIN_WALLET = 20000.0


def _wallet_floor() -> float:
    """Read ``min_wallet`` from the live config so this check cannot drift."""
    try:
        import yaml

        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        mode = cfg.get("options_intraday", cfg) or {}
        value = mode.get("min_wallet")
        if value is not None:
            return float(value)
    except Exception:
        pass
    return DEFAULT_MIN_WALLET


MIN_WALLET = _wallet_floor()


def _read_env_value(text: str, key: str) -> str:
    match = re.search(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)$",
                      text, flags=re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _write_access_token(text: str, token: str) -> str:
    line = f"ZERODHA_ACCESS_TOKEN={token}"
    pattern = re.compile(r"^\s*(?:export\s+)?ZERODHA_ACCESS_TOKEN\s*=.*$",
                         flags=re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def main() -> int:
    if not ENV_PATH.exists():
        print(f"missing {ENV_PATH.resolve()}")
        return 2

    text = ENV_PATH.read_text(encoding="utf-8")
    api_key = _read_env_value(text, "ZERODHA_API_KEY")
    api_secret = _read_env_value(text, "ZERODHA_API_SECRET")
    if not api_key or not api_secret:
        print("ZERODHA_API_KEY / ZERODHA_API_SECRET missing from .env")
        return 2

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    print("1) Open this URL, log in, then copy the request_token")
    print("   from the redirect URL:")
    print()
    print("   " + kite.login_url())
    print()
    request_token = input("2) Paste request_token: ").strip()
    if not request_token:
        print("no request_token given")
        return 1

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        print(f"token exchange failed: {exc}")
        return 2

    access_token = str(session.get("access_token") or "")
    if not access_token:
        print("token exchange returned no access_token")
        return 2

    backup = ENV_PATH.parent / (ENV_PATH.name + ".bak")
    shutil.copyfile(ENV_PATH, backup)
    ENV_PATH.write_text(_write_access_token(text, access_token),
                        encoding="utf-8")
    print(f"3) access token updated in {ENV_PATH.name} "
          f"(backup: {backup.name})")

    kite.set_access_token(access_token)
    try:
        profile = kite.profile() or {}
    except Exception as exc:
        print(f"login check failed: {exc}")
        return 2

    exchanges = [str(item).upper() for item in (profile.get("exchanges") or [])]
    print()
    print("user        :", profile.get("user_id"))
    print("exchanges   :", ", ".join(exchanges) or "-")

    margins = kite.margins() or {}
    equity = margins.get("equity") or {}
    available = equity.get("available") or {}
    cash = float(available.get("live_balance") or available.get("cash") or 0.0)

    print("equity net  :", equity.get("net"))
    print("available   :", f"{cash:,.2f}", f"(floor {MIN_WALLET:,.0f})")
    print()
    nfo_ok = "NFO" in exchanges
    cash_ok = cash >= MIN_WALLET
    print("NSE F&O (NFO):", "ACTIVE" if nfo_ok else "MISSING")
    print("cash floor   :", "OK" if cash_ok else "BELOW FLOOR")
    print()
    if nfo_ok and cash_ok:
        print("READY for preflight / probe.")
        return 0
    print("NOT READY yet.")
    if not nfo_ok:
        print("  - enable NSE F&O (NFO) in console.zerodha.com -> Segments")
    if not cash_ok:
        print(f"  - add funds until available cash >= {MIN_WALLET:,.0f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
