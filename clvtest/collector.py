"""Colector (SPEC §4.1). Rulează la fiecare „tick” programat și decide singur ce e de făcut.

Deciziile se iau din starea bazei de date, nu din ora exactă a rulării, ca o rulare
întârziată de GitHub Actions să fie recuperată la tick-ul următor.
"""
from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .config import SportConfig, require
from .odds import normalize_event_odds
from .propline import HTTPStatusError, PropLine

PERIODIC_TOLERANCE_MIN = 10   # un tick întârziat cu câteva minute nu amână snapshot-ul cu încă 2 ore
MIN_MINUTES_BEFORE_START = 5


# ---------------------------------------------------------------- decizii (funcții pure, testate)
def in_periodic_window(sport: SportConfig, commence: datetime, now: datetime) -> bool:
    if commence <= now:
        return False
    if sport.game_day_tz:
        tz = ZoneInfo(sport.game_day_tz)
        return commence.astimezone(tz).date() == now.astimezone(tz).date()
    if sport.lookahead_hours is not None:
        return commence - now <= timedelta(hours=sport.lookahead_hours)
    return False


def periodic_due(sport: SportConfig, last_at: datetime | None, now: datetime, window_events: int) -> bool:
    if window_events == 0:
        return False
    if last_at is None:
        return True
    return now - last_at >= timedelta(minutes=sport.periodic_every_min - PERIODIC_TOLERANCE_MIN)


def prestart_due(sport: SportConfig, commence: datetime, now: datetime, already_done: bool) -> bool:
    if already_done:
        return False
    minutes = (commence - now).total_seconds() / 60
    return MIN_MINUTES_BEFORE_START < minutes <= sport.prestart_min[1]


def event_odds_endpoint(sport_key: str, event_id: str) -> str:
    return f"/sports/{sport_key}/events/{event_id}/odds"


# ---------------------------------------------------------------- rulare
def _store_odds(conn, run_id, sport: SportConfig, kind: str, resp, events: list[dict]) -> tuple[int, list[str]]:
    raw_id = db.save_raw(conn, run_id, kind, sport.key, resp)
    db.upsert_events(conn, sport.key, events)       # cheia străină din odds_snapshots
    rows, warnings = [], []
    for ev in events:
        r, w = normalize_event_odds(ev, set(sport.markets.split(",")), set(sport.bookmakers))
        rows += r
        warnings += w
    n = db.save_snapshots(conn, run_id, raw_id, kind, rows)
    conn.commit()
    return n, warnings


def store_odds_snapshot(conn, api: PropLine, run_id: int, sport: SportConfig, kind: str) -> int:
    """Un snapshot complet al cotelor pentru sport (folosit și de predictor, cu kind='predictor')."""
    params = {"markets": sport.markets, "bookmakers": ",".join(sport.bookmakers)}
    r = api.get(f"/sports/{sport.key}/odds", params)
    n, _ = _store_odds(conn, run_id, sport, kind, r, r.data)
    return n


def collect_sport(conn, api: PropLine, run_id: int, sport: SportConfig) -> dict:
    d: dict = {"warnings": []}
    params = {"markets": sport.markets, "bookmakers": ",".join(sport.bookmakers)}

    # 1. meciurile viitoare
    r = api.get(f"/sports/{sport.key}/events")
    db.save_raw(conn, run_id, "events", sport.key, r)
    d["events_seen"] = db.upsert_events(conn, sport.key, r.data)
    conn.commit()

    now = db.db_now(conn)
    upcoming = db.upcoming_events(conn, sport.key, hours=max(36, sport.lookahead_hours or 0))
    window = [(eid, ct) for eid, ct in upcoming if in_periodic_window(sport, ct, now)]
    d["window_events"] = len(window)

    # 2. snapshot periodic (toate meciurile, o singură cerere) + scoruri
    last = db.last_raw_at(conn, sport.key, "periodic")
    if periodic_due(sport, last, now, len(window)):
        r = api.get(f"/sports/{sport.key}/odds", params)
        d["periodic_rows"], w = _store_odds(conn, run_id, sport, "periodic", r, r.data)
        d["warnings"] += w
        s = api.get(f"/sports/{sport.key}/scores", {"days_from": 1})
        db.save_raw(conn, run_id, "scores", sport.key, s)
        d["scores_updated"] = db.update_scores(conn, s.data)
        conn.commit()

    # 3. snapshot de dinainte de start, o dată pe meci
    now = db.db_now(conn)
    soon = [(eid, ct) for eid, ct in upcoming if ct > now]
    done = db.endpoints_fetched(conn, "prestart", [event_odds_endpoint(sport.key, eid) for eid, _ in soon])
    d["prestart"] = []
    for eid, ct in soon:
        ep = event_odds_endpoint(sport.key, eid)
        if not prestart_due(sport, ct, now, ep in done):
            continue
        minutes = round((ct - now).total_seconds() / 60, 1)
        try:
            r = api.get(ep, params)
        except HTTPStatusError as e:
            if e.status == 404:                 # meci scos din ofertă / amânat: nu e o eroare a colectorului
                d["warnings"].append(f"prestart {eid}: 404")
                continue
            raise
        rows, w = _store_odds(conn, run_id, sport, "prestart", r, [r.data])
        d["warnings"] += w
        d["prestart"].append({"event_id": eid, "minutes_to_start": minutes, "rows": rows})

    d["warnings"] = d["warnings"][:30]
    return d


def tick(sports: list[SportConfig]) -> int:
    api = PropLine(require("PROPLINE_API_KEY"))
    conn = db.connect("collector")
    run_id = db.start_run(conn, "collector")
    details: dict = {"sports": {}}
    try:
        for sport in sports:
            details["sports"][sport.key] = collect_sport(conn, api, run_id, sport)
    except Exception as e:  # noqa: BLE001 — orice eroare se înregistrează în runs, apoi rularea pică
        db.finish_run(conn, run_id, "error", api.requests, api.daily_remaining, details,
                      error="".join(traceback.format_exception(e))[-4000:])
        print(f"run {run_id}: EROARE {e}")
        return 1
    finally:
        api.close()
    db.finish_run(conn, run_id, "ok", api.requests, api.daily_remaining, details)
    conn.close()
    print(f"run {run_id}: ok, {api.requests} cereri, X-Daily-Remaining={api.daily_remaining}")
    print(db.dumps(details))
    return 0
