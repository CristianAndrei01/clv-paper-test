-- 003 — trei roluri Postgres, fiecare cu drepturile minime.
-- Parolele NU sunt aici: se setează separat (ALTER ROLE ... PASSWORD cu verificator SCRAM),
-- ca să nu ajungă în istoricul migrărilor.

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'clv_collector') then
    create role clv_collector login noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'clv_predictor') then
    create role clv_predictor login noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'clv_settler') then
    create role clv_settler login noinherit;
  end if;
end $$;

alter role clv_collector set statement_timeout = '60s';
alter role clv_predictor set statement_timeout = '60s';
alter role clv_settler   set statement_timeout = '120s';

grant usage on schema clv to clv_collector, clv_predictor, clv_settler;

-- ---------------------------------------------------------------- vederi pentru predictor
-- Predictorul vede doar meciurile care nu au început și cotele lor de dinainte de start.
-- Nu vede niciodată decontări, rezultate, cote de închidere sau meciuri încheiate (SPEC §5).
create or replace view clv.predictor_events as
  select id, sport_key, home_team, away_team, commence_time
  from clv.events
  where commence_time > now();

create or replace view clv.predictor_odds as
  select o.id, o.event_id, o.snapshot_kind, o.snapshot_at, o.bookmaker, o.market, o.line_type,
         o.player_id, o.player_name, o.side, o.line, o.price_american, o.price_decimal,
         o.market_last_update, o.book_updated_at, o.last_change_at, o.last_seen_at, o.is_stale
  from clv.odds_snapshots o
  join clv.events e on e.id = o.event_id
  where e.commence_time > now()
    and o.snapshot_kind in ('periodic', 'prestart');

-- ---------------------------------------------------------------- colector
grant select, insert on clv.events, clv.raw_responses, clv.odds_snapshots to clv_collector;
grant update (home_team, away_team, commence_time, status, home_score, away_score)
  on clv.events to clv_collector;

-- ---------------------------------------------------------------- predictor
grant select on clv.predictor_events, clv.predictor_odds to clv_predictor;
grant select, insert on clv.series, clv.predictions to clv_predictor;

-- ---------------------------------------------------------------- decontare (și raport)
grant select on clv.events, clv.raw_responses, clv.odds_snapshots, clv.series, clv.predictions
  to clv_settler;
grant insert on clv.raw_responses, clv.odds_snapshots to clv_settler;
grant select, insert, update on clv.settlements to clv_settler;
grant update (status, home_score, away_score) on clv.events to clv_settler;

-- ---------------------------------------------------------------- runs
-- Fiecare rol își scrie rulările. Predictorul poate citi doar coloana id (pentru RETURNING / WHERE).
grant insert, update on clv.runs to clv_collector, clv_predictor, clv_settler;
grant select on clv.runs to clv_collector, clv_settler;
grant select (id) on clv.runs to clv_predictor;

grant execute on function clv.verify_prediction_chain() to clv_predictor, clv_settler;

-- Nimeni din aplicație nu primește UPDATE/DELETE/TRUNCATE pe predictions sau series.
revoke update, delete, truncate on clv.predictions, clv.series from clv_collector, clv_predictor, clv_settler;
