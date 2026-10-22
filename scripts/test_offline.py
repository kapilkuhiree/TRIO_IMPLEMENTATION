"""Run pytest offline: python scripts/test_offline.py [pytest arguments].

Dependencies must already be installed. Guards apply before pytest imports or
collects tests; tests can still replace request methods or inject fake sessions.
Application fallbacks cannot swallow OfflineNetworkError and turn real network
attempts into passing tests. Use Docker's --network none for process isolation.
"""

import os
import socket
import sys
import tempfile
from contextlib import ExitStack
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch


class OfflineNetworkError(BaseException):
    """Fail a test immediately, including through broad application retries."""


def _blocked(*args, **kwargs):
    # Do not include URLs, headers or arguments: they could contain credentials.
    raise OfflineNetworkError("Network transport blocked by scripts/test_offline.py")


async def _blocked_async(*args, **kwargs):
    _blocked()


def _guard_connect(original):
    def connect(sock, address):
        # asyncio uses a local socketpair for its wakeup pipe on Windows.
        # HTTP libraries below remain blocked even for loopback addresses.
        if (sock.family == getattr(socket, "AF_UNIX", None)
                or (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"))):
            return original(sock, address)
        return _blocked()

    return connect


def _prepare_environment():
    # Keep inherited account credentials out of tests; fixtures may add fake ones.
    for name in tuple(os.environ):
        upper = name.upper()
        if (any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
                or upper.endswith("_KEY")
                or upper.startswith(("TRIO_", "MEGABULL_", "ZERODHA_", "TELEGRAM_", "REDDIT_"))):
            os.environ.pop(name, None)
    os.environ.update({
        "TRIO_TEST_MODE": "1",
        "TRIO_BROKER_PROVIDER": "paper",
        "TRIO_REMOTE_REQUIRED": "0",
        "TRIO_LIVE": "0",
        "PYTHON_DOTENV_DISABLED": "1",
        "NETRC": os.devnull,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    # Ignore machine-wide pytest plugins/options; explicit CLI options still work.
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    for name in tuple(os.environ):
        if name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
            os.environ.pop(name, None)


def main(args=None):
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    sys.path.insert(0, str(root))
    _prepare_environment()

    with ExitStack() as guards:
        guards.enter_context(patch("socket.create_connection", _blocked))
        for method in ("connect", "connect_ex"):
            original = getattr(socket.socket, method)
            guards.enter_context(patch.object(socket.socket, method, _guard_connect(original)))
        # Patch transport boundaries, not requests.get/post or injected sessions.
        guards.enter_context(patch("requests.sessions.Session.send", _blocked))
        if find_spec("curl_cffi") is not None:
            guards.enter_context(patch("curl_cffi.Curl.perform", _blocked))
            guards.enter_context(patch("curl_cffi.AsyncCurl.add_handle", _blocked))
            guards.enter_context(patch("curl_cffi.requests.Session.request", _blocked))
            guards.enter_context(patch("curl_cffi.requests.AsyncSession.request", _blocked_async))

        guards.enter_context(patch("dotenv.load_dotenv", return_value=False))
        guards.enter_context(patch("dotenv.dotenv_values", return_value={}))
        # This adapter also reads .env directly, independently of python-dotenv.
        guards.enter_context(patch("src.broker.megabull.load_dotenv_key", return_value=""))

        import pytest

        temp_root = root / ".pytest-tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="offline-", dir=temp_root) as temp:
            # Default provider diagnostics/plans must never overwrite the
            # checkout's persisted session artifacts. Tests can override these
            # paths again when explicitly exercising file persistence.
            from src import options_chain, plan
            guards.enter_context(patch.object(options_chain, "CHAIN_CACHE_DIR", Path(temp) / "chains"))
            guards.enter_context(patch.object(options_chain, "CHAIN_DIAGNOSTIC_DIR", Path(temp) / "diagnostics"))
            guards.enter_context(patch.object(plan, "PLAN_DIR", Path(temp) / "plans"))
            options = ["-q", "--basetemp", str(Path(temp) / "pytest")]
            options.extend(sys.argv[1:] if args is None else args)
            return int(pytest.main(options))


if __name__ == "__main__":
    raise SystemExit(main())
