-- 002 — integritatea datelor (SPEC §5 + deciziile de după Faza 0).
-- Regulile sunt în baza de date, nu în aplicație: niciun rol al aplicației nu le poate ocoli.

-- Timestamp în UTC, format fix — folosit în hash, ca rezultatul să nu depindă de TimeZone-ul sesiunii.
create or replace function clv.ts_utc(t timestamptz) returns text
language sql immutable as
$$ select to_char(t at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') $$;

-- ---------------------------------------------------------------- ora vine de la server
create or replace function clv.set_fetched_at() returns trigger language plpgsql as
$$ begin new.fetched_at := clock_timestamp(); return new; end $$;
create trigger raw_server_time before insert on clv.raw_responses
  for each row execute function clv.set_fetched_at();

create or replace function clv.set_snapshot_at() returns trigger language plpgsql as
$$ begin new.snapshot_at := clock_timestamp(); return new; end $$;
create trigger odds_server_time before insert on clv.odds_snapshots
  for each row execute function clv.set_snapshot_at();

create or replace function clv.set_updated_at() returns trigger language plpgsql as
$$ begin new.updated_at := clock_timestamp(); return new; end $$;
create trigger events_updated_at before update on clv.events
  for each row execute function clv.set_updated_at();

create or replace function clv.set_settled_at() returns trigger language plpgsql as
$$ begin new.settled_at := clock_timestamp(); return new; end $$;
create trigger settlements_server_time before insert on clv.settlements
  for each row execute function clv.set_settled_at();

create or replace function clv.set_series_created_at() returns trigger language plpgsql as
$$ begin new.created_at := clock_timestamp(); return new; end $$;
create trigger series_server_time before insert on clv.series
  for each row execute function clv.set_series_created_at();

-- ---------------------------------------------------------------- doar adăugare
create or replace function clv.forbid_modification() returns trigger language plpgsql as
$$ begin
  raise exception 'tabela %.% este doar pentru adăugare: % interzis', tg_table_schema, tg_table_name, tg_op
    using errcode = 'insufficient_privilege';
end $$;

create trigger predictions_append_only before update or delete on clv.predictions
  for each row execute function clv.forbid_modification();
create trigger predictions_no_truncate before truncate on clv.predictions
  for each statement execute function clv.forbid_modification();
create trigger series_append_only before update or delete on clv.series
  for each row execute function clv.forbid_modification();
create trigger series_no_truncate before truncate on clv.series
  for each statement execute function clv.forbid_modification();

-- ---------------------------------------------------------------- conținutul care intră în hash
create or replace function clv.prediction_content(p clv.predictions) returns text
language sql stable as $$
  select jsonb_build_object(
    'seq', p.seq, 'series_id', p.series_id, 'run_id', p.run_id,
    'prompt_version', p.prompt_version, 'model', p.model,
    'event_id', p.event_id, 'sport_key', p.sport_key, 'market', p.market,
    'player_id', p.player_id, 'player_name', p.player_name,
    'line', p.line, 'side', p.side, 'bookmaker', p.bookmaker,
    'price_american', p.price_american, 'price_decimal', p.price_decimal,
    'odds_snapshot_id', p.odds_snapshot_id,
    'odds_last_update', clv.ts_utc(p.odds_last_update), 'odds_is_stale', p.odds_is_stale,
    'p_no_vig', p.p_no_vig, 'p_model', p.p_model, 'edge', p.edge,
    'reason', p.reason, 'sources', p.sources,
    'is_control', p.is_control, 'pair_key', p.pair_key, 'rng_seed', p.rng_seed,
    'best_ro_bookmaker', p.best_ro_bookmaker, 'best_ro_price_decimal', p.best_ro_price_decimal,
    'stake', p.stake, 'hours_to_start', p.hours_to_start,
    'created_at', clv.ts_utc(p.created_at)
  )::text
$$;

-- ---------------------------------------------------------------- reguli la inserarea unei predicții
create or replace function clv.predictions_before_insert() returns trigger
language plpgsql security definer set search_path = clv, pg_catalog as $$
declare
  v_series   clv.series;
  v_commence timestamptz;
  v_prev     text;
  v_seq      bigint;
begin
  -- un singur scriitor la un moment dat, ca lanțul de hash-uri să nu se rupă
  perform pg_advisory_xact_lock(hashtext('clv.predictions.chain'));

  new.created_at := clock_timestamp();                  -- ora vine de la server

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

  -- cota trebuie să fie actuală (decizie după Faza 0)
  if new.odds_is_stale then
    raise exception 'cota e marcată is_stale';
  end if;
  if new.created_at - new.odds_last_update > interval '15 minutes' then
    raise exception 'ultima actualizare a cotei (%) e mai veche de 15 minute', new.odds_last_update;
  end if;
  if new.odds_last_update > new.created_at + interval '1 minute' then
    raise exception 'ultima actualizare a cotei (%) e în viitor', new.odds_last_update;
  end if;

  -- avantajul se calculează aici, nu în aplicație; pragul vine din serie
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

create trigger predictions_rules before insert on clv.predictions
  for each row execute function clv.predictions_before_insert();

-- ---------------------------------------------------------------- verificarea lanțului
-- Întoarce câte rânduri au fost verificate și primul `seq` stricat (null = lanț intact).
create or replace function clv.verify_prediction_chain()
returns table (checked bigint, first_bad_seq bigint)
language plpgsql stable security definer set search_path = clv, pg_catalog as $$
declare
  r     clv.predictions;
  prev  text := repeat('0', 64);
  n     bigint := 0;
begin
  for r in select * from clv.predictions order by seq loop
    n := n + 1;
    if r.seq <> n or r.prev_hash <> prev
       or r.row_hash <> encode(sha256(convert_to(r.prev_hash || clv.prediction_content(r), 'UTF8')), 'hex') then
      checked := n; first_bad_seq := r.seq;
      return next; return;
    end if;
    prev := r.row_hash;
  end loop;
  checked := n; first_bad_seq := null;
  return next;
end $$;
