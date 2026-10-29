create extension if not exists pg_cron;
-- 23:00 UTC is midnight in Budapest on New Year's Day. Daily retries also
-- recover a missed run; the rollover only inserts absent annual records.
select cron.schedule('annual-project-price-rollover', '0 23 * * *',
  'select private.rollover_project_pricing();');
