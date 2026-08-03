"""Minimal process entry point for the detached Silicon supervisor."""
from __future__ import annotations

import sys

from .process import watchdog_loop


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        raise SystemExit("usage: python -m silicon_cli._watchdog PATH NAME PID_FILE")
    path, name, pid_file = args
    watchdog_loop(name=name or "silicon", path=path, pid_file=pid_file)


if __name__ == "__main__":
    main()
