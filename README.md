# clv-paper-test

Test pe hârtie: bat predicțiile unui model AI cota de închidere? Specificația completă e în `SPEC.md`.
Nu se pariază bani.

## Structură

- `sql/` — schema (`001`), regulile de integritate (`002`), rolurile (`003`), teste SQL (`sql/tests/`)
- `clvtest/` — colectorul (Faza 1), predictorul (Faza 2), decontarea și raportul (Faza 3)
- `prompts/` — prompturile versionate; `prompt_version` = nume + primele 12 caractere din sha256-ul fișierului,
  deci orice modificare a textului deschide automat o serie nouă
- `.github/workflows/tick.yml` — rularea programată (60 de tick-uri pe zi, 14:00–05:00 UTC)
- `phase0_verify.py` — verificarea API-ului din Faza 0

## Rulare locală

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m clvtest tick
```

Secretele stau în `.env` (vezi `.env.example`), niciodată în git. În GitHub Actions vin din Secrets.

Predictorul, fără să lase nimic în baza de date (inserează, apoi face ROLLBACK):

```
.venv/Scripts/python -m clvtest predict --dry-run --fake-model --force   # fără apel la Claude
.venv/Scripts/python -m clvtest predict --dry-run --force                # cu Claude (costă)
```

## Predictor (Faza 2)

O rulare pe zi de NBA, cu 2–3 ore înainte de primul meci (ziua după ora SUA de Est):

1. snapshot Pinnacle proaspăt; liniile cu ambele părți, văzute în ultimele 15 minute și fără `is_stale`,
   sunt oferite modelului cu probabilitățile fără marjă (metoda multiplicativă);
2. Claude (implicit `claude-opus-5-5`, effort `high`) caută știri cu `web_search` și întoarce pariurile prin
   unealta `record_picks`; fără fallback pe alt model, ca seria să rămână curată;
3. snapshot nou după răspuns; pariul se înregistrează la cota de atunci, avantajul se recalculează,
   iar dacă linia s-a mutat pariul se sare;
4. prag 3%, maximum 10 pariuri pe zi (cele cu avantajul cel mai mare);
5. pentru fiecare pariu, un pariu de control ales aleator din aceleași meciuri, cu seed salvat în `runs.rng_seed`.

Predictorul rulează în GitHub Actions doar dacă secretul `ANTHROPIC_API_KEY` există.

## Fotbal (Serie B + Segunda, 1X2) — construit, inactiv

Același pipeline, altă configurație (`clvtest/config.py`); doar Pinnacle. Se activează adăugând ligile în
`ACTIVE_SPORTS` din `.github/workflows/tick.yml`, după 3 zile curate de NBA.

- Colector: un snapshot la 12 ore în ultimele 72 de ore înainte de meci și unul cu 30–60 de minute înainte de start.
  Partea 1/X/2 se ia din câmpul `side` al API-ului, pentru că numele echipelor din cote diferă de cele ale meciului
  („Leganes” față de „Leganés”).
- Predictor (`prompts/soccer-v1.md`): o singură dată pe meci, cât timp meciul e la 20–28 de ore de start (24 h ± 4,
  pentru că tick-urile rulează doar 14:00–05:00 UTC); meciurile intrate în fereastră în același tick merg într-un singur
  apel. Maximum 5 pariuri AI pe etapă și ligă; etapa = săptămâna marți–luni, în ora ligii. Controlul se alege la
  întâmplare dintre rezultatele 1/X/2 ale acelorași meciuri.
- Decontare: închiderea Pinnacle (văzută cu ≤ 30 de minute înainte de start), devig pe trei rezultate; fără consens
  și fără linii mutate. Rezultatul: rezolvarea Pinnacle pe timpul regulamentar (fără piața de la pauză), verificată
  cu scorul; dacă nu se potrivesc, pariul rămâne nedecontat. Meciurile amânate sau „expired” rămân nedecontate.
- Raport: seriile rămân separate pe ligă, plus un bloc „FOTBAL ÎMPREUNĂ” cu ținta de 60 (SPEC §2).

## Decontare și raport (Faza 3)

```
.venv/Scripts/python -m clvtest settle --force --dry-run   # decontează acum, apoi ROLLBACK
.venv/Scripts/python -m clvtest report
```

Decontarea rulează o dată pe zi, la primul tick de după 14:00 UTC, pentru meciurile începute cu > 4 ore în urmă:

1. salvează `/odds/closing` și `/results` (Pinnacle, DraftKings, FanDuel), integral și ca rânduri `closing`;
2. reper: Pinnacle, dacă a fost văzută cu ≤ 30 de minute înainte de start; altfel media fără marjă
   DraftKings + FanDuel (`us_consensus`), cu aceeași regulă;
3. linia de închidere = linia luată → `clv = cota_luată × p_închidere_fără_marjă − 1`; altfel `line_moved`
   și `line_delta` cu semn (pozitiv = în favoarea pariului). La consens, linia consensului e media liniilor
   caselor; dacă diferă de linia luată, pariul e cu linie mutată;
4. rezultatul se calculează din valoarea reală a statisticii, la linia noastră; jucătorul care n-a intrat pe
   teren (`void` la toate casele) => pariu anulat: fără CLV, profit 0, nu se numără la țintă; perechea lui rămâne;
5. un pariu fără rezultat încă, sau fără nicio cotă de închidere utilizabilă, rămâne nedecontat și apare în
   `runs.details` la `pending`.

Raportul e separat pe serii; verdictul se ia pe total, cu cifra doar-Pinnacle alături, și e marcat
provizoriu până la ținta de pariuri (300 NBA).

## Integritate

- `clv.predictions` și `clv.series`: doar adăugare (trigger care blochează UPDATE/DELETE/TRUNCATE).
- Ora înregistrării, `hours_to_start`, avantajul și lanțul `row_hash` sunt calculate de server.
- O predicție e respinsă dacă meciul începe în < 5 minute, dacă avantajul e sub pragul seriei, sau dacă nu
  corespunde exact unui snapshot salvat. Prețul, `last_seen_at` și `is_stale` se iau din snapshot; cota trebuie
  să fi fost văzută în ultimele 15 minute și să nu fie `is_stale`.
- `clv.settlements`: regulile de consistență sunt constrângeri în baza de date (`sql/006_settlement.sql`):
  pariu anulat => fără CLV; linie mutată => fără CLV pe cotă și cu `line_delta ≠ 0`; altfel CLV obligatoriu.
- Verificarea lanțului: `select * from clv.verify_prediction_chain();`
- Teste: `sql/tests/integrity_tests.sql` (rulează și face rollback; rezultatul așteptat e eroarea
  `INTEGRITY_TESTS_PASSED`).

Limită cunoscută: proprietarul bazei de date (`postgres`) poate dezactiva triggerele. Rolurile aplicației nu pot.
