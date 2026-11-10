"""Configurare: un singur pipeline, configurat per sport (decizia 2 după Faza 0)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROPLINE_BASE = "https://api.prop-line.com/v1"


@dataclass(frozen=True)
class SportConfig:
    key: str
    markets: str
    bookmakers: tuple[str, ...]
    # Snapshot-uri periodice: la fiecare `periodic_every_min` minute cât timp meciul e în fereastră.
    periodic_every_min: int
    # Fereastra periodică: NBA = ziua meciului (ora SUA de Est); fotbal = ultimele N ore.
    game_day_tz: str | None = None
    lookahead_hours: float | None = None
    # Snapshot-ul unic de dinainte de start: între (min, max) minute până la start.
    prestart_min: tuple[int, int] = (15, 30)


SPORTS: dict[str, SportConfig] = {
    "basketball_nba": SportConfig(
        key="basketball_nba",
        markets="player_points",
        bookmakers=("pinnacle", "draftkings", "fanduel"),
        periodic_every_min=120,
        game_day_tz="America/New_York",
        prestart_min=(15, 30),
    ),
    # Fotbal: doar Pinnacle (decizia 1). Inactiv până când NBA rulează curat 3 zile (decizia 2).
    "soccer_italy_serie_b": SportConfig(
        key="soccer_italy_serie_b",
        markets="h2h",
        bookmakers=("pinnacle",),
        periodic_every_min=720,
        lookahead_hours=72,
        prestart_min=(30, 60),
    ),
    "soccer_spain_segunda_division": SportConfig(
        key="soccer_spain_segunda_division",
        markets="h2h",
        bookmakers=("pinnacle",),
        periodic_every_min=720,
        lookahead_hours=72,
        prestart_min=(30, 60),
    ),
}


def load_env(path: Path = ROOT / ".env") -> None:
    """Încarcă .env local (în GitHub Actions variabilele vin din secrets)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def active_sports() -> list[SportConfig]:
    keys = [k.strip() for k in os.environ.get("ACTIVE_SPORTS", "basketball_nba").split(",") if k.strip()]
    unknown = [k for k in keys if k not in SPORTS]
    if unknown:
        raise SystemExit(f"ACTIVE_SPORTS conține sporturi necunoscute: {unknown}")
    return [SPORTS[k] for k in keys]


def require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"Lipsește variabila {name} (în .env sau în GitHub Secrets)")
    return val
