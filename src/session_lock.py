"""Single-instance guard for a trading session.

Two session processes sharing one options state file can both pass the entry
gates before either persists an intent.  On 2026-11-23 a second options
session was launched at 13:16 while the 10:21 session was still running; both
wrote to the same ``state.json`` and the skip rate doubled.  No duplicate order
was placed only because the consecutive-loss halt was already active.

The guard is an OS-level exclusive lock on a sidecar file next to the state,
so it is released by the kernel when the process dies and a crashed session
can never leave a permanent block.

The holder's PID goes in a second, *unlocked* sidecar file.  On Windows a
byte-range-locked region cannot even be read by another process, so the lock
file itself is useless as diagnostic output; ``session.lock.owner`` is what
the refusal message reports.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

if os.name == "nt":  # pragma: no cover - platform selection
    import msvcrt

    def _try_lock(handle) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:  # pragma: no cover - platform selection
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class SessionLockError(RuntimeError):
    """Raised when another session already owns the lock."""

    def __init__(self, path: Path, holder: str) -> None:
        self.path = Path(path)
        self.holder = holder
        detail = f" (held by pid {holder})" if holder else ""
        super().__init__(
            f"another session already owns {self.path}{detail}; "
            "refusing to start a second options session"
        )


def _state_base_dir() -> Path:
    """Mirror ``OptionsIntradayRuntime._resolve_state_path`` defaults."""
    state_path = os.environ.get("TRIO_OPTIONS_INTRADAY_STATE_PATH")
    if state_path:
        path = Path(state_path).expanduser()
        if path.suffix.lower() in {".json", ".state"}:
            return path.parent
        return path
    state_dir = os.environ.get("TRIO_OPTIONS_INTRADAY_STATE_DIR")
    if state_dir:
        return Path(state_dir).expanduser()
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if root:
        return Path(root) / "TRIO" / "options_intraday"
    return Path.home() / ".trio" / "options_intraday"


def options_session_lock_path() -> Path:
    """Sidecar lock file for the options session state."""
    return _state_base_dir() / "session.lock"


def options_session_owner_path(lock_path: Path) -> Path:
    """Sidecar owner file: PID plus start time of the current holder."""
    return Path(lock_path).with_name(Path(lock_path).name + ".owner")


def _read_owner(lock_path: Path) -> str:
    try:
        raw = options_session_owner_path(lock_path).read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""
    return raw.splitlines()[0].strip() if raw else ""


def _write_owner(lock_path: Path) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        options_session_owner_path(lock_path).write_text(
            f"{os.getpid()}\n{stamp}\n", encoding="utf-8")
    except OSError:  # diagnostics only; never fail the session over this
        pass


@contextmanager
def single_instance_lock(path: Optional[Path] = None) -> Iterator[Path]:
    """Hold an exclusive session lock for the duration of the block.

    Raises :class:`SessionLockError` when another live process owns the lock.
    """
    lock_path = Path(path) if path is not None else options_session_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    # A zero-length file cannot be byte-range locked on Windows.
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(" ")
        handle.flush()
    handle.seek(0)
    if not _try_lock(handle):
        holder = _read_owner(lock_path)
        handle.close()
        raise SessionLockError(lock_path, holder)
    try:
        _write_owner(lock_path)
        yield lock_path
    finally:
        _unlock(handle)
        handle.close()
