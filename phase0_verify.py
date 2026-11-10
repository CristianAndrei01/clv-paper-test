#!/usr/bin/env python3
"""Faza 0 — verifică cele 5 puncte NEVERIFICAT din SPEC §3 pe API-ul PropLine.

Doar citire. Nu scrie în nicio bază de date. Salvează fiecare răspuns brut în
phase0_out/raw/ și afișează forma exactă (trunchiată) a răspunsurilor.

Rulare:  python phase0_verify.py
Cheia se citește din .env (PROPLINE_API_KEY). Doar biblioteca standard.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "phase0_out"
RAW = OUT / "raw"
BASE = "https://api.prop-line.com/v1"

NBA = "basketball_nba"
SERIE_B = "soccer_italy_serie_b"
SEGUNDA = "soccer_spain_segunda_division"
SOCCER = [SERIE_B, SEGUNDA]

SHARP = "pinnacle"
US = ["draftkings", "fanduel"]
EXCH = "betfair_ex"
RO = ["superbet_ro", "netbet_ro", "mozzart"]
ALL_BOOKS = [SHARP, *US, EXCH, *RO]
NBA_REF = [SHARP, *US]

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------- utilitare
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def skeleton(obj, max_items: int = 2, depth: int = 0, max_depth: int = 9):
    """Forma răspunsului: liste tăiate la `max_items`, cu numărul total."""
    if depth > max_depth:
        return "…"
    if isinstance(obj, dict):
        return {k: skeleton(v, max_items, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [skeleton(x, max_items, depth + 1, max_depth) for x in obj[:max_items]]
        if len(obj) > max_items:
            out.append(f"… (+{len(obj) - max_items} elemente, total {len(obj)})")
        return out
    return obj


def show(title: str, obj, max_items: int = 2, limit: int = 6000) -> None:
    txt = json.dumps(skeleton(obj, max_items), indent=1, ensure_ascii=False)
    if len(txt) > limit:
        txt = txt[:limit] + f"\n… [trunchiat la afișare; complet în phase0_out/raw]"
    print(f"\n--- FORMA: {title} ---\n{txt}")


def h1(t: str) -> None:
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[.\-'’,]", " ", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- client API
class Client:
    def __init__(self, key: str):
        self.key = key
        self.n = 0
        self.remaining: str | None = None
        self.limit_headers: dict[str, str] = {}

    def get(self, path: str, params: dict | None = None, tag: str = "resp"):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(6):
            req = urllib.request.Request(url, headers={
                "X-API-Key": self.key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "clv-paper-test/0.1",
            })
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    status, hdrs, body = r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
            except urllib.error.HTTPError as e:
                status, hdrs, body = e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
            if hdrs.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            self.n += 1
            for k, v in hdrs.items():
                if "ratelimit" in k or "remaining" in k or k in ("retry-after", "x-tier", "x-plan"):
                    self.limit_headers[k] = v
            if "x-daily-remaining" in hdrs:
                self.remaining = hdrs["x-daily-remaining"]
            if status == 429:
                wait = float(hdrs.get("retry-after") or 2 ** attempt)
                print(f"   429 pe {path} — aștept {wait:.0f}s (Retry-After) și reîncerc")
                time.sleep(min(wait, 120))
                continue
            try:
                data = json.loads(body) if body else None
            except json.JSONDecodeError:
                data = {"_non_json": body[:2000].decode("utf-8", "replace")}
            RAW.mkdir(parents=True, exist_ok=True)
            fname = RAW / f"{self.n:02d}_{tag}.json"
            fname.write_text(json.dumps({
                "request": {"path": path, "params": params},
                "status": status,
                "elapsed_s": round(time.time() - t0, 2),
                "bytes": len(body),
                "headers": {k: v for k, v in hdrs.items() if k.startswith("x-") or k in ("retry-after", "content-type")},
                "body": data,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"   GET {path} {params} -> {status}, {len(body):,} B, {time.time() - t0:.1f}s "
                  f"[X-Daily-Remaining={hdrs.get('x-daily-remaining', '?')}]")
            return status, data
        raise RuntimeError(f"429 persistent pe {path}")


# ---------------------------------------------------------------- analiză
def iter_outcomes(event: dict):
    """(book, market, outcome, player) pentru orice răspuns odds/closing/results."""
    for b in event.get("bookmakers") or []:
        for m in b.get("markets") or []:
            for o in m.get("outcomes") or []:
                player = o.get("description") or m.get("description") or ""
                yield b["key"], m, o, player


def completed(scores: list[dict]) -> list[dict]:
    done = []
    for e in scores or []:
        st = (e.get("status") or "").lower()
        if st in {"final", "completed", "complete", "closed", "post", "finished", "ft", "full_time", "ended"} or (
            not e.get("live") and e.get("home_score") is not None
            and ts(e["commence_time"]) < NOW - timedelta(hours=3)
        ):
            done.append(e)
    return sorted(done, key=lambda e: e["commence_time"], reverse=True)


def closing_stats(ev: dict) -> dict[str, dict]:
    ct = ts(ev.get("commence_time"))
    per = defaultdict(lambda: {
        "markets": 0, "outcomes": 0, "players": set(), "points_per_player": defaultdict(set),
        "line_types": Counter(), "priced": 0, "closing_after_commence": 0, "stale": 0,
        "redacted": 0, "opening_point_differs": 0, "with_both_points": 0, "opening_before_closing": 0,
        "closing_age_s": [], "examples_moved": [],
    })
    for b in ev.get("bookmakers") or []:
        per[b["key"]]["markets"] += len(b.get("markets") or [])
    for book, m, o, player in iter_outcomes(ev):
        s = per[book]
        s["outcomes"] += 1
        s["players"].add(player)
        s["points_per_player"][(player, o.get("name"))].add(o.get("point"))
        s["line_types"][m.get("line_type")] += 1
        s["priced"] += o.get("price") is not None
        s["redacted"] += bool(o.get("redacted"))
        s["stale"] += bool(o.get("is_stale"))
        ca, oa = ts(o.get("closing_at")), ts(o.get("opening_at"))
        if ca and ct and ca > ct:
            s["closing_after_commence"] += 1
        if ca and oa and oa < ca:
            s["opening_before_closing"] += 1
        if o.get("closing_age_seconds") is not None:
            s["closing_age_s"].append(o["closing_age_seconds"])
        if o.get("opening_point") is not None and o.get("point") is not None:
            s["with_both_points"] += 1
        if o.get("opening_point") is not None and o.get("point") is not None and o["opening_point"] != o["point"]:
            s["opening_point_differs"] += 1
            if len(s["examples_moved"]) < 3:
                s["examples_moved"].append(
                    f"{player} {o.get('name')}: deschidere {o['opening_point']} @ {o.get('opening_price')} "
                    f"({o.get('opening_at')}) → închidere {o['point']} @ {o.get('price')} ({o.get('closing_at')})")
    return per


def print_closing_stats(per: dict, books: list[str]) -> None:
    for book in books:
        if book not in per:
            print(f"   {book:12s}: ABSENT din răspuns")
            continue
        s = per[book]
        multi = sum(1 for v in s["points_per_player"].values() if len(v) > 1)
        ages = sorted(s["closing_age_s"])
        med = ages[len(ages) // 2] if ages else None
        print(f"   {book:12s}: {s['markets']} piețe, {s['outcomes']} rezultate, {len(s['players'])} jucători/selecții, "
              f"cu preț={s['priced']}, redacted={s['redacted']}, is_stale={s['stale']}, line_type={dict(s['line_types'])}")
        pct = 100 * s["opening_point_differs"] / s["with_both_points"] if s["with_both_points"] else None
        print(f"   {'':12s}  closing_at după start={s['closing_after_commence']}, opening_at<closing_at={s['opening_before_closing']}, "
              f"linie mutată (opening_point≠point)={s['opening_point_differs']}/{s['with_both_points']}"
              f" = {pct and round(pct, 1)}%, (jucător,parte) cu >1 linie={multi}, "
              f"closing_age_seconds median={med}")
        for ex in s["examples_moved"]:
            print(f"   {'':12s}  ex. linie mutată: {ex}")


# ---------------------------------------------------------------- verificări
def main() -> int:
    load_env(ROOT / ".env")
    key = os.environ.get("PROPLINE_API_KEY", "").strip()
    if not key:
        print("Lipsește PROPLINE_API_KEY în .env")
        return 2
    c = Client(key)
    verdict: dict[str, str] = {}
    print(f"Faza 0 — {NOW.isoformat(timespec='seconds')}")

    # ---- 0. cheile de case există?
    h1("0. Cheile caselor (din /freshness)")
    st, fresh = c.get("/freshness", tag="freshness")
    known = {b.get("key"): b for b in (fresh or {}).get("bookmakers", [])} if isinstance(fresh, dict) else {}
    print(f"   {len(known)} case cunoscute de API")
    for k in ALL_BOOKS:
        if k in known:
            b = known[k]
            print(f"   {k:12s}: DA  (market_count={b.get('market_count')}, staleness_s={b.get('staleness_seconds')}, is_stale={b.get('is_stale')})")
        else:
            similar = [x for x in known if x and k.split("_")[0] in x]
            print(f"   {k:12s}: NU  — chei asemănătoare: {similar}")
    if known:
        show("/freshness (o casă)", {"bookmakers": [known.get(SHARP) or next(iter(known.values()))]}, 1)

    # ---- 1 + 2. Pinnacle la închidere pentru player_points + semantica price/point
    h1("1+2. NBA /odds/closing player_points — Pinnacle prezent? price = închidere? linia separată?")
    st, nba_scores = c.get(f"/sports/{NBA}/scores", {"days_from": 3}, tag="nba_scores")
    print(f"   statusuri în /scores: {Counter((e.get('status') for e in nba_scores or []))}")
    if nba_scores:
        show("/scores NBA", nba_scores[:2], 1)
    done = completed(nba_scores)[:3]
    print(f"   meciuri încheiate selectate: {[(e['away_team'] + ' @ ' + e['home_team'], e['commence_time']) for e in done]}")
    pin_found = 0
    first_pin_event = None
    moved_tot = {b: [0, 0] for b in NBA_REF}
    for i, e in enumerate(done):
        st, cl = c.get(f"/sports/{NBA}/events/{e['id']}/odds/closing",
                       {"markets": "player_points", "bookmakers": ",".join(NBA_REF)}, tag=f"nba_closing_{i}")
        if st != 200 or not isinstance(cl, dict):
            print(f"   status {st}: {str(cl)[:300]}")
            continue
        if i == 0:
            show("/odds/closing NBA player_points", cl, 2)
        per = closing_stats(cl)
        print(f"   {e['away_team']} @ {e['home_team']} (start {cl.get('commence_time')}):")
        print_closing_stats(per, NBA_REF)
        for b in NBA_REF:
            if b in per:
                moved_tot[b][0] += per[b]["opening_point_differs"]
                moved_tot[b][1] += per[b]["with_both_points"]
        if per.get(SHARP, {}).get("priced"):
            pin_found += 1
            first_pin_event = first_pin_event or (e, cl)
    print(f"\n   LINIE MUTATĂ ÎNTRE DESCHIDERE ȘI ÎNCHIDERE, total pe cele {len(done)} meciuri NBA (per rezultat over/under):")
    moved_txt = []
    for b, (mv, tot) in moved_tot.items():
        pct = f"{100 * mv / tot:.1f}%" if tot else "n/a"
        print(f"     {b:12s}: {mv}/{tot} = {pct}")
        moved_txt.append(f"{b} {pct}")
    verdict["2b"] = "linie mutată deschidere→închidere: " + ", ".join(moved_txt)
    verdict["1"] = (f"DA — Pinnacle are player_points la închidere în {pin_found}/{len(done)} meciuri"
                    if pin_found else f"NU — Pinnacle lipsește în toate cele {len(done)} meciuri verificate  ⇒ STOP")

    # verificare încrucișată: ultimul snapshot ≤ start din /odds/history == /odds/closing?
    if first_pin_event:
        e, cl = first_pin_event
        st, hist = c.get(f"/sports/{NBA}/events/{e['id']}/odds/history",
                         {"markets": "player_points", "bookmakers": SHARP, "changes_only": "true"}, tag="nba_history_pin")
        if st == 200 and isinstance(hist, dict):
            show("/odds/history NBA player_points pinnacle", hist, 2)
            ct = ts(hist.get("commence_time"))
            closing_map = {(m.get("description"), o.get("description"), o.get("name")): o
                           for b in cl["bookmakers"] if b["key"] == SHARP for m in b["markets"] for o in m["outcomes"]}
            match = mismatch = 0
            examples = []
            for b in hist.get("bookmakers", []):
                for m in b.get("markets", []):
                    for o in m.get("outcomes", []):
                        pre = [s for s in o.get("snapshots", []) if ts(s["recorded_at"]) <= ct]
                        if not pre:
                            continue
                        last = max(pre, key=lambda s: s["recorded_at"])
                        first = min(o["snapshots"], key=lambda s: s["recorded_at"])
                        co = closing_map.get((m.get("description"), o.get("description"), o.get("name")))
                        if not co:
                            continue
                        same = (last.get("price"), last.get("point")) == (co.get("price"), co.get("point"))
                        match += same
                        mismatch += not same
                        if (not same or len({s.get('point') for s in o['snapshots']}) > 1) and len(examples) < 4:
                            examples.append(
                                f"{o.get('description')} {o.get('name')}: history primul={first.get('point')}@{first.get('price')} "
                                f"ultim≤start={last.get('point')}@{last.get('price')} ({last['recorded_at']}) | "
                                f"closing={co.get('point')}@{co.get('price')} ({co.get('closing_at')}), "
                                f"opening={co.get('opening_point')}@{co.get('opening_price')}")
            print(f"   history vs closing (Pinnacle): {match} identice, {mismatch} diferite")
            for x in examples:
                print(f"     {x}")
            verdict["2"] = (f"history↔closing: {match} identice / {mismatch} diferite — vezi detalii; "
                            f"closing are o singură linie (`point`) per rezultat + `opening_point` separat")
    else:
        verdict["2"] = "neverificabil fără Pinnacle la închidere"

    # ---- 3. case de fotbal în ultimele 2–3 zile
    h1("3. Fotbal h2h — betfair_ex / superbet_ro / netbet_ro / mozzart au cote aproape de start?")
    v3 = []
    for sport in SOCCER:
        print(f"\n   [{sport}] — meciuri viitoare (/odds)")
        st, odds = c.get(f"/sports/{sport}/odds", {"markets": "h2h", "bookmakers": ",".join(ALL_BOOKS)}, tag=f"{sport}_odds")
        if st != 200 or not isinstance(odds, list):
            print(f"   status {st}: {str(odds)[:300]}")
            continue
        if odds:
            show(f"/odds {sport} h2h", odds[:1], 3)
        books_any, books_72h, n72 = Counter(), Counter(), 0
        for e in odds:
            hrs = (ts(e["commence_time"]) - NOW).total_seconds() / 3600
            keys = {b["key"] for b in e.get("bookmakers") or []}
            books_any.update(keys)
            if 0 < hrs <= 72:
                n72 += 1
                books_72h.update(keys)
        print(f"   {len(odds)} meciuri cu cote; {n72} încep în ≤72h")
        for k in [SHARP, EXCH, *RO]:
            print(f"     {k:12s}: în {books_any[k]}/{len(odds)} meciuri total, {books_72h[k]}/{n72} în ≤72h")

        print(f"\n   [{sport}] — meciuri încheiate (/odds/closing): câte case aveau cotă la start și de când")
        st, sc = c.get(f"/sports/{sport}/scores", {"days_from": 3}, tag=f"{sport}_scores")
        done_s = completed(sc)[:2]
        print(f"   meciuri încheiate selectate: {[(e['home_team'] + ' – ' + e['away_team'], e['commence_time']) for e in done_s]}")
        for i, e in enumerate(done_s):
            st, cl = c.get(f"/sports/{sport}/events/{e['id']}/odds/closing",
                           {"markets": "h2h", "bookmakers": ",".join(ALL_BOOKS)}, tag=f"{sport}_closing_{i}")
            if st != 200 or not isinstance(cl, dict):
                print(f"   status {st}: {str(cl)[:300]}")
                continue
            if i == 0 and sport == SERIE_B:
                show(f"/odds/closing {sport} h2h", cl, 3)
            ct = ts(cl["commence_time"])
            present = {b["key"]: b for b in cl.get("bookmakers") or []}
            print(f"   {e['home_team']} – {e['away_team']} (start {cl['commence_time']}):")
            for k in [SHARP, EXCH, *RO]:
                if k not in present:
                    print(f"     {k:12s}: ABSENT")
                    continue
                outs = [o for m in present[k]["markets"] for o in m["outcomes"]]
                op = [ts(o["opening_at"]) for o in outs if o.get("opening_at")]
                cs = [ts(o["closing_at"]) for o in outs if o.get("closing_at")]
                first_h = (ct - min(op)).total_seconds() / 3600 if op else None
                last_m = (ct - max(cs)).total_seconds() / 60 if cs else None
                prices = [(o["name"], o.get("price")) for o in outs]
                print(f"     {k:12s}: {len(outs)} rezultate {prices}; prima cotă cu {first_h and round(first_h, 1)}h "
                      f"înainte, ultima cu {last_m and round(last_m)} min înainte de start; "
                      f"is_stale={sum(bool(o.get('is_stale')) for o in outs)}")
                v3.append((sport, k, first_h, last_m))
    verdict["3"] = "vezi tabelele de mai sus (per ligă și casă)"

    # ---- 4. case RO la NBA player_points
    h1("4. NBA — superbet_ro / netbet_ro (și mozzart, betfair_ex) au player_points?")
    st, nba_ro = c.get(f"/sports/{NBA}/odds", {"markets": "player_points", "bookmakers": ",".join([*RO, EXCH])}, tag="nba_odds_ro")
    if st == 200 and isinstance(nba_ro, list):
        cnt = Counter()
        for e in nba_ro:
            for b in e.get("bookmakers") or []:
                cnt[b["key"]] += sum(len(m.get("outcomes") or []) for m in b.get("markets") or [])
        print(f"   {len(nba_ro)} meciuri în răspuns; rezultate player_points per casă: {dict(cnt) or 'niciuna'}")
        verdict["4"] = ("CONFIRMAT — superbet_ro și netbet_ro nu au player_points" if not (cnt["superbet_ro"] or cnt["netbet_ro"])
                        else f"INFIRMAT — {dict(cnt)}")
    else:
        print(f"   status {st}: {str(nba_ro)[:300]}")
    st, cov = c.get("/coverage/markets", {"sport": NBA, "days": 3}, tag="coverage_nba")
    if st == 200:
        show("/coverage/markets NBA (formă)", cov, 2, 3000)
        txt = json.dumps(cov)
        for k in [*RO, EXCH]:
            print(f"   {k} apare în /coverage/markets NBA: {k in txt}")

    # ---- 5. nume de jucători între case
    h1("5. NBA player_points — numele jucătorilor între case")
    st, nba = c.get(f"/sports/{NBA}/odds", {"markets": "player_points", "bookmakers": ",".join(NBA_REF)}, tag="nba_odds_ref")
    if st == 200 and isinstance(nba, list):
        if nba:
            show("/odds NBA player_points (pinnacle,draftkings,fanduel)", nba[:1], 2)
        by_id: dict[str, set] = defaultdict(set)
        by_norm: dict[str, set] = defaultdict(set)
        no_id = Counter()
        per_event = defaultdict(lambda: defaultdict(set))
        for e in nba:
            for book, m, o, player in iter_outcomes(e):
                per_event[e["id"]][book].add(player)
                if o.get("player_id"):
                    by_id[o["player_id"]].add((book, player))
                else:
                    no_id[book] += 1
                by_norm[norm_name(player)].add((book, player))
        print(f"   {len(nba)} meciuri. Jucători per meci per casă (medie):")
        for b in NBA_REF:
            vals = [len(v[b]) for v in per_event.values() if b in v]
            print(f"     {b:12s}: {round(sum(vals) / len(vals), 1) if vals else 0} jucători/meci în {len(vals)} meciuri")
        print(f"   rezultate fără player_id: {dict(no_id) or 'niciunul'}")
        diff_id = {pid: v for pid, v in by_id.items() if len({p for _, p in v}) > 1}
        print(f"   player_id cu grafii diferite între case: {len(diff_id)} din {len(by_id)}")
        for pid, v in list(diff_id.items())[:10]:
            print(f"     {pid}: {sorted(v)}")
        diff_norm = {n: v for n, v in by_norm.items() if len({p for _, p in v}) > 1}
        print(f"   nume care diferă doar prin diacritice/punctuație/sufix: {len(diff_norm)}")
        for n, v in list(diff_norm.items())[:10]:
            print(f"     {n}: {sorted(v)}")
        verdict["5"] = (f"{len(by_id)} player_id; {len(diff_id)} cu grafii diferite; "
                        f"fără player_id: {dict(no_id) or 0}")

    # ---- shape /results (deja verificat în SPEC, dar arătăm forma)
    if done:
        h1("Extra. Forma /results NBA (verificat deja în SPEC, afișat pentru schemă)")
        st, res = c.get(f"/sports/{NBA}/events/{done[0]['id']}/results",
                        {"markets": "player_points", "bookmakers": SHARP}, tag="nba_results_pin")
        if st == 200:
            show("/results NBA player_points pinnacle", res, 2)
        else:
            print(f"   status {st}: {str(res)[:300]}")

    # ---- rezumat
    h1("REZUMAT")
    for k in ["1", "2", "2b", "3", "4", "5"]:
        print(f"   [{k}] {verdict.get(k, 'neverificat (vezi erorile de mai sus)')}")
    print(f"\n   Cereri făcute: {c.n}; X-Daily-Remaining la final: {c.remaining}")
    print(f"   Headere de limită văzute: {c.limit_headers}")
    print(f"   Răspunsuri brute: {RAW}")
    return 0 if pin_found else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
