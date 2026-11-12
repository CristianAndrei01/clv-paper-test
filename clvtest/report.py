"""Raport (SPEC §4.4), separat pe serii (SPEC §5: seriile nu se amestecă).

Pentru grupul AI și pentru grupul de control:
  (a) pariuri cu linia nemutată: CLV mediu pe cotă cu IC 95% (bootstrap), % care au bătut închiderea,
      pe reper (Pinnacle, consens SUA) și total; verdictul se ia pe total, cifra doar-Pinnacle se arată alături;
  (b) pariuri cu linia mutată: % mutate în favoare cu IC 95% (Wilson) și mutarea medie în puncte;
      „favorabil” = limita de jos a IC peste 50%, iar dacă și controlul iese peste 50%, (b) nu contează ca dovadă;
  ROI fictiv, marcat ca nesemnificativ sub 1.000 de pariuri.
Pariurile anulate (jucătorul n-a intrat pe teren) nu intră în nicio cifră și nu se numără la țintă.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import db

TARGETS = {"basketball_nba": 300}
SOCCER = ("soccer_italy_serie_b", "soccer_spain_segunda_division")
SOCCER_TARGET = 60                 # Serie B + Segunda împreună (SPEC §2)
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20261111          # fix, ca raportul să iasă identic la fiecare rulare
ROI_MIN_BETS = 1000


# ---------------------------------------------------------------- statistică
def mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def bootstrap_ci(xs: list[float], n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED,
                 level: float = 0.95) -> tuple[float, float] | None:
    """IC percentil pentru medie. Sub 2 observații nu are sens."""
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rng.choices(xs, k=k)) / k for _ in range(n))
    lo = means[int((1 - level) / 2 * n)]
    hi = means[min(n - 1, int((1 + level) / 2 * n))]
    return lo, hi


def wilson_ci(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half, centre + half


# ---------------------------------------------------------------- date
@dataclass
class Bet:
    is_control: bool
    result: str | None             # None = nedecontat
    benchmark: str | None
    line_moved: bool | None
    line_delta: float | None
    clv: float | None
    profit: float | None
    stake: float


def load(conn) -> dict[tuple[str, str], list[Bet]]:
    rows = conn.execute(
        """select p.series_id, p.sport_key, p.is_control, s.result, s.benchmark, s.line_moved,
                  s.line_delta, s.clv, s.profit, p.stake
           from clv.predictions p left join clv.settlements s on s.prediction_id = p.id
           order by p.series_id, p.seq"""
    ).fetchall()
    out: dict[tuple[str, str], list[Bet]] = {}
    for sid, sport, ctrl, res, bm, moved, delta, clv, prof, stake in rows:
        out.setdefault((sid, sport), []).append(Bet(
            ctrl, res, bm, moved, float(delta) if delta is not None else None,
            float(clv) if clv is not None else None, float(prof) if prof is not None else None, float(stake)))
    return out


# ---------------------------------------------------------------- agregare
@dataclass
class ClvStats:
    n: int
    mean: float | None
    ci: tuple[float, float] | None
    beat_pct: float | None


@dataclass
class MovedStats:
    n: int
    favourable: int
    pct: float | None
    ci: tuple[float, float] | None
    mean_delta: float | None
    delta_ci: tuple[float, float] | None


@dataclass
class GroupStats:
    total: int
    settled: int
    void: int
    pending: int
    clv: dict[str, ClvStats] = field(default_factory=dict)      # pinnacle / us_consensus / total
    moved: MovedStats | None = None
    roi: float | None = None
    profit: float = 0.0


def clv_stats(xs: list[float]) -> ClvStats:
    return ClvStats(len(xs), mean(xs), bootstrap_ci(xs), (sum(x > 0 for x in xs) / len(xs)) if xs else None)


def group_stats(bets: list[Bet]) -> GroupStats:
    settled = [b for b in bets if b.result is not None and b.result != "void"]
    g = GroupStats(total=len(bets), settled=len(settled), void=sum(b.result == "void" for b in bets),
                   pending=sum(b.result is None for b in bets))
    same = [b for b in settled if not b.line_moved]
    for bm in ("pinnacle", "us_consensus"):
        g.clv[bm] = clv_stats([b.clv for b in same if b.benchmark == bm])
    g.clv["total"] = clv_stats([b.clv for b in same])
    moved = [b for b in settled if b.line_moved]
    deltas = [b.line_delta for b in moved]
    k = sum(d > 0 for d in deltas)
    g.moved = MovedStats(len(moved), k, k / len(moved) if moved else None, wilson_ci(k, len(moved)),
                         mean(deltas), bootstrap_ci(deltas))
    g.profit = sum(b.profit for b in settled)
    staked = sum(b.stake for b in settled)
    g.roi = g.profit / staked if staked else None
    return g


# ---------------------------------------------------------------- verdict (SPEC §8 + decizia 3)
def verdict_a(s: ClvStats) -> str:
    if s.mean is None:
        return "fără date"
    if s.mean > 0.03 and s.ci and s.ci[0] > 0:
        return "peste +3% și IC peste zero: test cu bani reali, mize mici"
    if s.mean > 0.03:
        return "peste +3%, dar IC nu e peste zero: condiția din §8 nu e îndeplinită"
    if s.mean >= 0.01:
        return "între +1% și +3%: marginal, încă 300 de pariuri"
    return "sub +1%: oprim proiectul"


def favourable_b(ai: MovedStats, ctrl: MovedStats | None) -> tuple[bool, str]:
    if ai.n == 0 or ai.ci is None:
        return False, "fără pariuri cu linie mutată"
    if ai.ci[0] <= 0.5:
        return False, f"limita de jos a IC {ai.ci[0]:.1%} nu e peste 50%"
    if ctrl and ctrl.pct is not None and ctrl.pct > 0.5:
        return False, f"IC peste 50%, dar și controlul iese {ctrl.pct:.1%} (> 50%): nu contează ca dovadă"
    return True, f"limita de jos a IC {ai.ci[0]:.1%} > 50%, controlul nu depășește 50%"


# ---------------------------------------------------------------- afișare
def pct(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "–"
    return f"{x:+.2%}" if signed else f"{x:.1%}"


def ci_str(ci, as_pct: bool = True, signed: bool = True) -> str:
    if ci is None:
        return "IC: prea puține date"
    if as_pct:
        return f"IC 95% [{pct(ci[0], signed)}, {pct(ci[1], signed)}]"
    return f"IC 95% [{ci[0]:+.2f}, {ci[1]:+.2f}]"


def render_group(name: str, g: GroupStats) -> list[str]:
    out = [f"  {name}: {g.total} pariuri — {g.settled} decontate, {g.void} anulate, {g.pending} în așteptare"]
    out.append("    (a) linie nemutată — CLV pe cotă:")
    for key, label in (("pinnacle", "Pinnacle"), ("us_consensus", "consens DK+FD"), ("total", "TOTAL")):
        s = g.clv[key]
        if s.n == 0:
            out.append(f"        {label:14s} n=0")
            continue
        out.append(f"        {label:14s} n={s.n:<4d} CLV mediu {pct(s.mean)}  {ci_str(s.ci)}  "
                   f"au bătut închiderea: {pct(s.beat_pct, False)}")
    m = g.moved
    if m.n == 0:
        out.append("    (b) linie mutată: n=0")
    else:
        out.append(f"    (b) linie mutată: n={m.n}, în favoare {m.favourable}, împotrivă {m.n - m.favourable} "
                   f"= {pct(m.pct, False)} în favoare  {ci_str(m.ci, signed=False)}")
        md = f"{m.mean_delta:+.2f}" if m.mean_delta is not None else "–"
        out.append(f"        mutare medie {md} puncte  {ci_str(m.delta_ci, as_pct=False)}")
    roi = pct(g.roi) if g.roi is not None else "–"
    out.append(f"    ROI fictiv {roi} (profit {g.profit:+.2f} lei)"
               + (f" — NESEMNIFICATIV: sub {ROI_MIN_BETS} de pariuri" if g.settled < ROI_MIN_BETS else ""))
    return out


def render_block(title: str, niche: str, bets: list[Bet], target: int | None) -> list[str]:
    ai = group_stats([b for b in bets if not b.is_control])
    ctrl = group_stats([b for b in bets if b.is_control])
    lines = [title, f"  nișă {niche}: {ai.settled} pariuri AI decontate" + (f" din ținta de {target}" if target else "")]
    lines += render_group("AI", ai)
    lines += render_group("CONTROL", ctrl)
    c = ctrl.clv["total"]
    if c.ci and c.ci[0] > 0:
        lines.append("  ATENȚIE: CLV-ul grupului de control iese clar pozitiv — măsurătoarea e suspectă (SPEC §4.2).")
    a_txt = verdict_a(ai.clv["total"])
    b_ok, b_txt = favourable_b(ai.moved, ctrl.moved)
    a_ok = a_txt.startswith("peste +3% și IC")
    final = target is not None and ai.settled >= target
    lines.append(f"  VERDICT {'(FINAL)' if final else f'(PROVIZORIU: {ai.settled}/{target or "?"}, testul nu se oprește mai devreme)'}")
    lines.append(f"    (a) total: {a_txt}")
    pin = ai.clv["pinnacle"]
    lines.append(f"        doar Pinnacle: n={pin.n}, CLV mediu {pct(pin.mean)}  {ci_str(pin.ci)}")
    lines.append(f"    (b) {'favorabil' if b_ok else 'nefavorabil'}: {b_txt}")
    lines.append(f"    Verdict pozitiv (cer ambele): {'DA' if a_ok and b_ok else 'NU'}")
    lines.append("")
    return lines


def render(data: dict[tuple[str, str], list[Bet]], chain: tuple[int, int | None]) -> str:
    checked, bad = chain
    lines = ["RAPORT CLV — test pe hârtie (miză fictivă 100 lei)",
             f"Lanțul de hash-uri al predicțiilor: {checked} rânduri verificate, "
             + ("INTACT" if bad is None else f"RUPT la seq {bad}"), ""]
    if not data:
        lines.append("Nicio predicție încă.")
    soccer: dict[str, list[tuple[str, list[Bet]]]] = {}
    for (sid, sport), bets in data.items():
        if sport in SOCCER:
            soccer.setdefault(sid.split("|", 1)[1], []).append((sport, bets))
            target = None                       # ținta de 60 e pentru ambele ligi împreună (SPEC §2)
        else:
            target = TARGETS.get(sport)
        lines += render_block(f"SERIA {sid}", sport, bets, target)
    # SPEC §2 și §8: fotbalul are o singură țintă, 60 de pariuri pe Serie B + Segunda. Seriile rămân separate
    # pe ligă (sport_key), dar cele cu aceeași configurație (model, prompt, prag) se adună aici.
    for cfg, parts in soccer.items():
        bets = [b for _, bs in parts for b in bs]
        leagues = " + ".join(sorted(sp for sp, _ in parts))
        lines += render_block(f"FOTBAL ÎMPREUNĂ ({leagues}) — configurația {cfg}",
                              "fotbal, concluzie doar orientativă (SPEC §8)", bets, SOCCER_TARGET)
    return "\n".join(lines)


def main() -> int:
    conn = db.connect("settler")
    data = load(conn)
    checked, bad = conn.execute("select checked, first_bad_seq from clv.verify_prediction_chain()").fetchone()
    conn.close()
    print(render(data, (checked, bad)))
    return 0 if bad is None else 1
