"""Fotbal (1X2, doar Pinnacle). Fixture-urile sunt răspunsuri reale PropLine:
  - soccer_odds_segunda.json: /odds h2h Segunda, 12 nov 2026 (4 meciuri; numele echipelor din cote
    diferă de cele ale meciului: „Leganes”/„Leganés”, „Real Valladolid”/„Valladolid”, „Real Sociedad II”/„Real Sociedad B”);
  - soccer_closing/results_685074.json: UD Las Palmas – CD Eldense 2-0 (Segunda, 9 nov);
  - soccer_closing/results_685220.json: Vicenza – Cremonese 1-4 (Serie B, 8 nov).
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from clvtest.config import SPORTS, PredictorConfig
from clvtest.devig import no_vig_multiplicative
from clvtest.odds import h2h_sides, normalize_event_odds, ts
from clvtest.predictor import (
    H2H_SIDES, ModelResult, Outcome, build_lines, draw_controls, match_games, prompt_version, record_picks_tool,
    round_key, select_picks, series_id, user_message_h2h,
)
from clvtest.report import Bet, render
from clvtest.settle import (
    benchmark_h2h, decide_h2h, decide_h2h_or_void, h2h_close, parse_closing_h2h, result_h2h, unplayed_void_due,
)

FIX = Path(__file__).parent / "fixtures"
SEG = SPORTS["soccer_spain_segunda_division"]
SERIE_B = SPORTS["soccer_italy_serie_b"]
NBA = SPORTS["basketball_nba"]
UTC = timezone.utc


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


ODDS = load("soccer_odds_segunda.json")
CLOSE_LP, RES_LP = load("soccer_closing_685074.json"), load("soccer_results_685074.json")
CLOSE_VC, RES_VC = load("soccer_closing_685220.json"), load("soccer_results_685220.json")


# ---------------------------------------------------------------- colector: partea 1/X/2
def test_collector_uses_api_side_when_team_names_differ():
    rows = []
    for ev in ODDS:
        r, w = normalize_event_odds(ev, {"h2h"}, {"pinnacle"})
        assert w == [], w
        rows += r
    assert len(rows) == 3 * len(ODDS)
    leg = {r["side"]: r["price_american"] for r in rows if r["event_id"] == "718588"}
    raw = {o["side"]: o["price"] for o in ODDS[0]["bookmakers"][0]["markets"][0]["outcomes"]}
    assert leg == {"1": raw["home"], "2": raw["away"], "X": raw["draw"]}


@pytest.mark.parametrize("names,home,away,expected", [
    (["Las Palmas", "Eldense", "Draw"], "UD Las Palmas", "CD Eldense", {"Las Palmas": "1", "Eldense": "2"}),
    (["Real Sociedad II", "Sabadell", "Draw"], "CE Sabadell FC", "Real Sociedad B",
     {"Sabadell": "1", "Real Sociedad II": "2"}),
    (["Gijon", "Valladolid", "Draw"], "Gijon", "Valladolid", {"Gijon": "1", "Valladolid": "2"}),
    (["Celta Vigo II", "Albacete", "Draw"], "Albacete Balompie", "Celta De Vigo B",
     {"Albacete": "1", "Celta Vigo II": "2"}),
    (["Cordoba", "Mallorca", "Draw"], "Mallorca", "Córdoba CF", {"Mallorca": "1", "Cordoba": "2"}),
    (["Sudtirol", "Cremonese", "Draw"], "FC Südtirol", "Cremonese", {"Sudtirol": "1", "Cremonese": "2"}),
])
def test_h2h_sides_matches_book_names_to_teams(names, home, away, expected):
    out = h2h_sides(names, home, away)
    assert out == {**expected, "Draw": "X"}


def test_h2h_sides_refuses_to_guess():
    assert h2h_sides(["Alpha", "Beta", "Draw"], "Gamma", "Delta") is None
    assert h2h_sides(["Alpha", "Draw"], "Alpha", "Beta") is None


# ---------------------------------------------------------------- predictor
NOW = datetime(2026, 11, 12, 16, 45, tzinfo=UTC)


def soccer_outcomes():
    outs, sid = [], 1
    for ev in ODDS:
        rows, _ = normalize_event_odds(ev, {"h2h"}, {"pinnacle"})
        for r in rows:
            outs.append(Outcome(sid, r["event_id"], None, None, r["side"], None, r["price_american"],
                                float(r["price_decimal"]), NOW - timedelta(minutes=3), False))
            sid += 1
    return outs


def test_build_lines_three_way_devig():
    lines = build_lines(soccer_outcomes(), NOW, H2H_SIDES)
    assert len(lines) == len(ODDS)
    for l in lines:
        assert l.sides == H2H_SIDES and l.line is None and l.player_id is None
        assert sum(l.probs.values()) == pytest.approx(1.0)
        assert [l.p(s) for s in H2H_SIDES] == pytest.approx(
            no_vig_multiplicative([l.side(s).price_decimal for s in H2H_SIDES]))


def test_build_lines_needs_all_three_outcomes():
    outs = [o for o in soccer_outcomes() if not (o.event_id == "718588" and o.side == "X")]
    assert "718588" not in {l.event_id for l in build_lines(outs, NOW, H2H_SIDES)}


def test_message_and_tool_for_h2h():
    lines = build_lines(soccer_outcomes(), NOW, H2H_SIDES)
    ids = {f"M{i + 1}": l for i, l in enumerate(lines)}
    games = [(ev["id"], ev["home_team"], ev["away_team"], SEG.key, ts(ev["commence_time"])) for ev in ODDS]
    msg = user_message_h2h(NOW, SEG.key, games, ids)
    assert "Segunda División" in msg and "Leganés vs Burgos" in msg and sum(x.startswith("M") and " | " in x for x in msg.splitlines()) == len(ODDS)
    assert record_picks_tool(H2H_SIDES)["input_schema"]["properties"]["picks"]["items"]["properties"]["side"]["enum"] \
        == ["1", "X", "2"]


def test_predictor_runs_once_per_match_about_24h_before():
    ev = [("a", "H", "A", SEG.key, NOW + timedelta(hours=19)),       # prea aproape
          ("b", "H", "A", SEG.key, NOW + timedelta(hours=21)),
          ("c", "H", "A", SEG.key, NOW + timedelta(hours=27.5)),
          ("d", "H", "A", SEG.key, NOW + timedelta(hours=30))]       # încă prea departe
    assert [e[0] for e in match_games(SEG, ev, NOW, set())] == ["b", "c"]
    assert [e[0] for e in match_games(SEG, ev, NOW, {"b"})] == ["c"]      # deja oferit modelului


def test_round_is_tuesday_to_monday_in_league_time():
    fri = datetime(2026, 11, 13, 19, 30, tzinfo=UTC)
    mon = datetime(2026, 11, 16, 19, 30, tzinfo=UTC)
    tue = datetime(2026, 11, 17, 19, 30, tzinfo=UTC)
    assert round_key(SEG, fri) == round_key(SEG, mon) == "2026-11-10"
    assert round_key(SEG, tue) == "2026-11-17"
    # luni 23:30 UTC = marți 00:30 la Roma: deja etapa următoare
    assert round_key(SERIE_B, datetime(2026, 11, 16, 23, 30, tzinfo=UTC)) == "2026-11-17"


def test_round_cap_keeps_best_edges_per_round():
    lines = build_lines(soccer_outcomes(), NOW, H2H_SIDES)
    offered = {f"M{i + 1}": l for i, l in enumerate(lines)}
    picks = [{"line_id": lid, "side": "2", "p_model": min(0.95, l.p("2") + 0.10 + i / 100), "reason": "r", "sources": []}
             for i, (lid, l) in enumerate(offered.items())]
    res = ModelResult(picks=picks, notes="", model="m", stop_reason="tool_use")
    group = {lid: ("r1" if i < 3 else "r2") for i, lid in enumerate(offered)}
    by_line = {id(l): lid for lid, l in offered.items()}
    chosen, skipped = select_picks(res, offered, lines, 0.03, 5,
                                   group_of=lambda c: group[by_line[id(c.line)]], caps={"r1": 1, "r2": 5})
    kept_r1 = [c for c in chosen if group[c.line_id] == "r1"]
    assert len(kept_r1) == 1
    assert sum(s["reason"] == "over_round_cap" for s in skipped) == 2
    assert all(c.edge >= max(x.edge for x in chosen if group[x.line_id] == "r1") or group[c.line_id] == "r2"
               for c in chosen)


def test_select_rejects_side_not_in_market():
    lines = build_lines(soccer_outcomes(), NOW, H2H_SIDES)
    res = ModelResult(picks=[{"line_id": "M1", "side": "over", "p_model": 0.6, "reason": "", "sources": []}],
                      notes="", model="m", stop_reason="tool_use")
    chosen, skipped = select_picks(res, {"M1": lines[0]}, lines, 0.03, 5)
    assert chosen == [] and skipped[0]["reason"] == "invalid"


def test_controls_from_three_way_markets_reproducible():
    lines = build_lines(soccer_outcomes(), NOW, H2H_SIDES)
    a = draw_controls(lines, 3, seed=7)
    assert a == draw_controls(list(reversed(lines)), 3, seed=7)
    assert all(s in H2H_SIDES for _, s in a)


def test_soccer_series_and_nba_series_unchanged():
    cfg = PredictorConfig(model="claude-opus-5-5", effort="high", edge_threshold=0.03, max_searches=30)
    pv, text = prompt_version(SEG)
    assert pv.startswith("soccer-v1@") and "1X2" in text
    assert series_id(SEG, cfg, pv).endswith("|cap=5/round") and series_id(SEG, cfg, pv).startswith(SEG.key + "|")
    # seria NBA în curs nu se schimbă
    assert series_id(NBA, cfg, "nba-v1@99e15831c707") == \
        "basketball_nba|claude-opus-5-5|nba-v1@99e15831c707|effort=high|edge=0.03|search=30|cap=10"


# ---------------------------------------------------------------- decontare
def pred(side, price, commence, ctrl=False, original_start=None):
    return (1, "e", None, None, side, None, Decimal(price), Decimal("100"), ctrl, commence, "final",
            original_start or commence)


def test_closing_h2h_parsed_with_sides_and_fresh():
    quotes, warn = parse_closing_h2h(CLOSE_LP)
    assert warn == [] and {q.side for q in quotes} == {"1", "X", "2"}
    commence = ts(CLOSE_LP["commence_time"])
    pin = h2h_close(quotes, "pinnacle", commence)
    assert pin.fresh and pin.gap_min == 0.0
    assert pin.quotes["1"].price_american == 114 and pin.quotes["2"].price_american == 260


def test_h2h_clv_by_hand():
    """Las Palmas (1) luat la 2,20: închidere +114 / X +251 / 2 +260."""
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    commence = ts(CLOSE_LP["commence_time"])
    imp = [1 / 2.14, 1 / 3.51, 1 / 3.60]
    p1 = imp[0] / sum(imp)
    b = benchmark_h2h("1", Decimal("2.20000"), quotes, commence)
    assert b.benchmark == "pinnacle" and not b.line_moved and b.line_delta is None and b.closing_line is None
    assert b.closing_p_no_vig == pytest.approx(p1, abs=1e-6)
    assert b.clv == pytest.approx(2.2 * p1 - 1, abs=1e-6)


def test_h2h_result_from_pinnacle_matches_score():
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    assert result_h2h("1", RES_LP, quotes)[0] == "won"           # 2-0
    assert result_h2h("X", RES_LP, quotes)[0] == "lost"
    assert result_h2h("2", RES_LP, quotes)[0] == "lost"
    q2, _ = parse_closing_h2h(CLOSE_VC)
    assert result_h2h("2", RES_VC, q2)[0] == "won"               # Vicenza – Cremonese 1-4
    assert result_h2h("1", RES_VC, q2)[0] == "lost"


def test_h2h_first_half_market_is_ignored():
    """/results are și piața h2h pe prima repriză (0-0 la pauză => Draw „won” acolo)."""
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    assert any(m.get("period") == "h1" for b in RES_LP["bookmakers"] for m in b["markets"])
    assert result_h2h("X", RES_LP, quotes)[0] == "lost"


def test_h2h_result_disagreeing_with_score_stays_pending():
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    bad = copy.deepcopy(RES_LP)
    bad["home_score"], bad["away_score"] = 0, 1
    res, _, info = result_h2h("1", bad, quotes)
    assert res is None and "nu se potrivește" in info["reason"]


def test_postponed_or_expired_match_stays_pending_before_72h():
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    expired = {**RES_LP, "status": "expired", "home_score": None, "away_score": None, "bookmakers": []}
    assert result_h2h("1", expired, quotes)[0] is None
    assert decide_h2h(pred("1", "2.2", ts(CLOSE_LP["commence_time"])), quotes, expired) is None


def test_unplayed_match_voided_after_72h():
    """Decizie 12 nov.: meci fără rezultat (amânat, „expired”) => anulat la 72 de ore după ora de start inițială."""
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    commence = ts(CLOSE_LP["commence_time"])
    expired = {**RES_LP, "status": "expired", "home_score": None, "away_score": None, "bookmakers": []}
    p = pred("1", "2.2", commence)
    assert decide_h2h_or_void(p, quotes, expired, "expired", commence + timedelta(hours=71, minutes=59)) is None
    b, res, actual, prof, details = decide_h2h_or_void(p, quotes, expired, "expired", commence + timedelta(hours=72))
    assert (res, prof, b.clv, b.line_moved, b.line_delta, b.benchmark) == ("void", Decimal("0.00"), None, False, None, "none")
    assert details["status"] == "expired" and "72 de ore" in details["reason"]


def test_rescheduled_match_counts_from_original_start():
    """Meciul amânat își mută `commence_time`; termenul de 72 de ore curge de la ora din momentul pariului."""
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    original = ts(CLOSE_LP["commence_time"])
    moved = original + timedelta(days=10)
    postponed = {**RES_LP, "status": "postponed", "home_score": None, "away_score": None, "bookmakers": []}
    p = pred("1", "2.2", moved, original_start=original)
    out = decide_h2h_or_void(p, quotes, postponed, "postponed", original + timedelta(hours=80))
    assert out is not None and out[1] == "void" and out[4]["commence_time_now"] == moved.isoformat()
    assert unplayed_void_due(original, original + timedelta(hours=72))
    assert not unplayed_void_due(original, original + timedelta(hours=71))


def test_played_match_without_usable_close_is_not_voided():
    """Doar meciurile fără rezultat se anulează; un meci jucat, cu închidere Pinnacle veche, rămâne nedecontat."""
    stale = copy.deepcopy(CLOSE_LP)
    for o in stale["bookmakers"][0]["markets"][0]["outcomes"]:
        o["closing_last_seen_at"] = "2026-11-09T18:30:00Z"
    quotes, _ = parse_closing_h2h(stale)
    commence = ts(CLOSE_LP["commence_time"])
    assert decide_h2h_or_void(pred("1", "2.2", commence), quotes, RES_LP, "final", commence + timedelta(days=5)) is None


def test_played_match_settles_normally_after_72h():
    quotes, _ = parse_closing_h2h(CLOSE_VC)
    commence = ts(CLOSE_VC["commence_time"])
    out = decide_h2h_or_void(pred("2", "2.30000", commence), quotes, RES_VC, "final", commence + timedelta(days=5))
    assert (out[0].benchmark, out[1], out[3]) == ("pinnacle", "won", Decimal("130.00"))


def test_void_match():
    quotes, _ = parse_closing_h2h(CLOSE_LP)
    void = copy.deepcopy(RES_LP)
    void["home_score"] = void["away_score"] = None
    for b in void["bookmakers"]:
        for m in b["markets"]:
            for o in m["outcomes"]:
                o["resolution"] = "void"
    b, res, actual, prof, _ = decide_h2h(pred("1", "2.2", ts(CLOSE_LP["commence_time"])), quotes, void)
    assert res == "void" and prof == 0 and b.clv is None


def test_stale_pinnacle_close_stays_unsettled():
    """Fotbal: fără consens DK+FD (decizia 1). O închidere Pinnacle văzută cu > 30 min înainte => nedecontat."""
    stale = copy.deepcopy(CLOSE_LP)
    for o in stale["bookmakers"][0]["markets"][0]["outcomes"]:
        o["closing_last_seen_at"] = "2026-11-09T18:30:00Z"
    quotes, _ = parse_closing_h2h(stale)
    commence = ts(CLOSE_LP["commence_time"])
    assert benchmark_h2h("1", Decimal("2.2"), quotes, commence).benchmark == "none"
    assert decide_h2h(pred("1", "2.2", commence), quotes, RES_LP) is None


def test_decide_h2h_full():
    quotes, _ = parse_closing_h2h(CLOSE_VC)
    b, res, actual, prof, details = decide_h2h(pred("2", "2.30000", ts(CLOSE_VC["commence_time"])), quotes, RES_VC)
    assert (b.benchmark, res, prof) == ("pinnacle", "won", Decimal("130.00"))
    assert details["score"] == [1, 4] and details["books"][0]["prices"] == {"1": 268, "X": 216, "2": 126}


# ---------------------------------------------------------------- raport: fotbalul are o singură țintă
def test_report_pools_both_leagues_with_target_60():
    b = Bet(False, "won", "pinnacle", False, None, 0.02, 100, 100)
    c = Bet(True, "lost", "pinnacle", False, None, -0.03, -100, 100)
    data = {("soccer_italy_serie_b|m|soccer-v1@x|cap=5/round", "soccer_italy_serie_b"): [b, c],
            ("soccer_spain_segunda_division|m|soccer-v1@x|cap=5/round", "soccer_spain_segunda_division"): [b, c]}
    out = render(data, (4, None))
    assert "FOTBAL ÎMPREUNĂ (soccer_italy_serie_b + soccer_spain_segunda_division)" in out
    assert "2 pariuri AI decontate din ținta de 60" in out
    assert "PROVIZORIU: 2/60" in out
