"""Drepturile celor trei roluri, verificate pe baza reală (doar citiri și încercări care trebuie să pice).

Rulează doar dacă DB_URL_* sunt setate (în .env). Nu scrie nimic: fiecare încercare e respinsă
de Postgres sau anulată cu rollback.
"""
import os

import psycopg
import pytest

from clvtest.config import load_env

load_env()
pytestmark = pytest.mark.skipif(not os.environ.get("DB_URL_PREDICTOR"), reason="fără DB_URL_* în .env")


def conn(role):
    return psycopg.connect(os.environ[f"DB_URL_{role.upper()}"], connect_timeout=20)


def denied(role, sql):
    with conn(role) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(sql)
        c.rollback()


@pytest.mark.parametrize("sql", [
    "select 1 from clv.settlements limit 1",
    "select 1 from clv.events limit 1",
    "select 1 from clv.odds_snapshots limit 1",
    "select 1 from clv.raw_responses limit 1",
    "select status from clv.runs limit 1",
    "update clv.predictions set reason = 'x'",
    "delete from clv.predictions",
    "update clv.series set edge_threshold = 0",
])
def test_predictor_cannot_see_results_or_change_history(sql):
    denied("predictor", sql)


def test_predictor_sees_only_future_through_views():
    with conn("predictor") as c:
        n_past = c.execute("select count(*) from clv.predictor_events where commence_time <= now()").fetchone()[0]
        assert n_past == 0
        c.execute("select 1 from clv.predictor_odds limit 1")
        c.execute("select 1 from clv.predictions limit 1")


@pytest.mark.parametrize("sql", [
    "select 1 from clv.predictions limit 1",
    "select 1 from clv.settlements limit 1",
    "insert into clv.predictions (seq) values (1)",
    "delete from clv.odds_snapshots",
    "update clv.odds_snapshots set price_american = 0",
])
def test_collector_limited(sql):
    denied("collector", sql)


@pytest.mark.parametrize("sql", [
    "update clv.predictions set reason = 'x'",
    "delete from clv.predictions",
    "truncate clv.predictions",
    "insert into clv.predictions (seq) values (1)",
    "delete from clv.odds_snapshots",
])
def test_settler_cannot_touch_predictions(sql):
    denied("settler", sql)


def test_anon_role_has_no_schema_access():
    with conn("settler") as c:
        assert c.execute("select has_schema_privilege('anon', 'clv', 'usage')").fetchone()[0] is False
