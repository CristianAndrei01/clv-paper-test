# clv-paper-test

Test pe hârtie: bat predicțiile unui model AI cota de închidere? Specificația completă e în `SPEC.md`.
Nu se pariază bani.

## Structură

- `sql/` — schema (`001`), regulile de integritate (`002`), rolurile (`003`), teste SQL (`sql/tests/`)
- `clvtest/` — colectorul (Faza 1) și predictorul (Faza 2); decontarea vine în Faza 3
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

## Integritate

- `clv.predictions` și `clv.series`: doar adăugare (trigger care blochează UPDATE/DELETE/TRUNCATE).
- Ora înregistrării, `hours_to_start`, avantajul și lanțul `row_hash` sunt calculate de server.
- O predicție e respinsă dacă meciul începe în < 5 minute, dacă avantajul e sub pragul seriei, sau dacă nu
  corespunde exact unui snapshot salvat. Prețul, `last_seen_at` și `is_stale` se iau din snapshot; cota trebuie
  să fi fost văzută în ultimele 15 minute și să nu fie `is_stale`.
- Verificarea lanțului: `select * from clv.verify_prediction_chain();`
- Teste: `sql/tests/integrity_tests.sql` (rulează și face rollback; rezultatul așteptat e eroarea
  `INTEGRITY_TESTS_PASSED`).

Limită cunoscută: proprietarul bazei de date (`postgres`) poate dezactiva triggerele. Rolurile aplicației nu pot.
