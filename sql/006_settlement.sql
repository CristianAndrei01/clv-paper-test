-- 006 — decontare (Faza 3).
-- 1. `benchmark = 'none'`: nici Pinnacle, nici DraftKings/FanDuel nu au o cotă de închidere utilizabilă.
--    E permis doar pentru pariuri anulate (jucătorul n-a intrat pe teren): acolo CLV nu se calculează.
-- 2. Regulile de consistență ale unei decontări stau în baza de date, nu doar în cod:
--    pariu anulat => fără CLV și fără linie mutată; linie mutată => fără CLV pe cotă, cu line_delta ≠ 0;
--    linie nemutată și pariu neanulat => CLV și probabilitatea de închidere obligatorii.

alter table clv.settlements drop constraint settlements_benchmark_check;
alter table clv.settlements add constraint settlements_benchmark_check
  check (benchmark in ('pinnacle', 'us_consensus', 'none'));

alter table clv.settlements alter column result set not null;
alter table clv.settlements alter column profit set not null;

alter table clv.settlements add constraint settlements_consistency check (
  case
    when result = 'void' then clv is null and line_moved = false and line_delta is null and profit = 0
    when benchmark = 'none' then false
    when line_moved then clv is null and line_delta is not null and line_delta <> 0
    else clv is not null and line_delta is null and closing_p_no_vig is not null
  end
);
