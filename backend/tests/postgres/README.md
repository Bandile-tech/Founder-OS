# Upgrade check on PostgreSQL

Proves that the branch's migrations are safe on a database that looks like production:
main's schema, RLS on, and real-looking rows. Needs a PostgreSQL 16 server and two
checkouts (main and the branch). Not collected by pytest.

```
createdb fos_upgrade                                   # empty and throwaway
export DATABASE_URL=postgresql://USER:PASS@127.0.0.1/fos_upgrade OPENAI_API_KEY= JOBS_IN_PROCESS=0

git worktree add /tmp/founder-os-main main
cd /tmp/founder-os-main/backend
PYTHONPATH=. python <branch>/backend/tests/postgres/seed_from_main.py      # step 1, on MAIN's code

cd <branch>/backend
PYTHONPATH=. MAIN_TABLES=/tmp/founder-os-main/backend/MAIN_TABLES.txt python tests/postgres/check_upgrade.py
```

The whole pytest suite on Postgres (every test drops and recreates the schema, so use
another throwaway database; it takes about ten minutes):

```
TEST_DATABASE_URL=postgresql://USER:PASS@127.0.0.1/fos_test python -m pytest -q
```
