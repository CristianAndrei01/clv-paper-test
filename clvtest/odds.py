"""Conversii de cote și normalizarea răspunsurilor PropLine în rânduri `odds_snapshots`."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal


def american_to_decimal(american: int | None) -> Decimal | None:
    """+150 -> 2.5 ; -200 -> 1.5. Rotunjit la 5 zecimale (ca în schemă)."""
    if american is None:
        return None
    a = Decimal(american)
    if a == 0:
        raise ValueError("cota americană 0 nu există")
    dec = 1 + (a / 100 if a > 0 else Decimal(100) / -a)
    return dec.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def side_of(outcome_name: str, home_team: str, away_team: str) -> str | None:
    """Over/Under pentru jucători; 1/X/2 pentru h2h."""
    n = (outcome_name or "").strip()
    low = n.lower()
    if low == "over":
        return "over"
    if low == "under":
        return "under"
    if low == "draw":
        return "X"
    if n == home_team:
        return "1"
    if n == away_team:
        return "2"
    return None


# /odds dă la h2h și partea (home/away/draw): numele echipei din cote diferă des de cel al meciului
# („Leganes” față de „Leganés”, „Real Valladolid” față de „Valladolid”). /odds/closing și /results nu o dau.
H2H_API_SIDE = {"home": "1", "away": "2", "draw": "X"}
_TEAM_NOISE = {"fc", "cf", "cd", "ud", "sd", "ad", "ce", "sc", "ac", "as", "us", "ss", "afc", "club", "calcio", "de", "la"}


def team_tokens(name: str) -> set[str]:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return {t for t in s.split() if t not in _TEAM_NOISE and not t.isdigit()}


def _team_sim(a: str, b: str) -> int:
    """Câte cuvinte din `a` apar în `b` (egale sau unul prefix al celuilalt, de la 4 litere)."""
    tb = team_tokens(b)
    return sum(any(x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x))) for y in tb)
               for x in team_tokens(a))


def h2h_sides(names: list[str], home_team: str, away_team: str) -> dict[str, str] | None:
    """Numele rezultatelor h2h ale unei case -> 1/X/2. None dacă potrivirea nu e sigură."""
    out: dict[str, str] = {}
    teams = []
    for n in dict.fromkeys(names):
        if (n or "").strip().lower() == "draw":
            out[n] = "X"
        else:
            teams.append(n)
    if len(teams) != 2:
        return None
    a, b = teams
    straight = (_team_sim(a, home_team), _team_sim(b, away_team))
    swapped = (_team_sim(a, away_team), _team_sim(b, home_team))
    if sum(straight) > sum(swapped) and min(straight) > 0:
        out[a], out[b] = "1", "2"
    elif sum(swapped) > sum(straight) and min(swapped) > 0:
        out[a], out[b] = "2", "1"
    else:
        return None
    return out


def ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


ROW_FIELDS = (
    "event_id", "bookmaker", "market", "line_type", "player_id", "player_name", "side", "line",
    "price_american", "price_decimal", "outcome_id", "market_last_update", "book_updated_at",
    "last_change_at", "last_seen_at", "is_stale",
)


def normalize_event_odds(event: dict, markets: set[str], bookmakers: set[str]) -> tuple[list[dict], list[str]]:
    """Un eveniment din /odds -> rânduri. Păstrează doar liniile principale.

    Întoarce (rânduri, avertismente). Rezultatele cu nume necunoscut nu se pierd tăcut:
    apar în avertismente (și rămân oricum în JSON-ul brut).
    """
    rows: list[dict] = []
    warnings: list[str] = []
    home, away = event.get("home_team", ""), event.get("away_team", "")
    for book in event.get("bookmakers") or []:
        if book.get("key") not in bookmakers:
            continue
        for m in book.get("markets") or []:
            if m.get("key") not in markets or m.get("period"):
                continue
            for o in m.get("outcomes") or []:
                line_type = o.get("line_type") or m.get("line_type") or "main"
                if line_type != "main":
                    continue
                side = H2H_API_SIDE.get(o.get("side")) or side_of(o.get("name", ""), home, away)
                if side is None:
                    warnings.append(f"{event.get('id')}/{book['key']}/{m['key']}: rezultat necunoscut {o.get('name')!r}")
                    continue
                is_player = side in ("over", "under")
                rows.append({
                    "event_id": str(event["id"]),
                    "bookmaker": book["key"],
                    "market": m["key"],
                    "line_type": line_type,
                    "player_id": o.get("player_id") if is_player else None,
                    "player_name": (o.get("description") or m.get("description")) if is_player else None,
                    "side": side,
                    "line": o.get("point"),
                    "price_american": o.get("price"),
                    "price_decimal": american_to_decimal(o.get("price")),
                    "outcome_id": o.get("outcome_id"),
                    "market_last_update": ts(m.get("last_update")),
                    "book_updated_at": ts(o.get("book_updated_at")),
                    "last_change_at": ts(o.get("last_change_at")),
                    "last_seen_at": ts(o.get("last_seen_at")),
                    "is_stale": o.get("is_stale"),
                })
    return rows, warnings
