"""Decontare și raport. Fixture-urile sunt răspunsuri reale (/odds/closing, /results) pentru
Orlando Magic @ Sacramento Kings, 11 nov 2026, reduse la patru jucători:
  - Anthony Black: Pinnacle proaspătă la închidere;
  - Jase Richardson: Pinnacle văzută ultima dată cu ~10 ore înainte de start -> consens DK + FD;
  - Tristan Da Silva: n-a intrat pe teren (void la toate casele);
  - Domantas Sabonis: a jucat, 0 puncte.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from clvtest.devig import no_vig_multiplicative
from clvtest.odds import ts
from clvtest.report import (
    Bet, bootstrap_ci, favourable_b, group_stats, verdict_a, wilson_ci, ClvStats, MovedStats,
)
from clvtest.settle import (
    Quote, benchmark_for, book_close, decide, in_settle_window, norm_name, parse_closing, profit,
    result_for, signed_delta,
)

FIX = Path(__file__).parent / "fixtures"
CLOSING = json.loads((FIX / "nba_closing_646677.json").read_text(encoding="utf-8"))
RESULTS = json.loads((FIX / "nba_results_646677.json").read_text(encoding="utf-8"))
COMMENCE = ts(CLOSING["commence_time"])
QUOTES, WARN = parse_closing(CLOSING, RESULTS, {}, "player_points")
PID = {q.player_name.lower(): q.player_id for q in QUOTES}
BLACK, JASE, DASILVA, SABONIS = (PID["anthony black"], PID["jase richardson"],
                                 PID["tristan da silva"], PID["domantas sabonis"])


def pred(player_id, side, line, price="1.90000", ctrl=False):
    return (1, "646677", player_id, None, side, Decimal(str(line)), Decimal(price), Decimal("100"),
            ctrl, COMMENCE, "final")


# ---------------------------------------------------------------- potrivire
def test_norm_name():
    assert norm_name("Tristan Da Silva") == norm_name("Tristan da Silva")
    assert norm_name("Jaren Jackson Jr.") == "jaren jackson"
    assert norm_name("Nikola Jokić") == "nikola jokic"
    assert norm_name("De'Andre Hunter") == "de andre hunter"


def test_every_closing_quote_gets_a_player_id_from_results():
    assert QUOTES and not WARN
    assert all(q.player_id and q.player_id.startswith("nba:") for q in QUOTES)
    assert {q.book for q in QUOTES} == {"pinnacle", "draftkings", "fanduel"}


def test_name_fallback_when_outcome_id_unknown():
    no_results = {"bookmakers": []}
    known = {("*", norm_name("Anthony Black")): "nba:X"}
    quotes, warn = parse_closing(CLOSING, no_results, known, "player_points")
    assert {q.player_id for q in quotes if q.player_name == "Anthony Black"} == {"nba:X"}
    assert warn                       # ceilalți jucători rămân fără id și apar în avertismente


# ---------------------------------------------------------------- reper
def test_pinnacle_same_line_price_clv():
    b = benchmark_for("under", 16.5, Decimal("1.90000"), BLACK, QUOTES, COMMENCE)
    pin = book_close(QUOTES, "pinnacle", BLACK, COMMENCE)
    assert b.benchmark == "pinnacle" and not b.line_moved and pin.fresh
    expected_p = no_vig_multiplicative([pin.over.price_decimal, pin.under.price_decimal])[1]
    assert b.closing_p_no_vig == pytest.approx(expected_p, abs=1e-6)
    assert b.clv == pytest.approx(1.9 * expected_p - 1, abs=1e-6)
    assert b.closing_price_american == pin.under.price_american


@pytest.mark.parametrize("side,taken,delta", [("over", 15.5, 1.0), ("under", 15.5, -1.0),
                                              ("over", 17.5, -1.0), ("under", 17.5, 1.0)])
def test_moved_line_has_signed_delta_and_no_price_clv(side, taken, delta):
    b = benchmark_for(side, taken, Decimal("1.90000"), BLACK, QUOTES, COMMENCE)
    assert b.line_moved and b.clv is None and b.closing_line == 16.5 and b.line_delta == delta


def test_signed_delta():
    assert signed_delta("over", 20.5, 21.5) == 1.0      # piața crede în mai multe puncte: over 20,5 e mai bun
    assert signed_delta("under", 20.5, 19.5) == 1.0
    assert signed_delta("under", 20.5, 21.5) == -1.0


def test_stale_pinnacle_falls_back_to_us_consensus():
    pin = book_close(QUOTES, "pinnacle", JASE, COMMENCE)
    assert pin is not None and not pin.fresh and pin.gap_min > 30
    b = benchmark_for("under", 8.5, Decimal("1.90000"), JASE, QUOTES, COMMENCE)
    assert b.benchmark == "us_consensus" and not b.line_moved
    books = [book_close(QUOTES, bk, JASE, COMMENCE) for bk in ("draftkings", "fanduel")]
    assert all(x.fresh and x.line == 8.5 for x in books)
    p = sum(x.p_under for x in books) / 2
    assert b.closing_p_no_vig == pytest.approx(p, abs=1e-6)
    assert b.clv == pytest.approx(1.9 * p - 1, abs=1e-6)


def test_consensus_line_differs_counts_as_moved():
    b = benchmark_for("over", 7.5, Decimal("1.90000"), JASE, QUOTES, COMMENCE)
    assert b.benchmark == "us_consensus" and b.line_moved and b.line_delta == 1.0 and b.clv is None


def _q(book, side, line, price, seen="2026-11-11T03:00:00Z"):
    return Quote(book, "p", "P", side, line, price, Decimal("1.90909"), None, ts(seen), ts(seen), False,
                 None, None, None)


def test_consensus_straddling_taken_line_stays_unsettled():
    quotes = [_q("draftkings", "over", 20.5, -110), _q("draftkings", "under", 20.5, -110),
              _q("fanduel", "over", 22.5, -110), _q("fanduel", "under", 22.5, -110)]
    b = benchmark_for("over", 21.5, Decimal("1.9"), "p", quotes, COMMENCE)
    assert b.benchmark == "none"


def test_no_quotes_at_all_is_none():
    assert benchmark_for("over", 10.5, Decimal("1.9"), "nobody", QUOTES, COMMENCE).benchmark == "none"


# ---------------------------------------------------------------- rezultat
def test_dnp_is_void_without_clv_and_zero_profit():
    b, res, actual, prof, details = decide(pred(DASILVA, "over", 13.5), QUOTES, RESULTS)
    assert res == "void" and actual is None and prof == 0
    assert b.clv is None and not b.line_moved and b.line_delta is None
    assert "n-a intrat" in details["reason"]


def test_zero_points_is_a_real_result_not_void():
    res, actual, _ = result_for("over", 13.5, SABONIS, RESULTS)
    assert (res, actual) == ("lost", 0.0)
    assert result_for("under", 13.5, SABONIS, RESULTS)[0] == "won"


def test_result_uses_our_line_not_the_books():
    av = result_for("over", 16.5, BLACK, RESULTS)[1]
    assert result_for("over", av - 0.5, BLACK, RESULTS)[0] == "won"
    assert result_for("over", av + 0.5, BLACK, RESULTS)[0] == "lost"
    assert result_for("over", av, BLACK, RESULTS)[0] == "push"


def test_unresolved_player_is_pending():
    assert result_for("over", 10.5, "nba:0", RESULTS) == (None, None, {"result_outcomes": 0})
    assert decide(pred("nba:0", "over", 10.5), QUOTES, RESULTS) is None


def test_profit():
    assert profit("won", Decimal("1.57803"), Decimal("100")) == Decimal("57.80")
    assert profit("lost", Decimal("1.57803"), Decimal("100")) == Decimal("-100.00")
    assert profit("push", Decimal("2"), Decimal("100")) == profit("void", Decimal("2"), Decimal("100")) == 0


def test_settle_window_is_daytime_utc():
    assert in_settle_window(datetime(2026, 11, 12, 14, 7, tzinfo=timezone.utc))
    assert not in_settle_window(datetime(2026, 11, 12, 4, 7, tzinfo=timezone.utc))
    assert not in_settle_window(datetime(2026, 11, 12, 21, 7, tzinfo=timezone.utc))


# ---------------------------------------------------------------- raport
def test_bootstrap_is_deterministic_and_brackets_the_mean():
    xs = [0.05, -0.02, 0.01, 0.03, -0.04, 0.02, 0.0, 0.06]
    a, b = bootstrap_ci(xs), bootstrap_ci(xs)
    assert a == b and a[0] < sum(xs) / len(xs) < a[1]
    assert bootstrap_ci([0.1]) is None


def test_wilson():
    lo, hi = wilson_ci(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)
    assert wilson_ci(0, 0) is None


def test_group_stats_excludes_void_and_pending():
    bets = [Bet(False, "won", "pinnacle", False, None, 0.05, 90, 100),
            Bet(False, "lost", "us_consensus", False, None, -0.01, -100, 100),
            Bet(False, "won", "pinnacle", True, 1.0, None, 90, 100),
            Bet(False, "void", "none", False, None, None, 0, 100),
            Bet(False, None, None, None, None, None, None, 100)]
    g = group_stats(bets)
    assert (g.total, g.settled, g.void, g.pending) == (5, 3, 1, 1)
    assert g.clv["total"].n == 2 and g.clv["pinnacle"].n == 1 and g.clv["us_consensus"].n == 1
    assert g.clv["total"].mean == pytest.approx(0.02)
    assert g.moved.n == 1 and g.moved.favourable == 1
    assert g.roi == pytest.approx(80 / 300)


def test_verdicts():
    assert verdict_a(ClvStats(300, 0.04, (0.01, 0.07), 0.6)).startswith("peste +3% și IC")
    assert "nu e îndeplinită" in verdict_a(ClvStats(300, 0.04, (-0.01, 0.09), 0.6))
    assert verdict_a(ClvStats(300, 0.02, (0.0, 0.04), 0.5)).startswith("între")
    assert verdict_a(ClvStats(300, 0.005, (-0.02, 0.03), 0.5)).startswith("sub")
    ai = MovedStats(100, 65, 0.65, wilson_ci(65, 100), 0.8, None)
    assert favourable_b(ai, MovedStats(100, 48, 0.48, None, 0, None))[0]
    assert not favourable_b(ai, MovedStats(100, 55, 0.55, None, 0, None))[0]   # și controlul > 50%
    assert not favourable_b(MovedStats(10, 6, 0.6, wilson_ci(6, 10), 0.5, None), None)[0]
