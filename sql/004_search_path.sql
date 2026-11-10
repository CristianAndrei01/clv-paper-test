-- 004 — search_path fix pentru toate funcțiile din clv (advisor Supabase 0011).
-- Fără el, un rol ar putea, în teorie, umbri funcții folosite la calculul hash-ului.
alter function clv.ts_utc(timestamptz)                 set search_path = clv, pg_catalog;
alter function clv.set_fetched_at()                    set search_path = clv, pg_catalog;
alter function clv.set_snapshot_at()                   set search_path = clv, pg_catalog;
alter function clv.set_updated_at()                    set search_path = clv, pg_catalog;
alter function clv.set_settled_at()                    set search_path = clv, pg_catalog;
alter function clv.set_series_created_at()             set search_path = clv, pg_catalog;
alter function clv.forbid_modification()               set search_path = clv, pg_catalog;
alter function clv.prediction_content(clv.predictions) set search_path = clv, pg_catalog;
