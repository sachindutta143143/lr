# Village People Management — Complete Railway PostgreSQL Build

This project is a Flask application for village-wise people records.

## Included
- Admin login protected by `ADMIN_PASSWORD`.
- Add and edit village and president names.
- Add, edit and delete people records.
- Village-wise and gender-wise totals.
- Search people by name, father's name and address.
- Filter by village and gender; matching count is separate from total count.
- Export all records or one village's records to CSV.
- Create time-limited president access links and revoke links.
- President link can only access its linked village.
- Responsive admin and president pages.
- PostgreSQL is required for all application data. SQLite is not used as a fallback for saving records.

## Railway environment variables
Set these on the **Flask web service** (not only on the PostgreSQL service):

- `DATABASE_URL`: use Railway's **Add Reference** to connect the web service to the intended PostgreSQL service. Use the private/internal connection URL where supported.
- `ADMIN_PASSWORD`: a long, unique administrator password. Do not put it in source code or share it in chat.
- `SECRET_KEY`: a long random secret used to sign browser sessions. Keep it private and stable between deployments.

Optional: `LOG_LEVEL=INFO`.

## One-time complete reset procedure

**This permanently deletes all rows and table definitions in the three application tables. It is not reversible except by restoring a backup.**

1. Open Railway and select the exact PostgreSQL service used by this app.
2. Create and verify a backup/snapshot first.
3. Open the PostgreSQL query console for that database.
4. Verify the target before doing anything destructive:

   ```sql
   SELECT current_database() AS database_name,
          current_schema() AS schema_name,
          current_user AS connected_user;
   ```

5. Confirm the expected application tables:

   ```sql
   SELECT table_schema, table_name
   FROM information_schema.tables
   WHERE table_schema = 'public'
     AND table_name IN ('village', 'person', 'access_link')
   ORDER BY table_name;
   ```

6. Open `RESET_DATABASE.sql`, review it, and execute it once in that same database. It drops only `public.access_link`, `public.person`, and `public.village`; it does not drop the database or Railway service. No `CASCADE` is used.
7. Deploy this project. On the first database-backed request, `db.create_all()` creates the three empty tables.
8. Visit `/health/db`; it should report `connected: true`.
9. Visit `/admin/login`, sign in with the `ADMIN_PASSWORD` value, then open `/admin`.
10. Verify counts are all zero before adding new records.

Do not run the reset SQL on every deployment. Do not add `DROP`, `TRUNCATE`, or reset logic to application startup.

## Database schema
The app creates these tables with SQLAlchemy:

- `village`: id, name, president_name, created_at
- `person`: id, village_id, serial_no, name, father_name, address, gender, created_at
- `access_link`: id, village_id, token_hash, expires_at, revoked, created_at

The person table has a unique constraint on `(village_id, serial_no)`.

## Deploy

Railway uses `railway.json` and `Procfile`. The start command is:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --access-logfile - --error-logfile -
```

## Health checks
- `/health`: app process status and whether a database variable is configured.
- `/health/config`: recognized variable names only; it never returns secrets.
- `/health/db`: verifies PostgreSQL connection and creates missing app tables if needed.

## Security notes
- Set a strong `ADMIN_PASSWORD` and `SECRET_KEY` in Railway Variables.
- President URLs are bearer links: anyone who has a valid link can access that village until expiry or revocation. Share them privately.
- The admin password form uses HTTPS on Railway. Use a strong password and do not share it.
- This build does not include role-based admin accounts, audit history, or CSRF tokens; those should be added before handling highly sensitive or regulated records.
