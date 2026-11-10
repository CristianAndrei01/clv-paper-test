"""Probabilitatea fără marjă, metoda multiplicativă (SPEC §4.2 pasul 2)."""
from __future__ import annotations

from decimal import Decimal


def no_vig_multiplicative(decimal_odds: list[Decimal | float]) -> list[float]:
    """Cote zecimale pentru toate rezultatele unei piețe (2 sau 3) -> probabilități care însumează 1."""
    if len(decimal_odds) < 2:
        raise ValueError("trebuie cel puțin două rezultate")
    implied = [1.0 / float(d) for d in decimal_odds]
    if any(p <= 0 for p in implied):
        raise ValueError(f"cote invalide: {decimal_odds}")
    total = sum(implied)
    return [p / total for p in implied]
