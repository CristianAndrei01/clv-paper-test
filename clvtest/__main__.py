"""Punct de intrare: `python -m clvtest tick`."""
from __future__ import annotations

import argparse
import sys

from .config import active_sports, load_env


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clvtest")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="colector: snapshot-urile care sunt scadente acum")
    args = p.parse_args(argv)

    load_env()
    if args.cmd == "tick":
        from .collector import tick
        return tick(active_sports())
    return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
