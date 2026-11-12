"""
Step 1 of the upgrade check (see README.md in this folder).

Run from the ``backend`` folder of a checkout of MAIN, with DATABASE_URL pointing at an
empty, throwaway Postgres database:

    PYTHONPATH=. DATABASE_URL=postgresql://… python <path to this file>

It builds main's schema through main's own startup chain, turns RLS on as production has
it (m008), and seeds rows that mirror production: clients, revenue in ZMW and USD, todos,
chat history. It writes the list of main's tables to MAIN_TABLES.txt in the current folder.
"""
import asyncio, json
from datetime import date, datetime, timedelta

import main
import models
from database import SessionLocal, engine
from sqlalchemy import text

asyncio.run(main.startup())
from migrations.m008_enable_rls import run as run_m008
run_m008()

db = SessionLocal()
D = date(2026, 11, 12)

clients = [
    models.Client(name="Wamu's Bakes & Cakes", company="Wamu's Bakes", status="active", value=3500, service="Order bot", notes="Monthly retainer"),
    models.Client(name="Hope Clinic", company="Hope Clinic Lusaka", status="active", value=8000, service="Booking portal"),
    models.Client(name="Bel Baby", company="Bel Baby Zambia", status="closed", value=1200, service="Landing page"),
    models.Client(name="Sunrise Pharmacy", company="Sunrise", status="prospect", value=0, service=""),
    models.Client(name="Kitwe Motors", company="", status="lost", value=500, service="Website"),
]
db.add_all(clients)
db.commit()
wamu, hope, bel, sunrise, kitwe = [c.id for c in clients]

revenue = [
    # (amount, currency, source, client_id, days ago)
    (3500, "ZMW", "Wamu's Bakes & Cakes", wamu, 70),
    (3500, "ZMW", "Wamu's Bakes & Cakes", wamu, 40),
    (3500, "ZMW", "Wamu's Bakes & Cakes", wamu, 10),
    (150, "USD", "Hope Clinic", hope, 55),
    (4000, "ZMW", "Hope Clinic", hope, 20),
    (120.5, "USD", "Bel Baby", bel, 120),
    (900, "ZMW", "Walk-in tutoring", None, 5),          # no client: must get no payment
    (45, "USD", "Fiverr gig", None, 33),
    (2500, "zmw", "Hope Clinic", hope, 3),              # lower case, as an old client could have sent it
]
for amount, currency, source, client_id, ago in revenue:
    db.add(models.Revenue(amount=amount, currency=currency, source=source, client_id=client_id,
                          date=D - timedelta(days=ago), notes=""))
db.commit()
# A row from before revenue.currency existed, had the m012 backfill not reached it.
db.execute(text("INSERT INTO revenue (amount, currency, source, client_id, date, notes, created_at) "
                "VALUES (75, NULL, 'Bel Baby', :c, :d, '', now())"), {"c": bel, "d": D - timedelta(days=200)})
db.commit()

todos = [
    models.Todo(text="Finalise Bel Baby delivery", priority=1, done=False, category="business", due="2026-11-12", due_date=D, source="chat"),
    models.Todo(text="Past paper: 9709 P3 2023", priority=2, done=False, category="academics", due="", due_date=D - timedelta(days=2), source="chat"),
    models.Todo(text="Renew domain", priority=4, done=True, category="business", due="", due_date=D - timedelta(days=9), source="chat", completed_at=D - timedelta(days=9)),
    models.Todo(text="Read Zero to One ch. 4", priority=5, done=False, category="personal", due="someday", due_date=None, source="chat"),
]
db.add_all(todos)

t0 = datetime(2026, 11, 10, 6, 30)
calls = [{"id": "call_1", "type": "function", "function": {"name": "route_brain_dump", "arguments": json.dumps({"text": "K3500 from Wamu"})}}]
history = [
    ("user", "Got K3500 from Wamu today", None, None, None),
    ("assistant", "", json.dumps(calls), None, None),
    ("tool", json.dumps({"revenue_logged": [{"amount": 3500, "currency": "ZMW"}]}), None, "call_1", None),
    ("assistant", "Logged K3,500 from Wamu's Bakes & Cakes.", None, None, json.dumps([{"id": 1, "summary": "Revenue K3,500", "batch_id": "b1"}])),
    ("user", "What is due today?", None, None, None),
    ("assistant", "Finalise the Bel Baby delivery, and the 9709 P3 paper is two days late.", None, None, None),
]
for n, (role, content, tool_calls, tool_call_id, changes) in enumerate(history):
    db.add(models.ChatMessage(session_id="default", thread_id="default", role=role, content=content,
                              tool_calls_json=tool_calls, tool_call_id=tool_call_id, changes_json=changes,
                              created_at=t0 + timedelta(minutes=n)))
db.add(models.Habit(key="scripture_prayer", label="Scripture & prayer", done=True, date=D))
db.add(models.Habit(key="ironing", label="Ironing", done=False, date=D))
db.commit()

print("SEEDED:", {t: db.execute(text(f"select count(*) from {t}")).scalar()
                  for t in ("clients", "revenue", "todos", "chat_messages", "habits")})
cols = [r[0] for r in db.execute(text("select column_name from information_schema.columns "
                                      "where table_name='clients' order by ordinal_position")).fetchall()]
print("clients columns on main:", cols)
tables = [r[0] for r in db.execute(text("select table_name from information_schema.tables "
                                        "where table_schema='public' order by 1")).fetchall()]
open("MAIN_TABLES.txt", "w").write("\n".join(tables))
print("tables on main:", len(tables))
db.close()
