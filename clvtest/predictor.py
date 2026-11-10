"""Predictor (SPEC §4.2). O singură rulare pe zi de NBA, cu 2–3 ore înainte de primul meci.

Fluxul unei rulări:
  1. snapshot Pinnacle proaspăt (rolul colector) -> liniile oferite modelului;
  2. apel Claude cu căutare web; modelul întoarce pariurile prin unealta `record_picks`;
  3. snapshot Pinnacle nou după răspuns (decizie după Faza 1): pariul se înregistrează la cota
     de atunci, avantajul se recalculează; dacă linia s-a mutat, pariul se sare;
  4. pentru fiecare pariu AI, un pariu de control ales la întâmplare (seed salvat);
  5. inserare în `clv.predictions` — trigger-ul verifică din nou toate regulile.

Predictorul folosește rolul `clv_predictor`, care vede doar meciuri viitoare și nu are acces la decontări.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from . import db
from .collector import store_odds_snapshot
from .config import ROOT, PredictorConfig, SportConfig, require
from .devig import no_vig_multiplicative
from .propline import PropLine

FRESH_MINUTES = 15
PINNACLE = "pinnacle"
MAX_CONTINUATIONS = 8


# ---------------------------------------------------------------- linii
@dataclass(frozen=True)
class Outcome:
    snapshot_id: int
    event_id: str
    player_id: str | None
    player_name: str | None
    side: str
    line: float
    price_american: int
    price_decimal: float
    last_seen_at: datetime
    is_stale: bool


@dataclass(frozen=True)
class Line:
    """O linie (meci, jucător, puncte) cu ambele părți și probabilitățile fără marjă."""
    event_id: str
    player_id: str | None
    player_name: str | None
    line: float
    over: Outcome
    under: Outcome
    p_over: float
    p_under: float

    def side(self, side: str) -> Outcome:
        return self.over if side == "over" else self.under

    def p(self, side: str) -> float:
        return self.p_over if side == "over" else self.p_under

    @property
    def key(self) -> tuple:
        return (self.event_id, self.player_id or self.player_name)


def build_lines(outcomes: list[Outcome], now: datetime) -> list[Line]:
    """Perechi over/under la aceeași linie; doar cote actuale (last_seen_at ≤ 15 min, fără is_stale)."""
    by_key: dict[tuple, dict[str, Outcome]] = {}
    for o in outcomes:
        if o.is_stale or now - o.last_seen_at > timedelta(minutes=FRESH_MINUTES):
            continue
        by_key.setdefault((o.event_id, o.player_id or o.player_name, o.line), {})[o.side] = o
    lines = []
    for (eid, _, ln), sides in by_key.items():
        if "over" in sides and "under" in sides:
            ov, un = sides["over"], sides["under"]
            p_over, p_under = no_vig_multiplicative([ov.price_decimal, un.price_decimal])
            lines.append(Line(eid, ov.player_id, ov.player_name, ln, ov, un, p_over, p_under))
    lines.sort(key=lambda l: (l.event_id, l.player_name or "", l.line))
    return lines


def load_outcomes(conn, market: str, event_ids: list[str], since: datetime) -> list[Outcome]:
    """Cel mai recent snapshot Pinnacle de la `since` încoace, din vederea predictorului."""
    rows = conn.execute(
        """select distinct on (event_id, coalesce(player_id, player_name), side)
                  id, event_id, player_id, player_name, side, line, price_american, price_decimal,
                  last_seen_at, coalesce(is_stale, false)
           from clv.predictor_odds
           where bookmaker = %s and market = %s and line_type = 'main'
             and event_id = any(%s) and snapshot_at >= %s and snapshot_kind = 'predictor'
           order by event_id, coalesce(player_id, player_name), side, snapshot_at desc""",
        (PINNACLE, market, event_ids, since),
    ).fetchall()
    return [Outcome(r[0], r[1], r[2], r[3], r[4], float(r[5]), r[6], float(r[7]), r[8], r[9]) for r in rows]


# ---------------------------------------------------------------- când rulează
def game_day(sport: SportConfig, now: datetime):
    return now.astimezone(ZoneInfo(sport.game_day_tz)).date()


def day_games(sport: SportConfig, events: list[tuple], now: datetime) -> list[tuple]:
    """Meciurile din ziua curentă (ora SUA de Est) care nu au început."""
    d = game_day(sport, now)
    tz = ZoneInfo(sport.game_day_tz)
    return [e for e in events if e[4] > now and e[4].astimezone(tz).date() == d]


def predictor_due(sport: SportConfig, games: list[tuple], now: datetime, already_done: bool) -> bool:
    if already_done or not games:
        return False
    first = min(e[4] for e in games)
    lo, hi = sport.predictor_hours_before
    return timedelta(hours=lo) <= first - now <= timedelta(hours=hi)


# ---------------------------------------------------------------- seria
def prompt_version(sport: SportConfig) -> tuple[str, str]:
    path = ROOT / sport.prompt_file
    text = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{Path(sport.prompt_file).stem}@{sha}", text


def series_id(sport: SportConfig, cfg: PredictorConfig, pversion: str) -> str:
    return (f"{sport.key}|{cfg.model}|{pversion}|effort={cfg.effort}"
            f"|edge={cfg.edge_threshold:g}|search={cfg.max_searches}|cap={sport.daily_cap}")


def ensure_series(conn, sid: str, sport: SportConfig, cfg: PredictorConfig, pversion: str) -> None:
    conn.execute(
        """insert into clv.series (id, sport_key, model, prompt_version, edge_threshold, config)
           values (%s, %s, %s, %s, %s, %s) on conflict (id) do nothing""",
        (sid, sport.key, cfg.model, pversion, cfg.edge_threshold,
         Jsonb({"effort": cfg.effort, "max_searches": cfg.max_searches, "max_tokens": cfg.max_tokens,
                "daily_cap": sport.daily_cap, "prompt_file": sport.prompt_file,
                "fresh_minutes": FRESH_MINUTES, "web_search_tool": "web_search_20260209"})),
    )


# ---------------------------------------------------------------- model
RECORD_PICKS_TOOL = {
    "name": "record_picks",
    "description": "Record the final picks. Call exactly once, at the end, with every pick (an empty list if none).",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_id": {"type": "string"},
                        "side": {"type": "string", "enum": ["over", "under"]},
                        "p_model": {"type": "number"},
                        "reason": {"type": "string"},
                        "sources": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["line_id", "side", "p_model", "reason", "sources"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["picks", "notes"],
        "additionalProperties": False,
    },
}


def user_message(now: datetime, games: list[tuple], lines: list[Line], ids: dict[str, Line]) -> str:
    game_label = {e[0]: f"{e[2]} @ {e[1]}" for e in games}
    out = [f"Current time (UTC): {now:%Y-%m-%d %H:%M}", "", "Games today (tip-off, UTC):"]
    for e in sorted(games, key=lambda e: e[4]):
        out.append(f"- {e[2]} @ {e[1]} — {e[4]:%Y-%m-%d %H:%M}")
    out += ["", "Pinnacle player points lines:",
            "id | game | player | line | over (decimal) | under (decimal) | p_over no-vig | p_under no-vig"]
    for lid, l in ids.items():
        out.append(f"{lid} | {game_label.get(l.event_id, l.event_id)} | {l.player_name} | {l.line:g} | "
                   f"{l.over.price_decimal:.3f} | {l.under.price_decimal:.3f} | {l.p_over:.3f} | {l.p_under:.3f}")
    return "\n".join(out)


@dataclass
class ModelResult:
    picks: list[dict]
    notes: str
    model: str
    stop_reason: str
    usage: dict = field(default_factory=dict)
    search_urls: list[str] = field(default_factory=list)
    text: str = ""


class ModelRefused(RuntimeError):
    pass


def _add_usage(total: dict, u) -> None:
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        total[k] = total.get(k, 0) + (getattr(u, k, None) or 0)
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        total["web_search_requests"] = total.get("web_search_requests", 0) + (getattr(stu, "web_search_requests", 0) or 0)


def call_model(cfg: PredictorConfig, system_prompt: str, message: str) -> ModelResult:
    """Un singur răspuns final prin `record_picks`. Fără fallback pe alt model: seria rămâne curată."""
    import anthropic  # importat aici ca testele fără cheie să nu depindă de el

    client = anthropic.Anthropic(api_key=require("ANTHROPIC_API_KEY"), max_retries=3, timeout=1200)
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": cfg.max_searches},
             RECORD_PICKS_TOOL]
    messages: list[dict] = [{"role": "user", "content": message}]
    usage: dict = {}
    urls: list[str] = []
    texts: list[str] = []
    nudged = False
    for _ in range(MAX_CONTINUATIONS):
        with client.messages.stream(
            model=cfg.model, max_tokens=cfg.max_tokens, system=system_prompt,
            thinking={"type": "adaptive"}, output_config={"effort": cfg.effort},
            tools=tools, messages=messages,
        ) as stream:
            resp = stream.get_final_message()
        _add_usage(usage, resp.usage)
        for b in resp.content:
            if b.type == "web_search_tool_result" and isinstance(b.content, list):
                urls += [r.url for r in b.content if getattr(r, "url", None)]
            elif b.type == "text":
                texts.append(b.text)

        if resp.stop_reason == "refusal":
            raise ModelRefused(f"modelul a refuzat: {getattr(resp, 'stop_details', None)}")
        call = next((b for b in resp.content if b.type == "tool_use" and b.name == "record_picks"), None)
        if call is not None:
            return ModelResult(picks=list(call.input.get("picks", [])), notes=call.input.get("notes", ""),
                               model=resp.model, stop_reason=resp.stop_reason, usage=usage,
                               search_urls=urls, text="\n".join(texts)[-4000:])
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason == "end_turn" and not nudged:
            nudged = True
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": "Call record_picks now with your final picks (an empty list if none)."})
            continue
        raise RuntimeError(f"răspuns fără record_picks (stop_reason={resp.stop_reason})")
    raise RuntimeError("prea multe continuări pause_turn")


def fake_model(lines_by_id: dict[str, Line]) -> ModelResult:
    """Doar pentru --dry-run: alege două linii cu un avantaj artificial, fără niciun apel API."""
    picks = []
    for lid, l in list(lines_by_id.items())[:2]:
        side = "over" if l.p_over <= l.p_under else "under"
        picks.append({"line_id": lid, "side": side, "p_model": min(0.95, l.p(side) + 0.08),
                      "reason": "test (dry-run)", "sources": []})
    return ModelResult(picks=picks, notes="fake", model="fake", stop_reason="fake")


# ---------------------------------------------------------------- selecția finală
@dataclass
class Candidate:
    line: Line          # linia din snapshot-ul de după model
    side: str
    p_model: float
    edge: float
    reason: str
    sources: list[dict]
    line_id: str


def select_picks(result: ModelResult, offered: dict[str, Line], fresh: list[Line],
                 threshold: float, cap: int) -> tuple[list[Candidate], list[dict]]:
    """Potrivește pariurile modelului cu snapshot-ul nou, aplică pragul și limita zilnică."""
    fresh_by_key: dict[tuple, Line] = {l.key: l for l in fresh}
    found = set(result.search_urls)
    chosen, skipped, seen = [], [], set()
    for p in result.picks:
        lid, side = p.get("line_id"), p.get("side")
        old = offered.get(lid)
        try:
            pm = float(p.get("p_model"))
        except (TypeError, ValueError):
            pm = -1.0
        if old is None or side not in ("over", "under") or not 0 < pm < 1:
            skipped.append({"line_id": lid, "reason": "invalid"})
            continue
        if old.key in seen:
            skipped.append({"line_id": lid, "reason": "duplicate_line"})
            continue
        seen.add(old.key)
        new = fresh_by_key.get(old.key)
        if new is None:
            skipped.append({"line_id": lid, "reason": "not_fresh_after_model"})
            continue
        if new.line != old.line:
            skipped.append({"line_id": lid, "reason": "line_changed", "from": old.line, "to": new.line})
            continue
        edge = pm * new.side(side).price_decimal - 1
        if edge < threshold:
            skipped.append({"line_id": lid, "reason": "edge_below_threshold", "edge": round(edge, 4),
                            "price_seen": old.side(side).price_decimal, "price_now": new.side(side).price_decimal})
            continue
        sources = [{"url": u, "found_in_search": u in found} for u in p.get("sources") or []]
        chosen.append(Candidate(new, side, pm, edge, (p.get("reason") or "")[:2000], sources, lid))
    chosen.sort(key=lambda c: c.edge, reverse=True)
    for c in chosen[cap:]:
        skipped.append({"line_id": c.line_id, "reason": "over_daily_cap", "edge": round(c.edge, 4)})
    return chosen[:cap], skipped


def control_pool(fresh: list[Line]) -> list[tuple[Line, str]]:
    """Toate (linie, parte) eligibile, într-o ordine deterministă, ca alegerea să fie reproductibilă din seed."""
    pool = [(l, s) for l in fresh for s in ("over", "under")]
    pool.sort(key=lambda x: (x[0].event_id, x[0].player_id or "", x[0].player_name or "", x[0].line, x[1]))
    return pool


def draw_controls(fresh: list[Line], k: int, seed: int) -> list[tuple[Line, str]]:
    pool = control_pool(fresh)
    return random.Random(seed).sample(pool, min(k, len(pool)))


# ---------------------------------------------------------------- inserare
def insert_prediction(conn, *, sid, run_id, sport, cfg, pversion, line: Line, side, p_model, reason,
                      sources, is_control, pair_key, seed) -> int:
    o = line.side(side)
    return conn.execute(
        """insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
               player_id, player_name, line, side, bookmaker, price_american, price_decimal, odds_snapshot_id,
               odds_last_update, odds_is_stale, p_no_vig, p_model, reason, sources, is_control, pair_key,
               rng_seed, seq, prev_hash, row_hash, hours_to_start, created_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, 0, '', '', 0, now())
           returning id""",
        (sid, run_id, pversion, cfg.model, line.event_id, sport.key, sport.markets,
         line.player_id, line.player_name, line.line, side, PINNACLE, o.price_american, o.price_decimal,
         o.snapshot_id, o.last_seen_at, o.is_stale, round(line.p(side), 6),
         None if is_control else round(p_model, 6), reason, Jsonb(sources), is_control, pair_key, seed),
    ).fetchone()[0]


# ---------------------------------------------------------------- rulare
def run_sport(sport: SportConfig, cfg: PredictorConfig, *, dry_run: bool, fake: bool, force: bool) -> int:
    pconn = db.connect("predictor")
    cconn = db.connect("collector")
    api = PropLine(require("PROPLINE_API_KEY"))
    pversion, system_prompt = prompt_version(sport)
    sid = series_id(sport, cfg, pversion)

    now = db.db_now(pconn)
    events = pconn.execute(
        "select id, home_team, away_team, sport_key, commence_time from clv.predictor_events where sport_key = %s",
        (sport.key,),
    ).fetchall()
    games = day_games(sport, events, now)
    gday = str(game_day(sport, now))
    done = pconn.execute(
        """select exists(select 1 from clv.predictor_runs
                         where status = 'ok' and details->>'sport_key' = %s and details->>'game_day' = %s
                           and coalesce(details->>'dry_run', 'false') <> 'true')
               or exists(select 1 from clv.predictions where series_id = %s and event_id = any(%s))""",
        (sport.key, gday, sid, [e[0] for e in games]),
    ).fetchone()[0]
    if not force and not predictor_due(sport, games, now, done):
        pconn.close(); cconn.close(); api.close()
        return 0

    seed = secrets.randbits(63)
    run_id = pconn.execute(
        "insert into clv.runs (component, rng_seed, gh_run_id, details) values ('predictor', %s, %s, %s) returning id",
        (seed, os.environ.get("GITHUB_RUN_ID"), Jsonb({"sport_key": sport.key, "game_day": gday, "dry_run": dry_run})),
    ).fetchone()[0]
    pconn.commit()
    details: dict = {"sport_key": sport.key, "game_day": gday, "dry_run": dry_run, "fake_model": fake,
                     "series_id": sid, "games": len(games)}
    usage: dict = {}
    try:
        # seria se creează în aceeași tranzacție cu predicțiile: la dry-run sau eroare nu rămâne nimic
        ensure_series(pconn, sid, sport, cfg, pversion)
        event_ids = [e[0] for e in games]

        # 1. snapshot proaspăt -> liniile oferite modelului
        t1 = db.db_now(pconn)
        store_odds_snapshot(cconn, api, run_id, sport, "predictor")
        lines1 = build_lines(load_outcomes(pconn, sport.markets, event_ids, t1), db.db_now(pconn))
        offered = {f"L{i + 1}": l for i, l in enumerate(lines1)}
        details["lines_offered"] = len(offered)
        if not offered:
            raise RuntimeError("nicio linie Pinnacle actuală pentru meciurile zilei")

        # 2. modelul
        message = user_message(db.db_now(pconn), games, lines1, offered)
        result = fake_model(offered) if fake else call_model(cfg, system_prompt, message)
        usage = result.usage
        if not fake and result.model != cfg.model:
            raise RuntimeError(f"a răspuns alt model ({result.model}) decât cel al seriei ({cfg.model})")
        details.update(model_answered=result.model, picks_returned=len(result.picks), notes=result.notes[:2000],
                       search_result_urls=len(set(result.search_urls)), model_text_tail=result.text[-1500:],
                       raw_picks=result.picks)

        # 3. snapshot nou după model; pariurile se iau la cota de acum
        t2 = db.db_now(pconn)
        store_odds_snapshot(cconn, api, run_id, sport, "predictor")
        lines2 = build_lines(load_outcomes(pconn, sport.markets, event_ids, t2), db.db_now(pconn))
        already = pconn.execute(
            """select count(*) from clv.predictions
               where series_id = %s and not is_control and event_id = any(%s)""",
            (sid, event_ids),
        ).fetchone()[0]
        chosen, skipped = select_picks(result, offered, lines2, cfg.edge_threshold, max(0, sport.daily_cap - already))

        # 4–5. control + inserare, fiecare pereche într-un savepoint (tranzacția e deja deschisă,
        #      deci pconn.transaction() creează SAVEPOINT, nu COMMIT)
        controls = draw_controls(lines2, len(chosen), seed)
        recorded = []
        for cand, (cline, cside) in zip(chosen, controls):
            pair = uuid.uuid4()
            try:
                with pconn.transaction():
                    ai_id = insert_prediction(pconn, sid=sid, run_id=run_id, sport=sport, cfg=cfg, pversion=pversion,
                                              line=cand.line, side=cand.side, p_model=cand.p_model, reason=cand.reason,
                                              sources=cand.sources, is_control=False, pair_key=pair, seed=seed)
                    ctl_id = insert_prediction(pconn, sid=sid, run_id=run_id, sport=sport, cfg=cfg, pversion=pversion,
                                               line=cline, side=cside, p_model=None, reason=None, sources=[],
                                               is_control=True, pair_key=pair, seed=seed)
                recorded.append({"ai": ai_id, "control": ctl_id, "line_id": cand.line_id, "edge": round(cand.edge, 4)})
            except Exception as e:  # noqa: BLE001 — trigger-ul a respins perechea: se notează, nu se ocolește
                skipped.append({"line_id": cand.line_id, "reason": "rejected_by_db", "error": str(e)[:300]})
        details.update(recorded=recorded, skipped=skipped, lines_after_model=len(lines2))

        if dry_run:
            pconn.rollback()          # nimic nu rămâne în predictions/series
            details["rolled_back"] = True
        else:
            pconn.commit()
        _finish(pconn, run_id, "ok", api, usage, details)
        print(f"predictor run {run_id}: {len(recorded)} perechi înregistrate{' (dry-run, rollback)' if dry_run else ''}")
        print(db.dumps({k: v for k, v in details.items() if k not in ('model_text_tail', 'raw_picks')}))
        return 0
    except Exception as e:  # noqa: BLE001
        pconn.rollback()
        details["error"] = "".join(traceback.format_exception(e))[-4000:]
        _finish(pconn, run_id, "error", api, usage, details, error=str(e)[:2000])
        print(f"predictor run {run_id}: EROARE {e}")
        return 1
    finally:
        api.close()
        cconn.close()
        pconn.close()


def _finish(conn, run_id, status, api: PropLine, usage: dict, details: dict, error: str | None = None) -> None:
    conn.rollback()
    conn.execute(
        """update clv.runs set finished_at = clock_timestamp(), status = %s, api_requests = %s,
                  api_daily_remaining = %s, anthropic_input_tokens = %s, anthropic_output_tokens = %s,
                  anthropic_web_searches = %s, details = %s, error = %s where id = %s""",
        (status, api.requests, api.daily_remaining,
         (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
          + usage.get("cache_creation_input_tokens", 0)) or None,
         usage.get("output_tokens"), usage.get("web_search_requests"),
         Jsonb(json.loads(db.dumps(details))), error, run_id),
    )
    conn.commit()


def tick(sports: list[SportConfig], *, dry_run: bool = False, fake: bool = False, force: bool = False) -> int:
    cfg = PredictorConfig.from_env()
    rc = 0
    for sport in sports:
        if not sport.prompt_file:
            continue
        rc |= run_sport(sport, cfg, dry_run=dry_run, fake=fake, force=force)
    return rc
