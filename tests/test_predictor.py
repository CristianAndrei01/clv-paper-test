from datetime import datetime, timedelta, timezone

import pytest

from clvtest.config import SPORTS, PredictorConfig
from clvtest.devig import no_vig_multiplicative
from clvtest.predictor import (
    ModelResult, Outcome, build_lines, day_games, draw_controls, predictor_due, prompt_version,
    select_picks, series_id,
)

NBA = SPORTS["basketball_nba"]
UTC = timezone.utc
NOW = datetime(2026, 11, 11, 21, 0, tzinfo=UTC)          # 16:00 ET


def outcome(sid, eid, pid, side, line, dec, seen_min=1, stale=False):
    return Outcome(sid, eid, pid, pid.upper(), side, line, 100, dec, NOW - timedelta(minutes=seen_min), stale)


def pair(sid, eid, pid, line, over, under, **kw):
    return [outcome(sid, eid, pid, "over", line, over, **kw), outcome(sid + 1, eid, pid, "under", line, under, **kw)]


# ---------------------------------------------------------------- devig
def test_no_vig_two_and_three_way():
    p = no_vig_multiplicative([1.909, 1.909])
    assert p == pytest.approx([0.5, 0.5])
    p = no_vig_multiplicative([2.0, 1.8])
    assert sum(p) == pytest.approx(1.0) and p[0] < p[1]
    p3 = no_vig_multiplicative([3.68, 2.26, 3.16])
    assert sum(p3) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        no_vig_multiplicative([2.0])


# ---------------------------------------------------------------- linii
def test_build_lines_needs_both_sides_and_fresh_quotes():
    outs = (pair(1, "e1", "a", 20.5, 1.95, 1.87)
            + pair(3, "e1", "b", 10.5, 1.9, 1.9, seen_min=16)          # văzută acum 16 min -> afară
            + pair(5, "e1", "c", 5.5, 1.9, 1.9, stale=True)            # is_stale -> afară
            + [outcome(7, "e2", "d", "over", 15.5, 1.9)])               # fără under -> afară
    lines = build_lines(outs, NOW)
    assert [(l.player_id, l.line) for l in lines] == [("a", 20.5)]
    assert lines[0].p_over + lines[0].p_under == pytest.approx(1.0)


# ---------------------------------------------------------------- selecția pariurilor
def lines_now(over=1.95, line=20.5):
    return build_lines(pair(1, "e1", "a", line, over, 1.87) + pair(3, "e1", "b", 10.5, 2.0, 1.8), NOW)


def result(*picks):
    return ModelResult(picks=list(picks), notes="", model="m", stop_reason="tool_use",
                       search_urls=["https://ok.example/x"])


def test_select_picks_uses_fresh_price_and_threshold():
    offered = {"L1": lines_now()[0], "L2": lines_now()[1]}
    res = result({"line_id": "L1", "side": "over", "p_model": 0.56, "reason": "r",
                  "sources": ["https://ok.example/x", "https://made.up/y"]},
                 {"line_id": "L2", "side": "under", "p_model": 0.56, "reason": "r", "sources": []})
    chosen, skipped = select_picks(res, offered, lines_now(over=1.85), threshold=0.03, cap=10)
    # L1: 0.56 × 1.85 − 1 = 0.036 ≥ 0.03 ; L2: 0.56 × 1.8 − 1 = 0.008 < 0.03
    assert [c.line_id for c in chosen] == ["L1"]
    assert chosen[0].edge == pytest.approx(0.036)
    assert chosen[0].sources == [{"url": "https://ok.example/x", "found_in_search": True},
                                 {"url": "https://made.up/y", "found_in_search": False}]
    assert skipped == [{"line_id": "L2", "reason": "edge_below_threshold", "edge": 0.008,
                        "price_seen": 1.8, "price_now": 1.8}]


def test_select_picks_skips_moved_line_invalid_and_duplicates():
    offered = {"L1": lines_now()[0], "L2": lines_now()[1]}
    res = result({"line_id": "L1", "side": "over", "p_model": 0.7, "reason": "", "sources": []},
                 {"line_id": "L1", "side": "under", "p_model": 0.7, "reason": "", "sources": []},
                 {"line_id": "L9", "side": "over", "p_model": 0.7, "reason": "", "sources": []},
                 {"line_id": "L2", "side": "over", "p_model": 1.2, "reason": "", "sources": []})
    chosen, skipped = select_picks(res, offered, lines_now(line=21.5), threshold=0.03, cap=10)
    assert chosen == []
    assert [s["reason"] for s in skipped] == ["line_changed", "duplicate_line", "invalid", "invalid"]


def test_select_picks_keeps_highest_edges_up_to_cap():
    outs = []
    for i in range(5):
        outs += pair(10 * i, "e1", f"p{i}", 10.5, 2.0, 1.8)
    fresh = build_lines(outs, NOW)
    offered = {f"L{i + 1}": l for i, l in enumerate(fresh)}
    res = result(*[{"line_id": f"L{i + 1}", "side": "over", "p_model": 0.55 + i / 100, "reason": "", "sources": []}
                   for i in range(5)])
    chosen, skipped = select_picks(res, offered, fresh, threshold=0.03, cap=2)
    assert [c.line_id for c in chosen] == ["L5", "L4"]
    assert sum(s["reason"] == "over_daily_cap" for s in skipped) == 3


# ---------------------------------------------------------------- control
def test_controls_reproducible_from_seed():
    fresh = build_lines(sum((pair(10 * i, "e1", f"p{i}", 10.5, 1.9, 1.9) for i in range(8)), []), NOW)
    a = draw_controls(fresh, 3, seed=12345)
    b = draw_controls(list(reversed(fresh)), 3, seed=12345)       # ordinea de intrare nu contează
    assert [(l.player_id, s) for l, s in a] == [(l.player_id, s) for l, s in b]
    assert len({(l.player_id, s) for l, s in a}) == 3
    c = draw_controls(fresh, 3, seed=999)
    assert [(l.player_id, s) for l, s in a] != [(l.player_id, s) for l, s in c]


# ---------------------------------------------------------------- când rulează
def games(*hours_from_now):
    return [(f"e{i}", "H", "A", "basketball_nba", NOW + timedelta(hours=h)) for i, h in enumerate(hours_from_now)]


def test_predictor_window_two_to_three_hours_before_first_game():
    g = day_games(NBA, games(2.5, 5), NOW)
    assert predictor_due(NBA, g, NOW, already_done=False)
    assert not predictor_due(NBA, g, NOW, already_done=True)
    assert not predictor_due(NBA, day_games(NBA, games(3.5), NOW), NOW, False)
    assert not predictor_due(NBA, day_games(NBA, games(1.5), NOW), NOW, False)


def test_day_games_are_us_eastern_date():
    g = day_games(NBA, games(2, 8, 30), NOW)          # 23:00 UTC, 05:00 UTC (00:00 ET a doua zi), mâine
    assert [e[0] for e in g] == ["e0"]


def test_series_changes_with_prompt_and_config():
    pv, text = prompt_version(NBA)
    assert pv.startswith("nba-v1@") and len(text) > 100
    cfg = PredictorConfig(model="claude-opus-5-5", effort="high", edge_threshold=0.03, max_searches=30)
    sid = series_id(NBA, cfg, pv)
    assert sid != series_id(NBA, PredictorConfig("claude-opus-5-5", "high", 0.04, 30), pv)
    assert sid != series_id(NBA, cfg, "nba-v1@000000000000")
    assert "claude-opus-5-5" in sid
