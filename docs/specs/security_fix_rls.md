# Security Fix: Supabase RLS Audit

**Date**: 2026-06-24  
**Severity**: High — all tables in `public` schema publicly readable/writable via Supabase REST API with anon key.

---

## Step 1 — Connection type verification

`backend/database.py` line 12:
```python
_raw_url = os.getenv("DATABASE_URL", "sqlite:///./founder_os.db")
```

The backend uses **SQLAlchemy with a direct PostgreSQL connection** (`postgresql://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres`).

This is NOT going through PostgREST. SQLAlchemy connects as the `postgres` superuser (database password), which bypasses RLS entirely. **Enabling RLS will not break the backend.**

The Supabase public REST API (PostgREST) exposes all tables via the anon key — that is the attack surface being closed.

---

## Step 2 — RLS migration

Migration file: `backend/migrations/m008_enable_rls.py`

- SQLite (local dev): skipped entirely
- PostgreSQL (Supabase): runs `ALTER TABLE public.[table] ENABLE ROW LEVEL SECURITY` for all 30 tables
- Idempotent: safe to run multiple times

Tables covered:
`weekly_targets`, `daily_logs`, `log_impacts`, `weekly_target_snapshots`, `ai_memory`, `habits`, `annual_targets`, `kpi_snapshots`, `roadmap_tasks`, `todos`, `chat_messages`, `bible_entries`, `daily_bible_log`, `books`, `social_scores`, `clients`, `revenue`, `subjects`, `topics`, `subtopics`, `daily_health`, `lifts`, `lift_logs`, `weekly_health`, `prop_firm_accounts`, `backtest_trades`, `live_trades`, `documents`, `document_chunks`, `non_negotiables`, `reading_plans`, `reading_plan_entries`, `cold_archive_chunks`

---

## Step 3 — SQL to apply immediately via Supabase SQL Editor

Paste this into **Supabase Dashboard → SQL Editor** for immediate effect (no deploy needed):

```sql
ALTER TABLE public.weekly_targets             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_logs                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.log_impacts                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.weekly_target_snapshots    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_memory                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.habits                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.annual_targets             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.kpi_snapshots              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.roadmap_tasks              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.todos                      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_messages              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bible_entries              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_bible_log            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.books                      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.social_scores              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.clients                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.revenue                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subjects                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.topics                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subtopics                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_health               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lifts                      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lift_logs                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.weekly_health              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prop_firm_accounts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backtest_trades            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.live_trades                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_chunks            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.non_negotiables            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reading_plans              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reading_plan_entries       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cold_archive_chunks        ENABLE ROW LEVEL SECURITY;
```

**No policies are added** — RLS on with no policies = all PostgREST/anon access denied. Backend direct connection is unaffected.

---

## Step 4 — Verification checklist

After applying RLS:

- [ ] **Public REST blocked**: `curl "https://[REF].supabase.co/rest/v1/todos?select=*" -H "apikey: [ANON_KEY]"` → returns `[]` or `{"message":"...","hint":"...","code":"42501"}`
- [ ] **Backend still works**: Hit `https://[your-render-url]/kpis` → returns KPI data normally
- [ ] If backend returns 500, stop and check Render logs — means DATABASE_URL is using anon key (not expected given SQLAlchemy setup)

---

## Step 5 — Rotate database password

The current `DATABASE_URL` password may be compromised since the database has been publicly accessible.

1. **Supabase Dashboard → Project Settings → Database → Reset database password**
2. Copy new password
3. **Render Dashboard → Environment → DATABASE_URL** → update password in the connection string
4. Trigger a manual redeploy on Render
5. Monitor Render logs to confirm backend reconnects successfully

---

## Risk assessment

| Vector | Before fix | After fix |
|--------|-----------|-----------|
| Supabase REST API (anon key) | Full read/write on all 33 tables | Blocked (RLS, no policies) |
| Backend SQLAlchemy (direct PG) | Full access (superuser) | Full access unchanged (bypasses RLS) |
| Supabase Dashboard SQL Editor | Full access | Full access |
| Render env DATABASE_URL leaked | Full access | Mitigated by password rotation |
