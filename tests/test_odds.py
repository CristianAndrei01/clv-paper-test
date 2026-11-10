import json
from decimal import Decimal
from pathlib import Path

import pytest

from clvtest.odds import american_to_decimal, normalize_event_odds, side_of

FIXTURE = Path(__file__).parent / "fixtures" / "nba_odds_event.json"


@pytest.mark.parametrize("american, decimal", [
    (100, "2.00000"), (150, "2.50000"), (-200, "1.50000"), (-110, "1.90909"), (122, "2.22000"), (-163, "1.61350"),
])
def test_american_to_decimal(american, decimal):
    assert american_to_decimal(american) == Decimal(decimal)


def test_american_none_and_zero():
    assert american_to_decimal(None) is None
    with pytest.raises(ValueError):
        american_to_decimal(0)


def test_side_of():
    assert side_of("Over", "H", "A") == "over"
    assert side_of("under", "H", "A") == "under"
    assert side_of("Draw", "H", "A") == "X"
    assert side_of("H", "H", "A") == "1"
    assert side_of("A", "H", "A") == "2"
    assert side_of("18+ Points", "H", "A") is None


def test_normalize_real_event_keeps_only_main_lines_of_requested_books():
    ev = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows, warnings = normalize_event_odds(ev, {"player_points"}, {"pinnacle", "draftkings", "fanduel"})
    assert warnings == []
    # 3 case × 2 piețe principale × (over, under); liniile milestone sunt eliminate
    assert len(rows) == 12
    assert {r["line_type"] for r in rows} == {"main"}
    assert {r["bookmaker"] for r in rows} == {"pinnacle", "draftkings", "fanduel"}
    brunson_pin = [r for r in rows if r["bookmaker"] == "pinnacle" and r["player_id"] == "nba:1628973"]
    assert {(r["side"], float(r["line"]), r["price_american"]) for r in brunson_pin} == {
        ("over", 26.5, -108), ("under", 26.5, -122)}
    assert all(r["player_name"] == "Jalen Brunson" for r in brunson_pin)
    assert all(r["last_seen_at"] is not None for r in rows)


def test_normalize_filters_bookmakers():
    ev = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows, _ = normalize_event_odds(ev, {"player_points"}, {"pinnacle"})
    assert {r["bookmaker"] for r in rows} == {"pinnacle"}


def test_normalize_h2h():
    ev = {"id": 1, "home_team": "Vicenza", "away_team": "Cremonese", "bookmakers": [{
        "key": "pinnacle", "markets": [{"key": "h2h", "line_type": "main", "last_update": "2026-11-08T18:25:00Z",
            "outcomes": [{"name": "Vicenza", "price": 268}, {"name": "Cremonese", "price": 126},
                         {"name": "Draw", "price": 216}]}]}]}
    rows, warnings = normalize_event_odds(ev, {"h2h"}, {"pinnacle"})
    assert warnings == []
    assert [(r["side"], r["price_american"]) for r in rows] == [("1", 268), ("2", 126), ("X", 216)]
    assert all(r["player_id"] is None and r["player_name"] is None for r in rows)
