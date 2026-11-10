from datetime import datetime, timedelta, timezone

from clvtest.collector import in_periodic_window, periodic_due, prestart_due
from clvtest.config import SPORTS

NBA = SPORTS["basketball_nba"]
SERIE_B = SPORTS["soccer_italy_serie_b"]
UTC = timezone.utc


def t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_nba_game_day_is_us_eastern_date():
    now = t("2026-11-10T16:00")                        # 11:00 ET, 10 nov
    assert in_periodic_window(NBA, t("2026-11-11T00:30"), now)       # 19:30 ET, 10 nov
    assert in_periodic_window(NBA, t("2026-11-11T03:00"), now)       # 22:00 ET, 10 nov
    assert not in_periodic_window(NBA, t("2026-11-11T17:00"), now)   # 12:00 ET, 11 nov
    assert not in_periodic_window(NBA, t("2026-11-10T15:00"), now)   # deja început


def test_soccer_window_is_last_72h():
    now = t("2026-11-10T12:00")
    assert in_periodic_window(SERIE_B, t("2026-11-13T11:00"), now)
    assert not in_periodic_window(SERIE_B, t("2026-11-13T13:00"), now)


def test_periodic_every_2h_with_tolerance():
    now = t("2026-11-10T18:00")
    assert periodic_due(NBA, None, now, 3)
    assert not periodic_due(NBA, None, now, 0)                       # niciun meci azi
    assert not periodic_due(NBA, now - timedelta(minutes=100), now, 3)
    assert periodic_due(NBA, now - timedelta(minutes=112), now, 3)   # tick întârziat cu câteva minute
    assert periodic_due(NBA, now - timedelta(hours=5), now, 3)


def test_prestart_once_inside_window():
    start = t("2026-11-11T00:30")
    assert not prestart_due(NBA, start, start - timedelta(minutes=37), False)
    assert prestart_due(NBA, start, start - timedelta(minutes=30), False)
    assert prestart_due(NBA, start, start - timedelta(minutes=22), False)
    assert prestart_due(NBA, start, start - timedelta(minutes=8), False)    # tick întârziat: mai bine târziu
    assert not prestart_due(NBA, start, start - timedelta(minutes=4), False)
    assert not prestart_due(NBA, start, start - timedelta(minutes=22), True)  # deja făcut


def test_soccer_prestart_window():
    start = t("2026-11-14T14:00")
    assert prestart_due(SERIE_B, start, start - timedelta(minutes=55), False)
    assert not prestart_due(SERIE_B, start, start - timedelta(minutes=75), False)
