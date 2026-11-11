"""Punct de intrare.

  python -m clvtest tick                    colectorul, apoi predictorul dacă e scadent
  python -m clvtest predict --dry-run       predictorul acum, cu rollback la final (nimic nu rămâne)
  python -m clvtest predict --dry-run --fake-model   la fel, fără niciun apel la Claude
  python -m clvtest settle [--force] [--dry-run]     decontarea (zilnică; --force ignoră pauza de 20 de ore)
  python -m clvtest report                  raportul CLV, pe serii
"""
from __future__ import annotations

import argparse
import os
import sys

from .config import active_sports, load_env


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clvtest")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="colector + predictor (dacă e scadent)")
    pr = sub.add_parser("predict", help="rulează predictorul acum")
    pr.add_argument("--dry-run", action="store_true", help="totul, inclusiv inserarea, apoi ROLLBACK")
    pr.add_argument("--fake-model", action="store_true", help="fără apel la Claude (doar cu --dry-run)")
    pr.add_argument("--force", action="store_true", help="ignoră fereastra de 2–3 ore (doar cu --dry-run)")
    st = sub.add_parser("settle", help="decontează pariurile pe meciurile încheiate")
    st.add_argument("--force", action="store_true", help="ignoră pauza de 20 de ore dintre decontări")
    st.add_argument("--dry-run", action="store_true", help="totul, inclusiv inserarea, apoi ROLLBACK")
    sub.add_parser("report", help="raportul CLV")
    args = p.parse_args(argv)

    load_env()
    sports = active_sports()
    if args.cmd == "tick":
        from .collector import tick as collect
        rc = collect(sports)
        if os.environ.get("ANTHROPIC_API_KEY", "").strip() and os.environ.get("PREDICTOR_ENABLED", "true") == "true":
            from .predictor import tick as predict
            rc |= predict(sports)
        if os.environ.get("DB_URL_SETTLER", "").strip():
            from .settle import tick as settle
            rc |= settle(sports)
        return rc
    if args.cmd == "predict":
        if (args.fake_model or args.force) and not args.dry_run:
            p.error("--fake-model și --force merg doar cu --dry-run")
        from .predictor import tick as predict
        return predict(sports, dry_run=args.dry_run, fake=args.fake_model, force=args.force)
    if args.cmd == "settle":
        from .settle import tick as settle
        return settle(sports, force=args.force, dry_run=args.dry_run)
    if args.cmd == "report":
        from .report import main as report
        return report()
    return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
