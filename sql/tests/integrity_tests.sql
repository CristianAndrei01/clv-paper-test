-- Teste pentru regulile de integritate (002 + 005).
-- Rulează ca proprietar al schemei. Totul se face într-un singur bloc care se termină
-- cu o excepție intenționată, deci NIMIC nu rămâne în baza de date.
-- Rezultat așteptat: eroarea „INTEGRITY_TESTS_PASSED: N teste”. Orice alt mesaj = test picat.

do $$
declare
  v_run    bigint;
  v_raw    bigint;
  s_ok     bigint;   -- over 20.5 @ +100, văzut acum 2 minute
  s_ctl    bigint;   -- under 10.5 @ -110, văzut acum
  s_old    bigint;   -- văzut acum 16 minute
  s_stale  bigint;   -- is_stale = true
  s_soon   bigint;   -- meci care începe în 4 minute
  s_past   bigint;   -- meci început
  s_close  bigint;   -- snapshot de închidere
  p1       clv.predictions;
  p2       clv.predictions;
  v_n      int := 0;
  v_ok     boolean;
  v_check  record;
  v_at     timestamptz;
begin
  insert into clv.runs (component) values ('predictor') returning id into v_run;
  insert into clv.series (id, sport_key, model, prompt_version, edge_threshold)
    values ('__test__', 'basketball_nba', 'test-model', 'v0', 0.03);
  insert into clv.events (id, sport_key, home_team, away_team, commence_time)
    values ('__t_later__', 'basketball_nba', 'H', 'A', clock_timestamp() + interval '3 hours'),
           ('__t_soon__',  'basketball_nba', 'H', 'A', clock_timestamp() + interval '4 minutes'),
           ('__t_past__',  'basketball_nba', 'H', 'A', clock_timestamp() - interval '1 hour');
  insert into clv.raw_responses (kind, sport_key, endpoint, status_code, bytes, body_sha256, body_gzip)
    values ('predictor', 'basketball_nba', '/x', 200, 0, 'x', '\x00') returning id into v_raw;

  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, line_type,
      player_id, player_name, side, line, price_american, price_decimal, last_seen_at, is_stale)
    values (v_raw, 'predictor', '__t_later__', 'pinnacle', 'player_points', 'main', 'nba:1', 'P1', 'over', 20.5, 100, 2.0,
            clock_timestamp() - interval '2 minutes', null) returning id into s_ok;
  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, line_type,
      player_id, player_name, side, line, price_american, price_decimal, last_seen_at)
    values (v_raw, 'predictor', '__t_later__', 'pinnacle', 'player_points', 'main', 'nba:2', 'P2', 'under', 10.5, -110, 1.90909,
            clock_timestamp()) returning id into s_ctl;
  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, line_type,
      player_id, side, line, price_american, price_decimal, last_seen_at)
    values (v_raw, 'predictor', '__t_later__', 'pinnacle', 'player_points', 'main', 'nba:3', 'over', 5.5, 100, 2.0,
            clock_timestamp() - interval '16 minutes') returning id into s_old;
  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, line_type,
      player_id, side, line, price_american, price_decimal, last_seen_at, is_stale)
    values (v_raw, 'predictor', '__t_later__', 'pinnacle', 'player_points', 'main', 'nba:4', 'over', 5.5, 100, 2.0,
            clock_timestamp(), true) returning id into s_stale;
  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, line_type,
      player_id, side, line, price_american, price_decimal, last_seen_at)
    values (v_raw, 'predictor', '__t_soon__', 'pinnacle', 'player_points', 'main', 'nba:5', 'over', 5.5, 100, 2.0,
            clock_timestamp()) returning id into s_soon;
  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, line_type,
      player_id, side, line, price_american, price_decimal, last_seen_at)
    values (v_raw, 'predictor', '__t_past__', 'pinnacle', 'player_points', 'main', 'nba:6', 'over', 5.5, 100, 2.0,
            clock_timestamp()) returning id into s_past;
  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, line_type,
      player_id, side, line, price_american, price_decimal, last_seen_at)
    values (v_raw, 'closing', '__t_later__', 'pinnacle', 'player_points', 'main', 'nba:7', 'over', 5.5, 100, 2.0,
            clock_timestamp()) returning id into s_close;

  -- 1. inserare validă: created_at, hours_to_start, edge, prețul și last_seen_at vin de la server/snapshot
  insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
      player_id, player_name, line, side, bookmaker, price_american, price_decimal, odds_snapshot_id,
      odds_last_update, odds_is_stale, p_no_vig, p_model, edge, is_control,
      seq, prev_hash, row_hash, hours_to_start, created_at)
    values ('__test__', v_run, 'v0', 'test-model', '__t_later__', 'basketball_nba', 'player_points',
      'nba:1', 'P1', 20.5, 'over', 'pinnacle', 100, 9.99, s_ok,
      '2000-01-01', true, 0.5, 0.55, 99, false,
      999, 'x', 'x', 99, '2000-01-01')
    returning * into p1;
  if p1.created_at < clock_timestamp() - interval '1 minute' then raise exception 'T1: created_at nu e de la server'; end if;
  if abs(p1.hours_to_start - 3) > 0.02 then raise exception 'T1: hours_to_start greșit: %', p1.hours_to_start; end if;
  if p1.price_decimal <> 2.0 then raise exception 'T1: price_decimal nu vine din snapshot: %', p1.price_decimal; end if;
  if p1.edge <> 0.1 then raise exception 'T1: edge greșit: %', p1.edge; end if;
  if p1.odds_is_stale or p1.odds_last_update < clock_timestamp() - interval '5 minutes' then
    raise exception 'T1: last_seen_at/is_stale nu vin din snapshot'; end if;
  v_n := v_n + 1;

  -- 2. al doilea rând (control) se leagă de primul
  insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
      player_id, player_name, line, side, bookmaker, price_american, price_decimal, odds_snapshot_id,
      odds_last_update, odds_is_stale, p_no_vig, is_control)
    values ('__test__', v_run, 'v0', 'test-model', '__t_later__', 'basketball_nba', 'player_points',
      'nba:2', 'P2', 10.5, 'under', 'pinnacle', -110, 0, s_ctl, clock_timestamp(), false, 0.5, true)
    returning * into p2;
  if p2.prev_hash <> p1.row_hash or p2.seq <> p1.seq + 1 then raise exception 'T2: lanțul nu se leagă'; end if;
  if p2.edge is not null then raise exception 'T2: edge la control trebuie să fie null'; end if;
  select * into v_check from clv.verify_prediction_chain();
  if v_check.first_bad_seq is not null then raise exception 'T2: lanț raportat stricat la %', v_check.first_bad_seq; end if;
  v_n := v_n + 1;

  -- 3–5. UPDATE / DELETE / TRUNCATE blocate
  v_ok := false;
  begin update clv.predictions set p_model = 0.9 where id = p1.id;
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T3: UPDATE a trecut'; end if;
  v_n := v_n + 1;

  v_ok := false;
  begin delete from clv.predictions where id = p1.id;
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T4: DELETE a trecut'; end if;
  v_n := v_n + 1;

  v_ok := false;
  begin truncate clv.predictions cascade;
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T5: TRUNCATE a trecut'; end if;
  v_n := v_n + 1;

  -- 6–15. predicții care trebuie respinse
  declare
    cases text[] := array[
      'T6 meci în 4 minute', 'T7 meci început', 'T8 cotă is_stale', 'T9 văzută acum 16 minute',
      'T10 snapshot de închidere', 'T11 fără snapshot', 'T12 linie diferită de snapshot',
      'T13 cotă diferită de snapshot', 'T14 avantaj sub prag', 'T15 model diferit de serie'];
    i int;
  begin
    for i in 1 .. array_length(cases, 1) loop
      v_ok := false;
      begin
        insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
            player_id, line, side, bookmaker, price_american, price_decimal, odds_snapshot_id,
            odds_last_update, odds_is_stale, p_no_vig, p_model, is_control)
        select '__test__', v_run, 'v0',
               case when i = 10 then 'alt-model' else 'test-model' end,
               s.event_id, 'basketball_nba', 'player_points', s.player_id,
               case when i = 7 then s.line + 1 else s.line end,
               s.side, 'pinnacle',
               case when i = 8 then s.price_american + 5 else s.price_american end,
               s.price_decimal,
               case when i = 6 then null else s.id end,
               clock_timestamp(), false, 0.5,
               case when i = 9 then 0.51 else null end,
               i <> 9
        from clv.odds_snapshots s
        where s.id = (array[s_soon, s_past, s_stale, s_old, s_close, s_ok, s_ok, s_ok, s_ok, s_ok])[i];
      exception when raise_exception then v_ok := true; end;
      if not v_ok then raise exception '%: predicție acceptată', cases[i]; end if;
      v_n := v_n + 1;
    end loop;
  end;

  -- 16. seria nu se poate modifica
  v_ok := false;
  begin update clv.series set edge_threshold = 0 where id = '__test__';
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T16: seria a putut fi modificată'; end if;
  v_n := v_n + 1;

  -- 17. verificatorul detectează o modificare (trigger dezactivat temporar, doar în acest test)
  alter table clv.predictions disable trigger predictions_append_only;
  update clv.predictions set p_model = 0.99 where id = p1.id;
  alter table clv.predictions enable trigger predictions_append_only;
  select * into v_check from clv.verify_prediction_chain();
  if v_check.first_bad_seq is distinct from p1.seq then
    raise exception 'T17: verificatorul nu a detectat modificarea (first_bad_seq=%)', v_check.first_bad_seq;
  end if;
  v_n := v_n + 1;

  -- 18. ora snapshot-ului și a răspunsului brut vin de la server
  insert into clv.raw_responses (kind, sport_key, endpoint, status_code, bytes, body_sha256, body_gzip, fetched_at)
    values ('periodic', 'basketball_nba', '/x', 200, 0, 'x', '\x00', '2000-01-01') returning fetched_at into v_at;
  if v_at < clock_timestamp() - interval '1 minute' then raise exception 'T18: fetched_at nu e de la server'; end if;
  insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, side, snapshot_at)
    values (v_raw, 'periodic', '__t_later__', 'pinnacle', 'player_points', 'over', '2000-01-01') returning snapshot_at into v_at;
  if v_at < clock_timestamp() - interval '1 minute' then raise exception 'T18: snapshot_at nu e de la server'; end if;
  v_n := v_n + 1;

  raise exception 'INTEGRITY_TESTS_PASSED: % teste', v_n;
end $$;
