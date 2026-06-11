# Database Setup

Founder OS uses SQLAlchemy with a dual-mode database configuration:

- **Local development** — SQLite file at `backend/founder_os.db` (default, no setup required)
- **Production (Render)** — PostgreSQL via Supabase, configured through the `DATABASE_URL` environment variable

---

## How the mode is selected

`backend/database.py` reads `DATABASE_URL` from the environment:

| `DATABASE_URL` value | Mode |
|---|---|
| Not set | SQLite (`founder_os.db`) |
| `sqlite:///...` | SQLite (explicit) |
| `postgresql://...` | Postgres |
| `postgres://...` | Postgres (legacy prefix auto-corrected) |

The `postgres://` → `postgresql://` translation is applied automatically.  
Supabase connection strings already use `postgresql://` (correct format).

---

## Local development (SQLite)

No setup needed. Run:

```bash
cd backend
uvicorn main:app --reload
```

The SQLite file is created automatically at `backend/founder_os.db`.

---

## Local development with Postgres (optional)

If you want to test against a real Postgres instance locally:

### Option A — Docker

```bash
docker run --name founder-os-pg \
  -e POSTGRES_DB=founder_os \
  -e POSTGRES_USER=founder \
  -e POSTGRES_PASSWORD=founder \
  -p 5432:5432 -d postgres:15
```

Then start the backend with:

```bash
DATABASE_URL=postgresql://founder:founder@localhost:5432/founder_os \
  uvicorn main:app --reload
```

### Option B — Local Postgres install

```bash
createdb founder_os
DATABASE_URL=postgresql://localhost/founder_os uvicorn main:app --reload
```

---

## Production setup (Render + Supabase)

### Step 1 — Create a Supabase project

1. Go to [supabase.com](https://supabase.com) and create a free project.
2. Wait for the database to provision (~2 minutes).

### Step 2 — Get the connection string

1. In your Supabase project, go to **Settings → Database**.
2. Scroll to **Connection string** and select **URI** mode.
3. Copy the URI — it looks like:
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres
   ```

### Step 3 — Set the environment variable on Render

1. In the Render dashboard, open your backend web service.
2. Go to **Environment → Environment Variables**.
3. Add a new variable:
   - **Key**: `DATABASE_URL`
   - **Value**: (the URI from Step 2)
4. Save. Render will trigger a redeploy.

### Step 4 — First deploy

On the first startup, the backend will:
1. Run `Base.metadata.create_all()` to create all tables in the Supabase DB.
2. Run seed functions: habits, annual targets, roadmap tasks, bible plan, books.
3. Run `seed_subjects()` (m002) — inserts 4 A-level subjects and their topics.
4. Run `run_m003()` — seeds 5 default lifts.
5. Log integrity check results — look for `[startup] Subjects OK` and `[startup] Lifts OK` in Render logs.

### Step 5 — Verify

Hit the live API:

```
GET https://[your-render-url]/subjects
# Should return 4 subjects

GET https://[your-render-url]/health/lifts
# Should return 5 lifts
```

---

## Running tests

Tests always use an **in-memory SQLite database** regardless of `DATABASE_URL`.  
`backend/tests/conftest.py` hardcodes `sqlite:///:memory:` with `StaticPool` and overrides the FastAPI dependency. Tests are fully isolated from the production database.

```bash
cd backend
pytest tests/           # uses in-memory SQLite — fast, always clean
```

To run tests against a real Postgres (advanced):

```bash
# Not the default — requires manual conftest.py modification
DATABASE_URL=postgresql://localhost/founder_os_test pytest tests/
```

---

## Backing up Supabase data

Supabase free tier includes:

- **Point-in-time recovery** is not included on free tier.
- **Manual backups**: go to **Database → Backups** in the Supabase dashboard. Free tier supports daily snapshots retained for 7 days.
- **pg_dump** (manual):
  ```bash
  pg_dump [YOUR-DATABASE-URL] > founder_os_backup_$(date +%Y%m%d).sql
  ```

---

## Migration notes

Migrations live in `backend/migrations/`:

| File | Purpose | Postgres-safe? |
|---|---|---|
| `001_dailylog_nullable_target.py` | Makes `weekly_target_id` nullable | Yes — skips on non-SQLite (ORM baseline is already correct) |
| `m002_academic_roadmap.py` | Seeds 4 A-level subjects + topics | Yes — pure SQLAlchemy ORM |
| `m003_lift_log.py` | Moves lift columns out of `daily_health`; seeds 5 default lifts | Yes — uses `information_schema` on Postgres, `PRAGMA` on SQLite |

All migrations are idempotent. Restarting the service is safe.
