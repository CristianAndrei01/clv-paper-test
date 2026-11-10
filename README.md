# clv-paper-test

Test pe hârtie: bat predicțiile unui model AI cota de închidere? Specificația completă e în `SPEC.md`.
Nu se pariază bani.

## Structură

- `sql/` — schema (`001`), regulile de integritate (`002`), rolurile (`003`), teste SQL (`sql/tests/`)
- `clvtest/` — colectorul (Faza 1); predictorul și decontarea vin în Fazele 2–3
- `.github/workflows/tick.yml` — rularea programată (56 de tick-uri pe zi)
- `phase0_verify.py` — verificarea API-ului din Faza 0

## Rulare locală

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m clvtest tick
```

Secretele stau în `.env` (vezi `.env.example`), niciodată în git. În GitHub Actions vin din Secrets.

## Integritate

- `clv.predictions` și `clv.series`: doar adăugare (trigger care blochează UPDATE/DELETE/TRUNCATE).
- Ora înregistrării, `hours_to_start`, avantajul și lanțul `row_hash` sunt calculate de server.
- O predicție e respinsă dacă meciul începe în < 5 minute, dacă cota e `is_stale` sau mai veche de 15 minute.
- Verificarea lanțului: `select * from clv.verify_prediction_chain();`
- Teste: `sql/tests/integrity_tests.sql` (rulează și face rollback; rezultatul așteptat e eroarea
  `INTEGRITY_TESTS_PASSED`).

Limită cunoscută: proprietarul bazei de date (`postgres`) poate dezactiva triggerele. Rolurile aplicației nu pot.
