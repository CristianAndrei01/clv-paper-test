# SPEC — Test pe hârtie: bate AI-ul cota de închidere?

Versiune 1.0 · 10 noiembrie 2026

## 1. Ce construim și de ce

Un sistem mic care rulează singur și răspunde la o singură întrebare: **predicțiile unui model AI, făcute înainte de meci, bat cota de închidere a pieței?**

Măsura este CLV (closing line value), nu profitul. Profitul pe câteva sute de pariuri e în mare parte noroc; CLV-ul arată avantajul real mult mai repede.

Nu se pariază bani. Totul este simulare, cu miză fictivă de 100 lei pe pariu.

### Ce NU construim acum
- plasare de pariuri reale sau integrare cu vreo casă;
- dashboard (vine după minimum două săptămâni de date);
- modele statistice proprii (Elo, xG etc.);
- pariuri live.

## 2. Cele două nișe

| Nișă | sport_key | Piață | Volum țintă |
|---|---|---|---|
| NBA, puncte pe jucător | `basketball_nba` | `player_points` | 300 de pariuri |
| Serie B (Italia) | `soccer_italy_serie_b` | `h2h` | 60 de pariuri în total cu Segunda |
| Segunda División (Spania) | `soccer_spain_segunda_division` | `h2h` | |

## 3. Sursa de date: PropLine API

Plan: Hobby (5.000 cereri/zi, istoric 30 de zile).

- Base URL: `https://api.prop-line.com/v1`
- Autentificare: header `X-API-Key` (cheia stă în `.env`, niciodată în cod sau în git)
- Specificație completă: `https://api.prop-line.com/openapi.json` — **citește-o înainte de a scrie cod**, câmpurile de mai jos sunt verificate doar parțial
- Cotele vin în format american; convertește-le în zecimal la ingestie și păstrează și valoarea brută

### Endpoint-uri folosite

| Scop | Endpoint |
|---|---|
| Meciuri viitoare | `GET /sports/{sport_key}/events` |
| Cote curente (toate meciurile) | `GET /sports/{sport_key}/odds?markets=...&bookmakers=...` |
| Scoruri și meciuri încheiate | `GET /sports/{sport_key}/scores?days_from=3` |
| Cote de deschidere și închidere | `GET /sports/{sport_key}/events/{event_id}/odds/closing?markets=...&bookmakers=...` |
| Rezultatul fiecărui pariu | `GET /sports/{sport_key}/events/{event_id}/results?markets=...&bookmakers=...` |
| Istoric de cote (rezervă) | `GET /sports/{sport_key}/events/{event_id}/odds/history` |

### Case (bookmaker keys)

- Reper de preț: `pinnacle`
- Rezervă pentru reper la NBA: `draftkings`, `fanduel`
- Exchange: `betfair_ex`
- Case licențiate în România: `superbet_ro`, `netbet_ro`, `mozzart`

Cere mereu doar casele de mai sus prin parametrul `bookmakers`. Fără filtru, un răspuns pentru o zi de NBA are 600.000–950.000 de caractere.

### Limite
- 5.000 cereri/zi; rafală de 20, apoi 10 pe secundă; maximum 20 de cereri simultane
- apelurile de tip closing/history: maximum 1.000 de evenimente distincte pe zi
- headerele `X-Daily-Remaining` și `Retry-After` se respectă; la 429 se așteaptă și se reîncearcă

### Verificat pe date reale (10 nov 2026)
- `/odds` pentru NBA cu `player_points`: Pinnacle are circa 11 jucători pe meci, DraftKings și FanDuel câte 12
- `/odds` pentru Serie B: Pinnacle are cote cu 10 zile înainte de meci
- `/odds/closing`: întoarce `opening_price`, `opening_at`, `closing_at` și `price`
- `/results`: întoarce rezultatul (won/lost/push) și valoarea reală a statisticii, inclusiv pentru Pinnacle

### NEVERIFICAT — de confirmat în Faza 0
1. Că `pinnacle` apare în răspunsul `/odds/closing` pentru `player_points` (am văzut doar DraftKings; răspunsul era trunchiat).
2. Că `price` din `/odds/closing` este într-adevăr cota de închidere și dacă linia de închidere apare separat de cea de deschidere.
3. Că `betfair_ex`, `superbet_ro`, `netbet_ro` și `mozzart` au cote la Serie B și Segunda în ultimele 2–3 zile înainte de meci.
4. Că `superbet_ro` și `netbet_ro` nu au deloc `player_points` la NBA.
5. Cum diferă numele jucătorilor între case (trebuie un pas de normalizare).

## 4. Componente

### 4.1 Colector
Rulează programat și salvează cotele brute.

- NBA: la fiecare 2 ore în ziua meciului și o dată cu 15–30 de minute înainte de start
- Fotbal: de două ori pe zi în ultimele 3 zile înainte de meci și o dată cu 30–60 de minute înainte de start
- Fiecare răspuns se salvează integral (JSON brut) plus rânduri normalizate

### 4.2 Predictor
Face predicțiile. Rulează **o singură dată pe meci**.

- NBA: cu 2–3 ore înainte de primul meci al zilei
- Fotbal: cu 24 de ore înainte de start

Pași:
1. Ia din baza de date meciurile care încep în fereastra următoare și cotele Pinnacle curente.
2. Calculează probabilitatea fără marjă pentru fiecare pariu (metoda multiplicativă, pe cele două sau trei rezultate).
3. Cheamă modelul Claude prin Anthropic API, cu căutare web activă, și îi dă: meciurile, liniile, probabilitățile fără marjă. Modelul caută știri (accidentări, loturi, minute, rotații, menajări) și întoarce pentru fiecare pariu ales: probabilitatea estimată, motivul în 1–3 fraze și sursele.
4. Un pariu se înregistrează doar dacă `p_model × cotă_zecimală − 1 ≥ 0,03`.
5. Limite: maximum 10 pariuri pe zi la NBA, maximum 5 pe etapă pentru fiecare ligă de fotbal. Dacă trec pragul mai multe, se păstrează cele cu avantajul estimat cel mai mare.

Cota înregistrată:
- NBA: cota Pinnacle din acel moment
- Fotbal: cota Pinnacle; se salvează separat și cea mai bună cotă dintre `superbet_ro`, `netbet_ro`, `mozzart`, `betfair_ex`, dacă există

Modelul și promptul sunt configurabile din `.env` și dintr-un fișier de prompt versionat. Câmpul `prompt_version` se salvează la fiecare predicție.

**Grup de control:** pentru fiecare pariu ales de AI, sistemul înregistrează și un pariu ales la întâmplare din aceleași meciuri, la aceeași oră. CLV-ul grupului de control trebuie să iasă aproape de zero sau ușor negativ. Dacă iese clar pozitiv, măsurătoarea e greșită.

### 4.3 Decontare
Rulează zilnic, pentru meciurile încheiate.

1. Ia cota de închidere Pinnacle și rezultatul din API.
2. Calculează CLV:
   `CLV = cota_luată × p_închidere_fără_marjă − 1`
3. Dacă linia de la închidere diferă de cea luată (de exemplu 20,5 → 19,5 puncte), CLV-ul pe cotă nu e comparabil. În cazul ăsta se salvează `line_moved = true` și `line_delta` cu semn (pozitiv dacă linia s-a mișcat în favoarea pariului), iar pariul intră într-o statistică separată.
4. Dacă Pinnacle nu are cotă de închidere, se folosește media fără marjă DraftKings + FanDuel și se marchează `benchmark = 'us_consensus'`.
5. Salvează rezultatul și profitul fictiv.

### 4.4 Raport (CLI)
Comanda `report` afișează:
- număr de pariuri, pe nișă
- CLV mediu cu interval de încredere 95% (bootstrap)
- procentul de pariuri care au bătut închiderea
- statistica separată pentru pariurile cu linie mutată (câte în favoare, câte împotrivă)
- aceleași cifre pentru grupul de control
- ROI fictiv, marcat explicit ca nesemnificativ sub 1.000 de pariuri

## 5. Integritatea datelor

Asta e partea care face testul credibil. Fără ea rezultatul nu valorează nimic.

- Tabela `predictions` este doar pentru adăugare. Un trigger în baza de date blochează `UPDATE` și `DELETE`.
- Ora înregistrării vine de la server, nu din aplicație.
- O predicție este respinsă dacă meciul începe în mai puțin de 5 minute sau a început deja.
- Fiecare rând păstrează `sha256(hash-ul rândului anterior + conținutul rândului)`.
- Predictorul nu are acces la tabela de decontări și nu primește niciodată date despre meciuri încheiate din aceeași zi.
- Nu se face backtest pe meciuri trecute: modelul le poate „ști” rezultatul.
- Orice schimbare de prompt, model sau prag deschide o serie nouă (`series_id`). Seriile nu se amestecă în raport.

## 6. Schema bazei de date

- `events` — id API, sport_key, echipe, ora de start, stare
- `odds_snapshots` — event_id, casă, piață, jucător, linie, parte (over/under/1/X/2), cotă americană, cotă zecimală, ora snapshot-ului
- `raw_responses` — endpoint, parametri, ora, JSON brut
- `predictions` — id, series_id, prompt_version, model, event_id, piață, jucător, linie, parte, cota luată, casa, p_fără_marjă, p_model, avantaj estimat, motiv, surse, is_control, created_at, row_hash
- `settlements` — prediction_id, cota și linia de închidere, benchmark, line_moved, line_delta, clv, rezultat, valoare reală, profit fictiv
- `runs` — componentă, start, sfârșit, stare, cereri API consumate, eroare

## 7. Stack propus

- Python 3.11+
- Supabase (Postgres) pentru date
- GitHub Actions cu cron pentru rulare programată
- Anthropic API pentru predictor

Dacă există un motiv bun pentru altă variantă (de exemplu un VPS deja disponibil), propune-o înainte de a începe.

## 8. Reguli de oprire și decizie

Testul nu se oprește mai devreme pentru că „arată bine” sau „arată rău”.

- NBA: decizie la 300 de pariuri decontate
- Fotbal: decizie la 60 de pariuri decontate (eșantion mic, concluzie doar orientativă)

| CLV mediu | Decizie |
|---|---|
| Peste +3% și intervalul de încredere peste zero | Trecem la test cu bani reali, mize mici |
| Între +1% și +3% | Marginal; continuăm încă 300 de pariuri |
| Sub +1% | Oprim proiectul |

## 9. Ordinea de lucru

**Faza 0 — verificare (înainte de orice altceva)**
Un script care confirmă cele 5 puncte neverificate din secțiunea 3 și afișează forma exactă a răspunsurilor. Dacă Pinnacle nu apare în cotele de închidere la `player_points`, oprește-te și raportează.

**Faza 1 — colector și bază de date**
Schema, triggerele de integritate, colectorul, programarea. Criteriu: 24 de ore de snapshot-uri fără erori.

**Faza 2 — predictor**
Inclusiv grupul de control. Criteriu: o zi de NBA cu predicții înregistrate înainte de start.

**Faza 3 — decontare și raport**
Criteriu: CLV calculat corect pe meciurile din ziua precedentă, verificat manual pe 5 pariuri.

## 10. Costuri de urmărit

- PropLine Hobby: abonament lunar
- Anthropic API: predictorul rulează o dată pe zi pentru NBA și de câteva ori pe săptămână pentru fotbal. Înregistrează tokenii consumați la fiecare rulare în `runs` și raportează costul după prima săptămână.
