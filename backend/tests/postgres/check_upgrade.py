"""
Step 2 of the upgrade check (see README.md in this folder).

Run from the ``backend`` folder of THIS branch, on the database that seed_from_main.py
filled, with MAIN_TABLES pointing at the table list that script wrote:

    PYTHONPATH=. DATABASE_URL=postgresql://… MAIN_TABLES=/path/MAIN_TABLES.txt python tests/postgres/check_upgrade.py

It runs the branch's startup chain twice and asserts that:
  * every seeded client is in DEFAULT_CURRENCY (ZMW) and none became USD
  * every revenue row with a client has exactly one paid payment, in that revenue row's own
    currency, for the same amount, client and day; revenue with no client has none
  * the payment backfill ran once (one row in migration_marks) and a second startup adds nothing
  * the rows that were there before are unchanged
  * row-level security is on for every table, the new ones included
  * the app answers on the upgraded data
  * a revenue row logged after the migration is matched to its invoice by reconciliation,
    not claimed by the backfill on the next restart
"""
import asyncio
import os
import sys
from datetime import date

from sqlalchemy import text

import main
import crm
import models
from database import SessionLocal, engine

assert engine.dialect.name == "postgresql", "point DATABASE_URL at the Postgres database from step 1"
main_tables = set(open(os.environ["MAIN_TABLES"]).read().split())


def scalar(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(sql):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql)).fetchall()]


before = {t: rows(f"select * from {t} order by id") for t in ("revenue", "todos", "chat_messages")}
clients_before = rows("select id, name, company, status, value, service, notes from clients order by id")
assert clients_before, "the database is empty: run seed_from_main.py first"

asyncio.run(main.startup())
with_client = scalar("select count(*) from revenue where client_id is not null")
first = scalar("select count(*) from payments")
asyncio.run(main.startup())

checks = {
    "clients not in ZMW": scalar("select count(*) from clients where currency is distinct from 'ZMW'"),
    "payments that differ from their revenue row (amount, client, day, status)": scalar(
        "select count(*) from payments p join revenue r on r.id = p.revenue_id "
        "where p.amount <> r.amount or p.client_id <> r.client_id or p.paid_at <> r.date or p.status <> 'paid'"),
    "payments whose currency is not the revenue row's": scalar(
        "select count(*) from payments p join revenue r on r.id = p.revenue_id "
        "where p.currency <> upper(coalesce(r.currency, 'USD'))"),
    "revenue rows with a client and no payment": scalar(
        "select count(*) from revenue r where r.client_id is not null "
        "and not exists (select 1 from payments p where p.revenue_id = r.id)"),
    "revenue rows with two payments": scalar(
        "select count(*) from (select revenue_id from payments where revenue_id is not null "
        "group by revenue_id having count(*) > 1) x"),
    "payments for revenue with no client": scalar(
        "select count(*) from payments p join revenue r on r.id = p.revenue_id where r.client_id is null"),
    "payments added by the second startup": scalar("select count(*) from payments") - first,
    "tables without row-level security": scalar(
        "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity"),
}
for label, count in checks.items():
    print(f"  {'ok ' if count == 0 else 'BAD'} {label}: {count}")
assert first == with_client, f"{first} payments for {with_client} revenue rows with a client"
assert scalar("select count(*) from migration_marks where key = 'm016_payments_backfill'") == 1
assert rows("select id, name, company, status, value, service, notes from clients order by id") == clients_before
after_cols = {"revenue": "*", "chat_messages": "*",
              "todos": "id, text, priority, done, category, due, due_date, source, roadmap_id, completed_at"}
for table, cols in after_cols.items():
    now = rows(f"select {cols} from {table} order by id")
    was = before[table] if cols == "*" else [r[:len(cols.split(','))] for r in before[table]]
    if table == "revenue":      # m012 (already on main) gives a revenue row with no currency USD
        now = [r[:2] + r[3:] for r in now]
        was = [r[:2] + r[3:] for r in was]
    assert now == was, f"{table} rows changed"
all_tables = {r[0] for r in rows("select relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                 "where n.nspname = 'public' and c.relkind = 'r'")}
print(f"  ok  existing rows unchanged; {len(all_tables - main_tables)} new tables, all with row-level security")

from fastapi.testclient import TestClient
client = TestClient(main.app)
for path in ("/clients", "/crm/summary", "/briefing/today", "/conversation/history", "/agents", "/integrations",
             "/portfolio", "/leads", "/applications", "/revenue", "/payments", "/notifications/settings"):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:200])
print("  ok  the app answers on the upgraded data")

db = SessionLocal()
someone = db.query(models.Client).filter(models.Client.status == "active").first()
invoice = crm.record_payment(db, someone, 500, "ZMW", status="due", title="Upgrade check invoice")
db.add(models.Revenue(amount=500, currency="ZMW", source=someone.name, client_id=someone.id, date=date.today()))
db.commit()
db.close()
imported = scalar("select count(*) from payments where title = 'Imported'")
asyncio.run(main.startup())                                     # a restart
assert scalar("select count(*) from payments where title = 'Imported'") == imported, "the backfill ran again"
assert client.get("/crm/summary").status_code == 200            # housekeeping reconciles
status, linked = rows(f"select status, revenue_id is not null from payments where id = {int(invoice['id'])}")[0]
assert (status, linked) == ("paid", True), (status, linked)
print("  ok  a later revenue row was matched to its invoice by reconciliation, not by the backfill")

bad = [label for label, count in checks.items() if count]
if bad:
    print("FAILED:", "; ".join(bad))
    sys.exit(1)
print("UPGRADE CHECK PASSED")
