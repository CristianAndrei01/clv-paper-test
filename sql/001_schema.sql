-- 001 — schema de bază (SPEC §6).
-- Totul stă în schema `clv`, care NU e expusă prin API-ul REST Supabase
-- (anon/authenticated nu au acces). Aplicația se conectează direct la Postgres
-- cu trei roluri separate (vezi 003_roles.sql).

create schema if not exists clv;
revoke all on schema clv from public;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on schema clv from anon, authenticated';
  end if;
end $$;

-- Fiecare rulare a unei componente (colector, predictor, decontare, raport).
create table clv.runs (
  id                       bigint generated always as identity primary key,
  component                text not null check (component in ('collector', 'predictor', 'settler', 'report')),
  started_at               timestamptz not null default clock_timestamp(),
  finished_at              timestamptz,
  status                   text not null default 'running' check (status in ('running', 'ok', 'error')),
  api_requests             int not null default 0,
  api_daily_remaining      int,
  anthropic_input_tokens   int,
  anthropic_output_tokens  int,
  anthropic_web_searches   int,
  rng_seed                 bigint,                  -- seed-ul grupului de control (predictor)
  gh_run_id                text,                    -- id-ul rulării GitHub Actions, dacă e cazul
  details                  jsonb not null default '{}',
  error                    text
);

create table clv.events (
  id             text primary key,                  -- id-ul PropLine
  sport_key      text not null,
  home_team      text not null,
  away_team      text not null,
  commence_time  timestamptz not null,
  status         text,
  home_score     int,
  away_score     int,
  first_seen_at  timestamptz not null default clock_timestamp(),
  updated_at     timestamptz not null default clock_timestamp()
);
create index events_sport_start on clv.events (sport_key, commence_time);

-- Răspunsul API complet, comprimat gzip. `body_sha256` e pe corpul necomprimat.
create table clv.raw_responses (
  id            bigint generated always as identity primary key,
  run_id        bigint references clv.runs (id),
  kind          text not null check (kind in ('events', 'scores', 'periodic', 'prestart', 'closing', 'results')),
  sport_key     text not null,
  endpoint      text not null,
  params        jsonb not null default '{}',
  status_code   int not null,
  fetched_at    timestamptz not null default clock_timestamp(),
  bytes         int not null,
  body_sha256   text not null,
  body_gzip     bytea not null,
  headers       jsonb not null default '{}'
);
create index raw_kind_time on clv.raw_responses (sport_key, kind, fetched_at desc);

-- Rânduri normalizate: o cotă = (meci, casă, piață, jucător, parte, linie).
-- Doar liniile principale (line_type = 'main'); liniile „milestone” rămân doar în JSON-ul brut.
create table clv.odds_snapshots (
  id                      bigint generated always as identity primary key,
  raw_response_id         bigint not null references clv.raw_responses (id),
  run_id                  bigint references clv.runs (id),
  snapshot_kind           text not null check (snapshot_kind in ('periodic', 'prestart', 'closing')),
  snapshot_at             timestamptz not null default clock_timestamp(),
  event_id                text not null references clv.events (id),
  bookmaker               text not null,
  market                  text not null,
  line_type               text,
  player_id               text,
  player_name             text,
  side                    text not null check (side in ('over', 'under', '1', 'X', '2')),
  line                    numeric(6, 2),
  price_american          int,
  price_decimal           numeric(10, 5),
  outcome_id              bigint,
  market_last_update      timestamptz,
  book_updated_at         timestamptz,
  last_change_at          timestamptz,
  last_seen_at            timestamptz,
  is_stale                boolean,
  -- completate doar pentru snapshot_kind = 'closing' (decontare, Faza 3)
  closing_at              timestamptz,
  closing_last_seen_at    timestamptz,
  opening_price_american  int,
  opening_line            numeric(6, 2),
  opening_at              timestamptz
);
create index odds_lookup on clv.odds_snapshots (event_id, bookmaker, market, player_id, side, snapshot_at desc);
create index odds_raw on clv.odds_snapshots (raw_response_id);

-- O serie = combinația model + prompt + prag. Orice schimbare = serie nouă (SPEC §5).
create table clv.series (
  id              text primary key,
  sport_key       text not null,
  model           text not null,
  prompt_version  text not null,
  edge_threshold  numeric(6, 4) not null,
  config          jsonb not null default '{}',
  created_at      timestamptz not null default clock_timestamp()
);

create table clv.predictions (
  id                     bigint generated always as identity primary key,
  seq                    bigint not null unique,           -- poziția în lanțul de hash-uri
  series_id              text not null references clv.series (id),
  run_id                 bigint not null references clv.runs (id),
  prompt_version         text not null,
  model                  text not null,
  event_id               text not null references clv.events (id),
  sport_key              text not null,
  market                 text not null,
  player_id              text,
  player_name            text,
  line                   numeric(6, 2),
  side                   text not null check (side in ('over', 'under', '1', 'X', '2')),
  bookmaker              text not null,
  price_american         int not null,
  price_decimal          numeric(10, 5) not null,
  odds_snapshot_id       bigint references clv.odds_snapshots (id),
  odds_last_update       timestamptz not null,             -- ultima actualizare Pinnacle a liniei
  odds_is_stale          boolean not null,
  p_no_vig               numeric(8, 6) not null,
  p_model                numeric(8, 6),                    -- null la grupul de control
  edge                   numeric(8, 6),                    -- calculat de server: p_model × cotă − 1
  reason                 text,
  sources                jsonb,
  is_control             boolean not null,
  pair_key               uuid,                             -- leagă pariul AI de pariul de control
  rng_seed               bigint,
  best_ro_bookmaker      text,                             -- rămâne gol (decizia 1)
  best_ro_price_decimal  numeric(10, 5),
  stake                  numeric(8, 2) not null default 100,
  hours_to_start         numeric(8, 3) not null,           -- calculat de server
  created_at             timestamptz not null,             -- pus de server
  prev_hash              text not null,
  row_hash               text not null unique
);
create index predictions_event on clv.predictions (event_id);
create index predictions_series on clv.predictions (series_id, created_at);

create table clv.settlements (
  id                     bigint generated always as identity primary key,
  prediction_id          bigint not null unique references clv.predictions (id),
  run_id                 bigint references clv.runs (id),
  benchmark              text not null check (benchmark in ('pinnacle', 'us_consensus')),
  closing_line           numeric(6, 2),
  closing_price_american int,
  closing_price_decimal  numeric(10, 5),
  closing_p_no_vig       numeric(8, 6),
  closing_at             timestamptz,
  line_moved             boolean not null,
  line_delta             numeric(6, 2),                    -- pozitiv = în favoarea pariului
  clv                    numeric(10, 6),                   -- null dacă line_moved
  result                 text check (result in ('won', 'lost', 'push', 'void')),
  actual_value           numeric(8, 2),
  profit                 numeric(10, 2),                   -- fictiv, miză 100 lei
  details                jsonb not null default '{}',
  settled_at             timestamptz not null default clock_timestamp()
);
