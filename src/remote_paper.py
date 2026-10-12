"""
TRIO — Remote paper execution helpers
Author: Kapil Kuhire <kapilkuhire89@gmail.com>

Validates that a remote-only session is correctly configured before any
order is sent, and manufactures the options broker that matches the
equity broker provider.  No local shadow fallback is permitted when
remote_paper_required() is True.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import os
from typing import Any, Dict

from src.utils import get_logger

logger = get_logger("remote_paper")


def _boolish(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def validate_remote_config(cfg: Dict[str, Any], provider: str) -> None:
    """Fail-closed validation before any remote order is sent.

    GitHub sessions must explicitly declare:
      TRIO_BROKER_PROVIDER=megabull
      TRIO_REMOTE_REQUIRED=1
      TRIO_ALLOW_LOCAL_OPTION_SHADOW=0
    """
    if provider != "megabull":
        raise RuntimeError(
            f"Remote paper required but broker provider is '{provider}'. "
            "Set TRIO_BROKER_PROVIDER=megabull."
        )

    remote_env = os.environ.get("TRIO_REMOTE_REQUIRED", "")
    if not _boolish(remote_env):
        remote_cfg = (cfg.get("execution", {}) or {}).get("remote_paper_required")
        if not _boolish(remote_cfg):
            raise RuntimeError(
                "Remote paper execution not explicitly enabled. "
                "Set TRIO_REMOTE_REQUIRED=1 or execution.remote_paper_required=true."
            )

    shadow_env = os.environ.get("TRIO_ALLOW_LOCAL_OPTION_SHADOW", "")
    if shadow_env != "0":
        shadow_cfg = (cfg.get("execution", {}) or {}).get("allow_local_option_shadow")
        if _boolish(shadow_cfg):
            raise RuntimeError(
                "Local option shadow must be disabled for remote-only mode. "
                "Set TRIO_ALLOW_LOCAL_OPTION_SHADOW=0 or "
                "execution.allow_local_option_shadow=false."
            )

    api_key = os.environ.get("MEGABULL_API_KEY", "")
    if not api_key:
        from src.broker.megabull import load_dotenv_key
        api_key = load_dotenv_key("MEGABULL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing MEGABULL_API_KEY — remote paper execution requires credentials."
        )

    if os.environ.get("TRIO_TEST_MODE") == "1":
        return

    from src.broker.megabull import MegaBullBroker
    mb = MegaBullBroker(api_key=api_key)
    try:
        bal = mb.get_balance()
        if not isinstance(bal, dict) or "available" not in bal:
            raise RuntimeError("MegaBull balance endpoint returned invalid schema.")
    except Exception as exc:
        raise RuntimeError(f"MegaBull health check failed: {exc}") from exc


def make_options_broker(trader, cfg: Dict[str, Any]) -> Any:
    """Create the options broker matching the remote paper equity broker."""
    eq_broker = getattr(trader, "broker", None)
    if eq_broker is None:
        raise RuntimeError("No equity broker attached to trader")
    eq_name = str(getattr(eq_broker, "provider_name", "")).lower()
    if "megabull" not in eq_name:
        raise RuntimeError(
            "Options broker requires MegaBull equity broker in remote mode; "
            f"got '{eq_name}'."
        )
    from src.broker.megabull_options import MegaBullOptionsBroker
    opt_lot = int((cfg.get("options", {}) or {}).get("lot_size", 65))
    return MegaBullOptionsBroker(eq_broker, lot_size=opt_lot)
