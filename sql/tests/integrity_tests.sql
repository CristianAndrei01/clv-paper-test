-- Teste pentru regulile de integritate (002_integrity.sql).
-- Rulează ca proprietar al schemei. Totul se face într-un singur bloc care se termină
-- cu o excepție intenționată, deci NIMIC nu rămâne în baza de date.
-- Rezultat așteptat: eroarea „INTEGRITY_TESTS_PASSED: N teste”.
-- Orice alt mesaj de eroare = test picat.

do $$
declare
  v_run   bigint;
  p1      clv.predictions;
  p2      clv.predictions;
  v_n     int := 0;
  v_ok    boolean;
  v_check record;
begin
  insert into clv.runs (component) values ('predictor') returning id into v_run;
  insert into clv.series (id, sport_key, model, prompt_version, edge_threshold)
    values ('__test__', 'basketball_nba', 'test-model', 'v0', 0.03);
  insert into clv.events (id, sport_key, home_team, away_team, commence_time)
    values ('__t_later__', 'basketball_nba', 'H', 'A', clock_timestamp() + interval '3 hours'),
           ('__t_soon__',  'basketball_nba', 'H', 'A', clock_timestamp() + interval '4 minutes'),
           ('__t_past__',  'basketball_nba', 'H', 'A', clock_timestamp() - interval '1 hour');

  -- 1. inserare validă; created_at trimis de aplicație e ignorat; hours_to_start și edge vin de la server
  insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
      player_id, player_name, line, side, bookmaker, price_american, price_decimal,
      odds_last_update, odds_is_stale, p_no_vig, p_model, edge, is_control,
      seq, prev_hash, row_hash, hours_to_start, created_at)
    values ('__test__', v_run, 'v0', 'test-model', '__t_later__', 'basketball_nba', 'player_points',
      'nba:1', 'Test Player', 20.5, 'over', 'pinnacle', 100, 2.0,
      clock_timestamp() - interval '2 minutes', false, 0.5, 0.55, 99, false,
      999, 'x', 'x', 99, '2000-01-01')
    returning * into p1;
  if p1.created_at < clock_timestamp() - interval '1 minute' then raise exception 'T1: created_at nu e de la server'; end if;
  if abs(p1.hours_to_start - 3) > 0.02 then raise exception 'T1: hours_to_start greșit: %', p1.hours_to_start; end if;
  if p1.edge <> 0.1 then raise exception 'T1: edge greșit: %', p1.edge; end if;
  if p1.prev_hash <> repeat('0', 64) and p1.seq = 1 then raise exception 'T1: prev_hash greșit'; end if;
  v_n := v_n + 1;

  -- 2. al doilea rând se leagă de primul
  insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
      player_name, line, side, bookmaker, price_american, price_decimal,
      odds_last_update, odds_is_stale, p_no_vig, is_control)
    values ('__test__', v_run, 'v0', 'test-model', '__t_later__', 'basketball_nba', 'player_points',
      'Control Player', 10.5, 'under', 'pinnacle', -110, 1.90909,
      clock_timestamp(), false, 0.5, true)
    returning * into p2;
  if p2.prev_hash <> p1.row_hash or p2.seq <> p1.seq + 1 then raise exception 'T2: lanțul nu se leagă'; end if;
  if p2.edge is not null then raise exception 'T2: edge la control trebuie să fie null'; end if;
  select * into v_check from clv.verify_prediction_chain();
  if v_check.first_bad_seq is not null then raise exception 'T2: lanț raportat stricat la %', v_check.first_bad_seq; end if;
  v_n := v_n + 1;

  -- 3. UPDATE blocat
  v_ok := false;
  begin update clv.predictions set p_model = 0.9 where id = p1.id;
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T3: UPDATE a trecut'; end if;
  v_n := v_n + 1;

  -- 4. DELETE blocat
  v_ok := false;
  begin delete from clv.predictions where id = p1.id;
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T4: DELETE a trecut'; end if;
  v_n := v_n + 1;

  -- 5. TRUNCATE blocat
  v_ok := false;
  begin truncate clv.predictions cascade;
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T5: TRUNCATE a trecut'; end if;
  v_n := v_n + 1;

  -- 6. meci care începe în 4 minute -> respins
  v_ok := false;
  begin
    insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
        side, bookmaker, price_american, price_decimal, odds_last_update, odds_is_stale, p_no_vig, is_control)
      values ('__test__', v_run, 'v0', 'test-model', '__t_soon__', 'basketball_nba', 'player_points',
        'over', 'pinnacle', 100, 2.0, clock_timestamp(), false, 0.5, true);
  exception when raise_exception then v_ok := true; end;
  if not v_ok then raise exception 'T6: predicție acceptată cu 4 minute înainte de start'; end if;
  v_n := v_n + 1;

  -- 7. meci deja început -> respins
  v_ok := false;
  begin
    insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
        side, bookmaker, price_american, price_decimal, odds_last_update, odds_is_stale, p_no_vig, is_control)
      values ('__test__', v_run, 'v0', 'test-model', '__t_past__', 'basketball_nba', 'player_points',
        'over', 'pinnacle', 100, 2.0, clock_timestamp(), false, 0.5, true);
  exception when raise_exception then v_ok := true; end;
  if not v_ok then raise exception 'T7: predicție acceptată după start'; end if;
  v_n := v_n + 1;

  -- 8. cotă is_stale -> respinsă
  v_ok := false;
  begin
    insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
        side, bookmaker, price_american, price_decimal, odds_last_update, odds_is_stale, p_no_vig, is_control)
      values ('__test__', v_run, 'v0', 'test-model', '__t_later__', 'basketball_nba', 'player_points',
        'over', 'pinnacle', 100, 2.0, clock_timestamp(), true, 0.5, true);
  exception when raise_exception then v_ok := true; end;
  if not v_ok then raise exception 'T8: cotă is_stale acceptată'; end if;
  v_n := v_n + 1;

  -- 9. cotă actualizată acum 16 minute -> respinsă
  v_ok := false;
  begin
    insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
        side, bookmaker, price_american, price_decimal, odds_last_update, odds_is_stale, p_no_vig, is_control)
      values ('__test__', v_run, 'v0', 'test-model', '__t_later__', 'basketball_nba', 'player_points',
        'over', 'pinnacle', 100, 2.0, clock_timestamp() - interval '16 minutes', false, 0.5, true);
  exception when raise_exception then v_ok := true; end;
  if not v_ok then raise exception 'T9: cotă veche de 16 minute acceptată'; end if;
  v_n := v_n + 1;

  -- 10. avantaj sub prag -> respins (0.51 × 2.0 − 1 = 0.02 < 0.03)
  v_ok := false;
  begin
    insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
        side, bookmaker, price_american, price_decimal, odds_last_update, odds_is_stale, p_no_vig, p_model, is_control)
      values ('__test__', v_run, 'v0', 'test-model', '__t_later__', 'basketball_nba', 'player_points',
        'over', 'pinnacle', 100, 2.0, clock_timestamp(), false, 0.5, 0.51, false);
  exception when raise_exception then v_ok := true; end;
  if not v_ok then raise exception 'T10: avantaj sub prag acceptat'; end if;
  v_n := v_n + 1;

  -- 11. model diferit de cel al seriei -> respins
  v_ok := false;
  begin
    insert into clv.predictions (series_id, run_id, prompt_version, model, event_id, sport_key, market,
        side, bookmaker, price_american, price_decimal, odds_last_update, odds_is_stale, p_no_vig, is_control)
      values ('__test__', v_run, 'v0', 'alt-model', '__t_later__', 'basketball_nba', 'player_points',
        'over', 'pinnacle', 100, 2.0, clock_timestamp(), false, 0.5, true);
  exception when raise_exception then v_ok := true; end;
  if not v_ok then raise exception 'T11: model diferit de serie acceptat'; end if;
  v_n := v_n + 1;

  -- 12. seria nu se poate modifica
  v_ok := false;
  begin update clv.series set edge_threshold = 0 where id = '__test__';
  exception when insufficient_privilege then v_ok := true; end;
  if not v_ok then raise exception 'T12: seria a putut fi modificată'; end if;
  v_n := v_n + 1;

  -- 13. verificatorul detectează o modificare (trigger dezactivat temporar, doar în acest test)
  alter table clv.predictions disable trigger predictions_append_only;
  update clv.predictions set p_model = 0.99 where id = p1.id;
  alter table clv.predictions enable trigger predictions_append_only;
  select * into v_check from clv.verify_prediction_chain();
  if v_check.first_bad_seq is distinct from p1.seq then
    raise exception 'T13: verificatorul nu a detectat modificarea (first_bad_seq=%)', v_check.first_bad_seq;
  end if;
  v_n := v_n + 1;

  -- 14. ora snapshot-ului vine de la server
  declare v_raw bigint; v_at timestamptz;
  begin
    insert into clv.raw_responses (kind, sport_key, endpoint, status_code, bytes, body_sha256, body_gzip, fetched_at)
      values ('periodic', 'basketball_nba', '/x', 200, 0, 'x', '\x00', '2000-01-01') returning id, fetched_at into v_raw, v_at;
    if v_at < clock_timestamp() - interval '1 minute' then raise exception 'T14: fetched_at nu e de la server'; end if;
    insert into clv.odds_snapshots (raw_response_id, snapshot_kind, event_id, bookmaker, market, side, snapshot_at)
      values (v_raw, 'periodic', '__t_later__', 'pinnacle', 'player_points', 'over', '2000-01-01') returning snapshot_at into v_at;
    if v_at < clock_timestamp() - interval '1 minute' then raise exception 'T14: snapshot_at nu e de la server'; end if;
  end;
  v_n := v_n + 1;

  raise exception 'INTEGRITY_TESTS_PASSED: % teste', v_n;
end $$;
