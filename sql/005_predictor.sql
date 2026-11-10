-- 005 — reguli pentru predictor (Faza 2).
-- 1. Cota unei predicții vine dintr-un snapshot salvat: trigger-ul ia prețul, linia,
--    `last_seen_at` și `is_stale` direct din snapshot, nu din ce trimite aplicația.
--    Regula de 15 minute se aplică pe `last_seen_at` (decizie după Faza 1).
-- 2. Snapshot-urile făcute chiar de predictor (după răspunsul modelului) au tipul 'predictor'.
-- 3. Predictorul își vede propriile rulări printr-o vedere, nu tabela `runs` întreagă.

alter table clv.raw_responses drop constraint raw_responses_kind_check;
alter table clv.raw_responses add constraint raw_responses_kind_check
  check (kind in ('events', 'scores', 'periodic', 'prestart', 'predictor', 'closing', 'results'));

alter table clv.odds_snapshots drop constraint odds_snapshots_snapshot_kind_check;
alter table clv.odds_snapshots add constraint odds_snapshots_snapshot_kind_check
  check (snapshot_kind in ('periodic', 'prestart', 'predictor', 'closing'));

create or replace view clv.predictor_odds as
  select o.id, o.event_id, o.snapshot_kind, o.snapshot_at, o.bookmaker, o.market, o.line_type,
         o.player_id, o.player_name, o.side, o.line, o.price_american, o.price_decimal,
         o.market_last_update, o.book_updated_at, o.last_change_at, o.last_seen_at, o.is_stale
  from clv.odds_snapshots o
  join clv.events e on e.id = o.event_id
  where e.commence_time > now()
    and o.snapshot_kind in ('periodic', 'prestart', 'predictor');

create or replace view clv.predictor_runs as
  select id, started_at, finished_at, status, details
  from clv.runs
  where component = 'predictor';
grant select on clv.predictor_runs to clv_predictor;

-- Colectorul scrie snapshot-urile cerute de predictor sub rularea predictorului.
-- (nicio schimbare de drepturi: colectorul are deja insert pe raw_responses/odds_snapshots)

create or replace function clv.predictions_before_insert() returns trigger
language plpgsql security definer set search_path = clv, pg_catalog as $$
declare
  v_series   clv.series;
  v_snap     clv.odds_snapshots;
  v_commence timestamptz;
  v_prev     text;
  v_seq      bigint;
begin
  perform pg_advisory_xact_lock(hashtext('clv.predictions.chain'));

  new.created_at := clock_timestamp();

  select * into v_series from clv.series where id = new.series_id;
  if not found then
    raise exception 'seria % nu există', new.series_id;
  end if;
  if v_series.model <> new.model or v_series.prompt_version <> new.prompt_version
     or v_series.sport_key <> new.sport_key then
    raise exception 'predicția nu corespunde seriei % (model/prompt/sport)', new.series_id;
  end if;

  select commence_time into v_commence from clv.events where id = new.event_id;
  if v_commence is null then
    raise exception 'meci necunoscut: %', new.event_id;
  end if;
  if v_commence - new.created_at < interval '5 minutes' then
    raise exception 'meciul % începe în mai puțin de 5 minute sau a început (start %)', new.event_id, v_commence;
  end if;

  -- cota vine din snapshot, nu din aplicație
  if new.odds_snapshot_id is null then
    raise exception 'odds_snapshot_id lipsește';
  end if;
  select * into v_snap from clv.odds_snapshots where id = new.odds_snapshot_id;
  if not found then
    raise exception 'snapshot % inexistent', new.odds_snapshot_id;
  end if;
  if v_snap.snapshot_kind not in ('periodic', 'prestart', 'predictor')
     or v_snap.event_id <> new.event_id or v_snap.bookmaker <> new.bookmaker
     or v_snap.market <> new.market or v_snap.side <> new.side
     or v_snap.player_id is distinct from new.player_id
     or v_snap.line is distinct from new.line
     or v_snap.price_american is distinct from new.price_american then
    raise exception 'predicția nu corespunde snapshot-ului % (meci/casă/piață/parte/jucător/linie/cotă)', new.odds_snapshot_id;
  end if;
  new.price_decimal    := v_snap.price_decimal;
  new.odds_last_update := v_snap.last_seen_at;
  new.odds_is_stale    := coalesce(v_snap.is_stale, false);

  if new.odds_last_update is null then
    raise exception 'snapshot-ul % nu are last_seen_at', new.odds_snapshot_id;
  end if;
  if new.odds_is_stale then
    raise exception 'cota e marcată is_stale';
  end if;
  if new.created_at - new.odds_last_update > interval '15 minutes' then
    raise exception 'cota a fost văzută ultima dată la % (mai mult de 15 minute)', new.odds_last_update;
  end if;
  if new.odds_last_update > new.created_at + interval '1 minute' then
    raise exception 'last_seen_at (%) e în viitor', new.odds_last_update;
  end if;

  if new.is_control then
    new.edge := null;
  else
    if new.p_model is null then
      raise exception 'p_model lipsește la un pariu care nu e de control';
    end if;
    new.edge := round(new.p_model * new.price_decimal - 1, 6);
    if new.edge < v_series.edge_threshold then
      raise exception 'avantaj % sub pragul seriei %', new.edge, v_series.edge_threshold;
    end if;
  end if;

  new.hours_to_start := round(extract(epoch from (v_commence - new.created_at)) / 3600.0, 3);

  select seq, row_hash into v_seq, v_prev from clv.predictions order by seq desc limit 1;
  new.seq := coalesce(v_seq, 0) + 1;
  new.prev_hash := coalesce(v_prev, repeat('0', 64));
  new.row_hash := encode(sha256(convert_to(new.prev_hash || clv.prediction_content(new), 'UTF8')), 'hex');
  return new;
end $$;
