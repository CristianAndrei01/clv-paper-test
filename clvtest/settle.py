"""Decontare (SPEC §4.3). Rulează o dată pe zi, pentru meciurile încheiate.

Pentru fiecare meci cu pariuri nedecontate:
  1. ia cotele de închidere (/odds/closing) și rezultatele (/results) pentru Pinnacle, DraftKings, FanDuel;
     ambele răspunsuri se salvează integral, iar cotele de închidere și ca rânduri `odds_snapshots`
     (decizia 4: nu ne bazăm pe arhiva de 30 de zile a API-ului);
  2. reperul de închidere: Pinnacle, dacă a fost văzută cu cel mult 30 de minute înainte de start;
     altfel consensul fără marjă DraftKings + FanDuel (`us_consensus`), cu aceeași regulă de 30 de minute;
  3. linie de închidere = linia luată -> CLV pe cotă: cota_luată × p_închidere_fără_marjă − 1;
     altfel `line_moved` și `line_delta` cu semn (pozitiv = în favoarea pariului), fără CLV pe cotă;
  4. rezultatul se calculează din valoarea reală a statisticii, la linia noastră (nu la linia casei);
     jucătorul care n-a intrat pe teren (rezultat „void”, fără valoare) => pariu anulat, fără CLV
     (decizie după Faza 2: se anulează doar pariul, perechea lui rămâne);
  5. profit fictiv la miza de 100 lei.

Rulează cu rolul `clv_settler`.
"""
from __future__ import annotations

import re
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from psycopg.types.json import Jsonb

from . import db
from .config import SportConfig, require
from .devig import no_vig_multiplicative
from .odds import american_to_decimal, side_of, ts
from .propline import PropLine

PINNACLE = "pinnacle"
CONSENSUS_BOOKS = ("draftkings", "fanduel")
BOOKS = (PINNACLE, *CONSENSUS_BOOKS)
CLOSING_MAX_GAP_MIN = 30          # ultima vedere a cotei, față de start (decizie după Faza 0)
SETTLE_AFTER_START_H = 4          # un meci NBA se termină în ~2,5 ore; lăsăm marjă pentru rezultate
SETTLE_EVERY_H = 20               # „rulează zilnic”: o încercare pe zi
SETTLE_UTC_HOURS = (14, 20)       # ... la primul tick de după 14:00 UTC (după pauza de dimineață a programării)


# ---------------------------------------------------------------- potrivirea jucătorilor
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(name: str | None) -> str:
    """„Tristan Da Silva” = „Tristan da Silva”; „Jaren Jackson Jr.” = „Jaren Jackson”."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(t for t in s.split() if t not in _SUFFIXES)


@dataclass(frozen=True)
class Quote:
    """O cotă de închidere (linie principală) dintr-o casă."""
    book: str
    player_id: str | None
    player_name: str
    side: str
    line: float
    price_american: int | None
    price_decimal: Decimal | None
    outcome_id: int | None
    closing_at: datetime | None
    closing_last_seen_at: datetime | None
    is_stale: bool
    opening_price_american: int | None
    opening_line: float | None
    opening_at: datetime | None


def results_player_ids(results: dict) -> dict[tuple[str, int], str]:
    """(casă, outcome_id) -> player_id. Id-urile din /odds/closing sunt aceleași cu cele din /results."""
    out = {}
    for b in results.get("bookmakers") or []:
        for m in b.get("markets") or []:
            for o in m.get("outcomes") or []:
                if o.get("outcome_id") is not None and o.get("player_id"):
                    out[(b["key"], o["outcome_id"])] = o["player_id"]
    return out


def parse_closing(closing: dict, results: dict, known_names: dict[tuple[str, str], str],
                  market: str) -> tuple[list[Quote], list[str]]:
    """Răspunsul /odds/closing -> cote, cu player_id (din /results, apoi după nume, din snapshot-urile noastre)."""
    by_outcome = results_player_ids(results)
    home, away = closing.get("home_team", ""), closing.get("away_team", "")
    quotes, warnings = [], []
    for b in closing.get("bookmakers") or []:
        book = b.get("key")
        if book not in BOOKS:
            continue
        for m in b.get("markets") or []:
            if m.get("key") != market or m.get("period") or (m.get("line_type") or "main") != "main":
                continue
            for o in m.get("outcomes") or []:
                side = side_of(o.get("name", ""), home, away)
                if side not in ("over", "under") or o.get("point") is None:
                    continue
                name = o.get("description") or ""
                pid = (by_outcome.get((book, o.get("outcome_id")))
                       or known_names.get((book, norm_name(name)))
                       or known_names.get(("*", norm_name(name))))
                if pid is None:
                    warnings.append(f"{book}: jucător fără id {name!r}")
                quotes.append(Quote(
                    book=book, player_id=pid, player_name=name, side=side, line=float(o["point"]),
                    price_american=o.get("price"), price_decimal=american_to_decimal(o.get("price")),
                    outcome_id=o.get("outcome_id"), closing_at=ts(o.get("closing_at")),
                    closing_last_seen_at=ts(o.get("closing_last_seen_at")), is_stale=bool(o.get("is_stale")),
                    opening_price_american=o.get("opening_price"),
                    opening_line=float(o["opening_point"]) if o.get("opening_point") is not None else None,
                    opening_at=ts(o.get("opening_at")),
                ))
    return quotes, warnings


# ---------------------------------------------------------------- reperul de închidere
@dataclass(frozen=True)
class BookClose:
    book: str
    line: float
    over: Quote
    under: Quote
    p_over: float
    p_under: float
    gap_min: float | None          # cu câte minute înainte de start a fost văzută ultima dată
    fresh: bool

    def p(self, side: str) -> float:
        return self.p_over if side == "over" else self.p_under

    def quote(self, side: str) -> Quote:
        return self.over if side == "over" else self.under

    def summary(self) -> dict:
        return {"book": self.book, "line": self.line, "over": self.over.price_american,
                "under": self.under.price_american, "p_over": round(self.p_over, 6),
                "p_under": round(self.p_under, 6), "last_seen_min_before_start": self.gap_min,
                "is_stale": self.over.is_stale or self.under.is_stale, "fresh": self.fresh,
                "closing_at": max(self.over.closing_at, self.under.closing_at).isoformat()
                if self.over.closing_at and self.under.closing_at else None}


def book_close(quotes: list[Quote], book: str, player_id: str, commence: datetime) -> BookClose | None:
    """Over + Under la aceeași linie principală, pentru un jucător, într-o casă."""
    by_line: dict[float, dict[str, Quote]] = {}
    for q in quotes:
        if q.book == book and q.player_id == player_id and q.price_decimal is not None:
            by_line.setdefault(q.line, {})[q.side] = q
    pairs = [s for s in by_line.values() if "over" in s and "under" in s]
    if not pairs:
        return None
    # Phase 0: o singură linie principală pe jucător și casă; dacă apar mai multe, cea închisă ultima.
    s = max(pairs, key=lambda d: max(d["over"].closing_at or datetime.min.replace(tzinfo=commence.tzinfo),
                                     d["under"].closing_at or datetime.min.replace(tzinfo=commence.tzinfo)))
    ov, un = s["over"], s["under"]
    p_over, p_under = no_vig_multiplicative([ov.price_decimal, un.price_decimal])
    seen = [x for x in (ov.closing_last_seen_at, un.closing_last_seen_at) if x is not None]
    gap = round((commence - min(seen)).total_seconds() / 60, 1) if len(seen) == 2 else None
    fresh = gap is not None and gap <= CLOSING_MAX_GAP_MIN
    return BookClose(book, ov.line, ov, un, p_over, p_under, gap, fresh)


def signed_delta(side: str, taken: float, closing: float) -> float:
    """Pozitiv = linia s-a mutat în favoarea pariului (over 20,5 -> închidere 21,5: +1)."""
    return closing - taken if side == "over" else taken - closing


@dataclass(frozen=True)
class Benchmark:
    benchmark: str                       # pinnacle | us_consensus | none
    closing_line: float | None
    closing_price_american: int | None
    closing_price_decimal: Decimal | None
    closing_p_no_vig: float | None
    closing_at: datetime | None
    line_moved: bool
    line_delta: float | None
    clv: float | None
    books: list[dict]


def benchmark_for(side: str, taken_line: float, taken_price: Decimal, player_id: str,
                  quotes: list[Quote], commence: datetime) -> Benchmark:
    pin = book_close(quotes, PINNACLE, player_id, commence)
    seen = [pin.summary()] if pin else []
    if pin and pin.fresh:
        q = pin.quote(side)
        if pin.line == taken_line:
            p = pin.p(side)
            return Benchmark("pinnacle", pin.line, q.price_american, q.price_decimal, round(p, 6),
                             q.closing_at, False, None, round(float(taken_price) * p - 1, 6), seen)
        return Benchmark("pinnacle", pin.line, q.price_american, q.price_decimal, round(pin.p(side), 6),
                         q.closing_at, True, round(signed_delta(side, taken_line, pin.line), 2), None, seen)

    cons = [b for b in (book_close(quotes, bk, player_id, commence) for bk in CONSENSUS_BOOKS) if b]
    seen += [b.summary() for b in cons]
    fresh = [b for b in cons if b.fresh]
    if not fresh:
        return Benchmark("none", None, None, None, None, None, False, None, None, seen)
    closing_at = max((b.quote(side).closing_at for b in fresh if b.quote(side).closing_at), default=None)
    if all(b.line == taken_line for b in fresh):
        p = sum(b.p(side) for b in fresh) / len(fresh)
        return Benchmark("us_consensus", taken_line, None, None, round(p, 6), closing_at, False, None,
                         round(float(taken_price) * p - 1, 6), seen)
    # Linia consensului diferă de linia luată => linie mutată (decizie după Faza 0).
    mean_line = sum(b.line for b in fresh) / len(fresh)
    delta = round(signed_delta(side, taken_line, mean_line), 2)
    if delta == 0:
        # ex. DK 20,5 și FD 22,5 în jurul liniei luate 21,5: nici cotă la linia noastră, nici mutare
        # cu direcție. Caz nedefinit în specificație: rămâne nedecontat și apare în `pending`.
        return Benchmark("none", None, None, None, None, None, False, None, None, seen)
    return Benchmark("us_consensus", round(mean_line, 2), None, None, None, closing_at,
                     True, delta, None, seen)


# ---------------------------------------------------------------- rezultat și profit
def result_for(side: str, line: float, player_id: str, results: dict) -> tuple[str | None, float | None, dict]:
    """(won/lost/push/void sau None dacă încă nu e rezolvat, valoarea reală, informații pentru details)."""
    outs = []
    for b in results.get("bookmakers") or []:
        for m in b.get("markets") or []:
            for o in m.get("outcomes") or []:
                if o.get("player_id") == player_id:
                    outs.append((b["key"], o))
    values = [o["actual_value"] for _, o in outs if o.get("actual_value") is not None]
    info: dict = {"result_outcomes": len(outs)}
    if values:
        if len(set(values)) > 1:
            info["actual_values_disagree"] = sorted(set(values))
        # Pinnacle întâi, ca sursă; valorile sunt aceeași statistică la toate casele.
        pin = [o["actual_value"] for bk, o in outs if bk == PINNACLE and o.get("actual_value") is not None]
        av = float(pin[0] if pin else values[0])
        if av > line:
            res = "won" if side == "over" else "lost"
        elif av < line:
            res = "lost" if side == "over" else "won"
        else:
            res = "push"
        # Verificare: dacă o casă a rezolvat exact linia noastră, rezolvarea ei trebuie să coincidă.
        for bk, o in outs:
            if (o.get("point") is not None and float(o["point"]) == line
                    and side_of(o.get("name", ""), "", "") == side and o.get("resolution") in ("won", "lost", "push")
                    and o["resolution"] != res):
                info.setdefault("book_resolution_disagrees", []).append({bk: o["resolution"]})
        return res, av, info
    if outs and all(o.get("resolution") == "void" for _, o in outs):
        info["reason"] = "jucătorul n-a intrat pe teren (void la toate casele)"
        return "void", None, info
    return None, None, info


def profit(result: str, price_decimal: Decimal, stake: Decimal) -> Decimal:
    if result == "won":
        return (stake * (Decimal(price_decimal) - 1)).quantize(Decimal("0.01"))
    if result == "lost":
        return -Decimal(stake).quantize(Decimal("0.01"))
    return Decimal("0.00")


# ---------------------------------------------------------------- bază de date
PENDING_SQL = """
    select p.id, p.event_id, p.player_id, p.player_name, p.side, p.line, p.price_decimal, p.stake,
           p.is_control, e.commence_time, e.status
    from clv.predictions p
    join clv.events e on e.id = p.event_id
    left join clv.settlements s on s.prediction_id = p.id
    where s.id is null and p.sport_key = %s
      and e.commence_time < clock_timestamp() - make_interval(hours => %s)
    order by e.commence_time, p.id"""


def pending(conn, sport_key: str) -> list[tuple]:
    return conn.execute(PENDING_SQL, (sport_key, SETTLE_AFTER_START_H)).fetchall()


def known_player_names(conn, event_id: str) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        """select distinct bookmaker, player_name, player_id from clv.odds_snapshots
           where event_id = %s and player_id is not null and player_name is not null""",
        (event_id,),
    ).fetchall()
    out: dict[tuple[str, str], str] = {}
    for book, name, pid in rows:
        out[(book, norm_name(name))] = pid
        out[("*", norm_name(name))] = pid
    return out


CLOSING_COLS = (
    "raw_response_id", "run_id", "snapshot_kind", "event_id", "bookmaker", "market", "line_type",
    "player_id", "player_name", "side", "line", "price_american", "price_decimal", "outcome_id",
    "is_stale", "closing_at", "closing_last_seen_at", "opening_price_american", "opening_line", "opening_at",
)


def save_closing(conn, run_id: int, raw_id: int, event_id: str, market: str, quotes: list[Quote]) -> int:
    if not quotes:
        return 0
    with conn.cursor().copy(f"copy clv.odds_snapshots ({', '.join(CLOSING_COLS)}) from stdin") as cp:
        for q in quotes:
            cp.write_row((raw_id, run_id, "closing", event_id, q.book, market, "main", q.player_id,
                          q.player_name, q.side, q.line, q.price_american, q.price_decimal, q.outcome_id,
                          q.is_stale, q.closing_at, q.closing_last_seen_at, q.opening_price_american,
                          q.opening_line, q.opening_at))
    return len(quotes)


def insert_settlement(conn, run_id: int, pred_id: int, b: Benchmark, result: str, actual: float | None,
                      prof: Decimal, details: dict) -> None:
    conn.execute(
        """insert into clv.settlements (prediction_id, run_id, benchmark, closing_line, closing_price_american,
                  closing_price_decimal, closing_p_no_vig, closing_at, line_moved, line_delta, clv,
                  result, actual_value, profit, details)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (prediction_id) do nothing""",
        (pred_id, run_id, b.benchmark, b.closing_line, b.closing_price_american, b.closing_price_decimal,
         b.closing_p_no_vig, b.closing_at, b.line_moved, b.line_delta, b.clv, result, actual, prof,
         Jsonb(details)),
    )


def decide(pred: tuple, quotes: list[Quote], results: dict) -> tuple[Benchmark, str, float | None, Decimal, dict] | None:
    """O predicție -> decontare, sau None dacă rezultatul nu e încă disponibil."""
    pid, _eid, player_id, _name, side, line, price_dec, stake, _ctrl, commence, _status = pred
    line = float(line)
    res, actual, info = result_for(side, line, player_id, results)
    if res is None:
        return None
    b = benchmark_for(side, line, price_dec, player_id, quotes, commence)
    if res == "void":
        # Pariu anulat: fără CLV, fără linie mutată; cotele văzute rămân în details.
        b = Benchmark(b.benchmark, b.closing_line, b.closing_price_american, b.closing_price_decimal,
                      b.closing_p_no_vig, b.closing_at, False, None, None, b.books)
    elif b.benchmark == "none":
        info["reason"] = "nicio cotă de închidere utilizabilă (Pinnacle, DraftKings, FanDuel)"
        return None
    details = {"books": b.books, **info}
    return b, res, actual, profit(res, price_dec, stake), details


def settle_sport(conn, api: PropLine, run_id: int, sport: SportConfig, dry_run: bool = False) -> dict:
    d: dict = {"settled": [], "pending": [], "warnings": []}
    commit = (lambda: None) if dry_run else conn.commit
    s = api.get(f"/sports/{sport.key}/scores", {"days_from": 3})
    db.save_raw(conn, run_id, "scores", sport.key, s)
    d["scores_updated"] = db.update_scores(conn, s.data)
    commit()

    preds = pending(conn, sport.key)
    by_event: dict[str, list[tuple]] = {}
    for p in preds:
        by_event.setdefault(p[1], []).append(p)
    params = {"markets": sport.markets, "bookmakers": ",".join(BOOKS)}
    for eid, ps in by_event.items():
        status = ps[0][10]
        if status != "final":
            d["pending"] += [{"prediction_id": p[0], "event_id": eid, "reason": f"meci în starea {status!r}"} for p in ps]
            continue
        c = api.get(f"/sports/{sport.key}/events/{eid}/odds/closing", params)
        r = api.get(f"/sports/{sport.key}/events/{eid}/results", params)
        raw_c = db.save_raw(conn, run_id, "closing", sport.key, c)
        db.save_raw(conn, run_id, "results", sport.key, r)
        quotes, w = parse_closing(c.data, r.data, known_player_names(conn, eid), sport.markets)
        d["warnings"] += w
        save_closing(conn, run_id, raw_c, eid, sport.markets, quotes)
        for p in ps:
            out = decide(p, quotes, r.data)
            if out is None:
                d["pending"].append({"prediction_id": p[0], "event_id": eid, "reason": "fără rezultat sau fără cotă de închidere"})
                continue
            b, res, actual, prof, details = out
            insert_settlement(conn, run_id, p[0], b, res, actual, prof, details)
            d["settled"].append({"prediction_id": p[0], "control": p[8], "benchmark": b.benchmark,
                                 "line_moved": b.line_moved, "clv": b.clv, "line_delta": b.line_delta,
                                 "result": res, "profit": float(prof)})
        commit()
    d["warnings"] = d["warnings"][:30]
    return d


def in_settle_window(now: datetime) -> bool:
    """Ziua, ora UTC: toate meciurile NBA din noaptea trecută s-au încheiat (decizia 4: a doua zi)."""
    return SETTLE_UTC_HOURS[0] <= now.astimezone(timezone.utc).hour < SETTLE_UTC_HOURS[1]


def settle_due(conn, sport_key: str, force: bool) -> bool:
    if not pending(conn, sport_key):
        return False
    if force:
        return True
    if not in_settle_window(db.db_now(conn)):
        return False
    last = conn.execute(
        """select max(started_at) from clv.runs where component = 'settler' and status = 'ok'
             and (details->'sports'->%s) is not null and not coalesce((details->>'dry_run')::boolean, false)""",
        (sport_key,),
    ).fetchone()[0]
    return last is None or db.db_now(conn) - last >= timedelta(hours=SETTLE_EVERY_H)


def tick(sports: list[SportConfig], force: bool = False, dry_run: bool = False) -> int:
    """Decontarea zilnică. `dry_run`: totul, inclusiv inserările, apoi ROLLBACK (rămâne doar rândul din runs)."""
    conn = db.connect("settler")
    due = [s for s in sports if settle_due(conn, s.key, force)]
    if not due:
        conn.close()
        print("decontare: nimic scadent")
        return 0
    api = PropLine(require("PROPLINE_API_KEY"))
    run_id = db.start_run(conn, "settler")
    details: dict = {"sports": {}, "dry_run": dry_run}
    try:
        for sport in due:
            details["sports"][sport.key] = settle_sport(conn, api, run_id, sport, dry_run=dry_run)
        if dry_run:
            conn.rollback()
    except Exception as e:  # noqa: BLE001
        db.finish_run(conn, run_id, "error", api.requests, api.daily_remaining, details,
                      error="".join(traceback.format_exception(e))[-4000:])
        print(f"decontare run {run_id}: EROARE {e}")
        return 1
    finally:
        api.close()
    db.finish_run(conn, run_id, "ok", api.requests, api.daily_remaining, details)
    conn.close()
    print(f"decontare run {run_id}: ok, {api.requests} cereri" + (" (dry-run, rollback)" if dry_run else ""))
    print(db.dumps(details))
    return 0
