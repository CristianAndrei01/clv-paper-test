"""Acces la Postgres. Fiecare componentă folosește doar rolul ei (DB_URL_COLLECTOR etc.)."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

from .config import require
from .odds import ROW_FIELDS
from .propline import Response


def connect(component: str) -> psycopg.Connection:
    url = require(f"DB_URL_{component.upper()}")
    return psycopg.connect(url, autocommit=False, connect_timeout=20, application_name=f"clvtest-{component}")


def start_run(conn: psycopg.Connection, component: str) -> int:
    run_id = conn.execute(
        "insert into clv.runs (component, gh_run_id) values (%s, %s) returning id",
        (component, os.environ.get("GITHUB_RUN_ID")),
    ).fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, status: str, api_requests: int,
               daily_remaining: int | None, details: dict, error: str | None = None) -> None:
    conn.rollback()  # o eroare anterioară nu trebuie să blocheze înregistrarea rulării
    conn.execute(
        """update clv.runs set finished_at = clock_timestamp(), status = %s, api_requests = %s,
                  api_daily_remaining = %s, details = %s, error = %s where id = %s""",
        (status, api_requests, daily_remaining, Jsonb(details), error, run_id),
    )
    conn.commit()


def upsert_events(conn: psycopg.Connection, sport_key: str, events: list[dict]) -> int:
    n = 0
    for e in events:
        if e.get("is_outright"):
            continue
        conn.execute(
            """insert into clv.events (id, sport_key, home_team, away_team, commence_time)
               values (%s, %s, %s, %s, %s)
               on conflict (id) do update set
                 home_team = excluded.home_team, away_team = excluded.away_team,
                 commence_time = excluded.commence_time
               where (clv.events.home_team, clv.events.away_team, clv.events.commence_time)
                     is distinct from (excluded.home_team, excluded.away_team, excluded.commence_time)""",
            (str(e["id"]), sport_key, e["home_team"], e["away_team"], e["commence_time"]),
        )
        n += 1
    return n


def update_scores(conn: psycopg.Connection, scores: list[dict]) -> int:
    n = 0
    for s in scores:
        cur = conn.execute(
            """update clv.events set status = %s, home_score = %s, away_score = %s
               where id = %s and (status, home_score, away_score) is distinct from (%s::text, %s::int, %s::int)""",
            (s.get("status"), s.get("home_score"), s.get("away_score"), str(s["id"]),
             s.get("status"), s.get("home_score"), s.get("away_score")),
        )
        n += cur.rowcount
    return n


def save_raw(conn: psycopg.Connection, run_id: int, kind: str, sport_key: str, resp: Response) -> int:
    return conn.execute(
        """insert into clv.raw_responses (run_id, kind, sport_key, endpoint, params, status_code,
                                          bytes, body_sha256, body_gzip, headers)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (run_id, kind, sport_key, resp.endpoint, Jsonb(resp.params), resp.status,
         len(resp.body), resp.sha256, resp.gzipped(), Jsonb(resp.headers)),
    ).fetchone()[0]


def save_snapshots(conn: psycopg.Connection, run_id: int, raw_id: int, kind: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ("raw_response_id", "run_id", "snapshot_kind", *ROW_FIELDS)
    with conn.cursor().copy(f"copy clv.odds_snapshots ({', '.join(cols)}) from stdin") as cp:
        for r in rows:
            cp.write_row((raw_id, run_id, kind, *(r[f] for f in ROW_FIELDS)))
    return len(rows)


def last_raw_at(conn: psycopg.Connection, sport_key: str, kind: str):
    return conn.execute(
        "select max(fetched_at) from clv.raw_responses where sport_key = %s and kind = %s",
        (sport_key, kind),
    ).fetchone()[0]


def endpoints_fetched(conn: psycopg.Connection, kind: str, endpoints: list[str]) -> set[str]:
    """Care dintre aceste endpoint-uri au deja un răspuns salvat de tipul `kind`."""
    if not endpoints:
        return set()
    rows = conn.execute(
        "select distinct endpoint from clv.raw_responses where kind = %s and endpoint = any(%s)",
        (kind, endpoints),
    ).fetchall()
    return {r[0] for r in rows}


def upcoming_events(conn: psycopg.Connection, sport_key: str, hours: float) -> list[tuple]:
    return conn.execute(
        """select id, commence_time from clv.events
           where sport_key = %s and commence_time > clock_timestamp()
             and commence_time < clock_timestamp() + make_interval(hours => %s)
           order by commence_time""",
        (sport_key, hours),
    ).fetchall()


def db_now(conn: psycopg.Connection):
    return conn.execute("select clock_timestamp()").fetchone()[0]


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
