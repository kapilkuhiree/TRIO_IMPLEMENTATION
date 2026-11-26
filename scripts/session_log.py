"""Read (and optionally mirror) a TRIO session log.

Windows shells write ``> file`` as UTF-16, while the application writes UTF-8
via ``TRIO_LOG_FILE``.  This tool sniffs the encoding so either file can be
read, and ``--follow`` copies new lines into a UTF-8 mirror.

    python scripts/session_log.py --source <path>            # print once
    python scripts/session_log.py --source <path> --follow   # mirror live

DISCLAIMER: Educational purposes only. Not financial advice.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def detect_encoding(path: Path) -> str:
    """Return the encoding that makes ``path`` readable as text."""
    try:
        head = path.open("rb").read(4096)
    except OSError:
        return "utf-8"
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    # UTF-16LE without a BOM shows up as interleaved NUL bytes.
    if head and head.count(b"\x00") > len(head) // 4:
        return "utf-16-le"
    return "utf-8"


def read_text(path: Path) -> str:
    encoding = detect_encoding(path)
    try:
        return path.read_text(encoding=encoding, errors="replace")
    except OSError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a TRIO session log")
    parser.add_argument("--source", default=None,
                        help="log file to read (default: today's app log)")
    parser.add_argument("--out", default=None,
                        help="UTF-8 mirror path (default: output/logs/session_<date>.log)")
    parser.add_argument("--follow", action="store_true",
                        help="keep mirroring new lines as they are written")
    parser.add_argument("--tail", type=int, default=0,
                        help="only print the last N lines (0 = all)")
    args = parser.parse_args()

    source = Path(args.source).expanduser() if args.source else (
        ROOT / "output" / "logs" / f"session_{time.strftime('%Y-%m-%d')}.log")
    if not source.exists():
        print(f"no such log: {source}", file=sys.stderr)
        return 2

    encoding = detect_encoding(source)
    print(f"# source : {source}")
    print(f"# encoding: {encoding}")

    text = read_text(source)
    lines = text.splitlines()
    if args.tail and args.tail > 0:
        lines = lines[-args.tail:]
    if not args.follow:
        for line in lines:
            print(line)
        return 0

    mirror = Path(args.out).expanduser() if args.out else (
        ROOT / "output" / "logs" / f"session_{time.strftime('%Y-%m-%d')}.log")
    mirror.parent.mkdir(parents=True, exist_ok=True)
    print(f"# mirror : {mirror}")
    print(f"# following {source} — Ctrl-C to stop")

    seen = 0
    with io.open(mirror, "a", encoding="utf-8") as handle:
        while True:
            current = read_text(source).splitlines()
            if len(current) < seen:
                # The writer truncated/rotated the file; restart from the top.
                seen = 0
            for line in current[seen:]:
                print(line)
                handle.write(line + "\n")
            seen = len(current)
            handle.flush()
            time.sleep(1.0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
