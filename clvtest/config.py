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
    # Predictor (SPEC §4.2): fereastra în ore înainte de primul meci al zilei, limita zilnică, promptul.
    predictor_hours_before: tuple[float, float] = (2.0, 3.0)
    daily_cap: int = 10
    prompt_file: str | None = None


SPORTS: dict[str, SportConfig] = {
    "basketball_nba": SportConfig(
        key="basketball_nba",
        markets="player_points",
        bookmakers=("pinnacle", "draftkings", "fanduel"),
        periodic_every_min=120,
        game_day_tz="America/New_York",
        prestart_min=(15, 30),
        predictor_hours_before=(2.0, 3.0),
        daily_cap=10,
        prompt_file="prompts/nba-v1.md",
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


@dataclass(frozen=True)
class PredictorConfig:
    """Tot ce definește o serie (SPEC §5): orice schimbare aici = serie nouă."""
    model: str
    effort: str
    edge_threshold: float
    max_searches: int
    max_tokens: int = 64000

    @classmethod
    def from_env(cls) -> "PredictorConfig":
        return cls(
            model=os.environ.get("PREDICTOR_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("PREDICTOR_EFFORT", "high"),
            edge_threshold=float(os.environ.get("EDGE_THRESHOLD", "0.03")),
            max_searches=int(os.environ.get("PREDICTOR_MAX_SEARCHES", "30")),
        )


def require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"Lipsește variabila {name} (în .env sau în GitHub Secrets)")
    return val
