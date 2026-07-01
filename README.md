# Founder OS

A personal operating system I built for myself — not a product, not a client tool. It's the system I use daily to run my academics, health, trading discipline, and business execution against my own written standards, not generic productivity defaults.


## What it does

- **Academic tracking** — syllabus progress by subject, topic, and subtopic, weighted against actual exam dates
- **Health** — daily and weekly logs, lift progression
- **Trading** — backtest logging with a hard-coded 50-trade / 90%-adherence gate before live trading unlocks. The gate is enforced in code, not by willpower.
- **Non-negotiables** — daily habit tracking against standards I've set for myself
- **AI layer** — an orchestrator that reasons from documents I've written (personal doctrine, standing rules, reading plans) rather than generic advice, and can log brain dumps, surface off-track alerts, and answer questions grounded in what I've actually said matters to me

## What it isn't

Not a SaaS product. Not built for other users. No auth layer, no multi-tenancy — it's scoped to one person's data because that's the only person it was built for.

## Stack

FastAPI + vanilla JS frontend, Supabase (Postgres), deployed on Render (backend) and Netlify (frontend). AI orchestration via OpenAI's API.

## Why

Every domain in here — academics, training, trading, building — runs on the same principle: the standard is worthless if nothing enforces it when no one's watching. This is the enforcement layer.

---

Built solo. First full build, shipped and iterated on in public.
