"""
TRIO — Utility functions: logging setup, config loading, retry logic, helpers.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import logging
import os
import time
import functools
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Type vars
# ---------------------------------------------------------------------------
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Load .env file from project root if it exists."""
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)


def get_env(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """
    Get an environment variable.

    Args:
        key: Environment variable name.
        default: Fallback value if not set.
        required: If True, raise ValueError when missing.

    Returns:
        The value, or default.
    """
    value = os.environ.get(key, default)
    if required and value is None:
        raise ValueError(f"Required environment variable '{key}' is not set. "
                         f"Add it to your .env file or export it.")
    return value


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_config_cache: Optional[Dict[str, Any]] = None


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Load and cache the YAML config file.

    Args:
        path: Override path to config file.

    Returns:
        Parsed config dictionary.
    """
    global _config_cache
    if _config_cache is not None and path is None:
        return _config_cache

    config_file = path or CONFIG_PATH
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    with open(config_file, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if path is None:
        _config_cache = cfg
    return cfg


def get_config_section(section: str) -> Dict[str, Any]:
    """Get a top-level section from the config."""
    cfg = load_config()
    if section not in cfg:
        raise KeyError(f"Config section '{section}' not found.")
    return cfg[section]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_logger_initialized = False


def setup_logging(level: Optional[str] = None) -> logging.Logger:
    """
    Set up project-wide logging.

    Args:
        level: Override log level (DEBUG, INFO, WARNING, ERROR).

    Returns:
        The root TRIO logger.
    """
    global _logger_initialized

    if level is None:
        try:
            cfg = load_config()
            level = cfg.get("general", {}).get("log_level", "INFO")
        except FileNotFoundError:
            level = "INFO"

    log_level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger("trio")

    if not _logger_initialized:
        logger.setLevel(log_level)
        handler = logging.StreamHandler()
        handler.setLevel(log_level)
        formatter = logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(name)s.%(module)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        _logger_initialized = True

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the trio namespace."""
    setup_logging()
    return logging.getLogger(f"trio.{name}")


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------

def retry_with_backoff(
    max_retries: int = 3,
    backoff_base: float = 2.0,
    exceptions: tuple = (Exception,),
) -> Callable:
    """
    Decorator: retry a function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts.
        backoff_base: Base for exponential wait (seconds).
        exceptions: Tuple of exception types to catch.

    Returns:
        Decorated function.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            logger = get_logger("retry")
            last_exception: Optional[Exception] = None

            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        wait = backoff_base ** attempt
                        logger.warning(
                            "%s attempt %d/%d failed: %s — retrying in %.1fs",
                            func.__name__, attempt, max_retries, exc, wait,
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__name__, max_retries, exc,
                        )

            raise last_exception  # type: ignore[misc]
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
from typing import Optional


def utc_now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def hours_ago(iso_str: str) -> float:
    """Return how many hours ago an ISO-8601 timestamp was."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - dt
    return delta.total_seconds() / 3600.0


IST = timezone(timedelta(hours=5, minutes=30))


def ist_display(iso_str: Optional[str] = None) -> str:
    """Render an ISO-8601 UTC timestamp as IST + UTC, for humans.

    Example: ``07 Oct, 10:15 IST (04:45 UTC)``. ``None``/unparseable
    input yields ``—`` so formatters never crash on missing data.
    """
    try:
        if not iso_str:
            return "—"
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist = dt.astimezone(IST)
        return (ist.strftime("%d %b, %H:%M IST")
                + dt.astimezone(timezone.utc).strftime(" (%H:%M UTC)"))
    except Exception:  # noqa: BLE001 - display helper, never raise
        return "—"


def minutes_between(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[int]:
    """Whole minutes between two ISO-8601 timestamps (None if unparseable)."""
    try:
        if not start_iso or not end_iso:
            return None
        a = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        return int((b - a).total_seconds() // 60)
    except Exception:  # noqa: BLE001 - display helper, never raise
        return None
