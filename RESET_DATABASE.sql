-- ONE-TIME DESTRUCTIVE RESET for Village People Management.
-- BACK UP THE CORRECT Railway PostgreSQL database first.
-- Confirm current_database() and current_schema() before running.
-- This drops only the three application tables listed below.
-- It does NOT drop the PostgreSQL database or Railway service.
-- If a table has an unexpected external dependency, DROP will fail safely
-- because CASCADE is intentionally NOT used.

BEGIN;

DROP TABLE IF EXISTS public.access_link;
DROP TABLE IF EXISTS public.person;
DROP TABLE IF EXISTS public.village;

COMMIT;

-- After deploying the application, db.create_all() creates the fresh tables.
-- Verify with:
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema='public' AND table_name IN ('village','person','access_link')
-- ORDER BY table_name;
