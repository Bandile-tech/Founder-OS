# Founder OS — Roadmap

Eight phases. Phases 1–4 have their core built (the intelligence-layer rebuild, Stages 1–6),
and so do Phases 5 and 6. Phase 7 is in progress; Phase 8 is planned. The product vision, with
what is built and what is missing in each area, is `docs/FOUNDER_OS_2_SPEC.md`.

## Phase 1 — Foundation and security · core built
- One OpenAI gateway with a lazy client; the app and tests run with no key.
- Single-password login with signed session tokens; every route protected except health,
  ping and login; CORS restricted to the app's origin.
- Re-runnable migrations on SQLite and Postgres, RLS on every table, schema check at startup.
- Change log on every write, with undo; deletes need an explicit yes.
- Usage logging and cost estimates for every model call.
- Remaining: set a dedicated `FOUNDER_OS_SECRET` on Render; backfill embeddings once a key
  is set (`retrieval.index_notes/memories`).

## Phase 2 — Conversational intelligence · core built
- Orchestrator with 22 tools, server-side conversation shared across devices, history budget
  with rolling summary, long-term memory recalled every turn.
- Parser with intent classification (commitment / idea / discussion) and confidence; low
  confidence becomes a one-tap suggestion.
- Meaning-based retrieval across the Obsidian vault, notes and memory, searched proactively.
- Real-time voice over WebRTC with the same tools, change log and confirmation rules; the
  record → transcribe → speak loop as fallback.
- Remaining: a live voice smoke test with a real key; voice selection by ear.

## Phase 3 — Command centre and planning · core built
- `GET /briefing/today`: ranked, deterministic brief (overdue, today, deadlines, non-negotiables,
  exams, syllabus priorities, critical targets, alerts), progress, one cached AI summary a day.
- Home screen built on it: greeting, Today's Brief with undoable checkboxes, progress bars,
  talk bar, alerts bell, Academics tile.
- Changes with Undo and suggestions with Accept inside the command dock.
- Remaining: weekly planning view; timetable driven by the brief; the orb and "Ready when you
  are" presence from the design.

## Phase 4 — Personal and academic management · core built
- Syllabus tracker: four states, per subject and paper, one tap to advance, CSV import from the
  tracker sheet, `update_syllabus` from conversation, weakest topics by status, staleness,
  weakness notes and exam proximity.
- Exam dates per paper set from conversation.
- Notes for books, Bible, journal, inbox and general, with reading progress appended from
  brain dumps.
- Health, timetable and annual targets carried over from the earlier build.
- Remaining: retire the legacy subject/topic/subtopic tables once the tracker has a full
  term of data; a notes UI; past-paper score trends.

## Phase 5 — Business · core built
- Market intelligence v2: weighted opportunity scoring, standing research objectives, a
  pipeline with next actions, cost per run.
- Leads and outreach: CSV import, qualification against the ideal customer profile, drafted
  messages that only leave after a tap, follow-up tasks, reply and conversion tracking.
- CRM: clients, contacts, deals, payments (with recurring instalments and overdue tracking),
  communications, client health.
- Project portfolio: focus / maintain / pause / close recommendations, blockers, weekly review.
- Personal brand: YouTube sync, LinkedIn export import, performance, a weekly insight, ideas
  and a content calendar. Only numbers the platforms report are shown.
- The Business page (Pipeline, Clients, Portfolio, Brand) and the Business, Projects and
  Content tiles on the home screen.
- Remaining: a button for lead import and for qualifying leads (both work from conversation
  and the API); a default country code so WhatsApp links work for local numbers; the X export
  importer; a Disconnect control on the Brand tab; the client KPI strip in Kwacha.
Specification: `docs/phases/PHASE_5_SPEC.md`; build sequence: `docs/phases/BUILD_ORDER.md`;
what was built and every decision: `docs/phases/PROGRESS.md`.

## Phase 6 — Long-term goals, agents, integrations, proactive communication · core built
- Applications: deadlines that come only from Bandile or from text he pastes, a requirements
  checklist, a gap review, countdown alerts.
- Agents: one registry and one runner on a 15-minute tick, with a permission audit, a cost cap
  and an activity log with Undo per run. Twelve agents ship.
- Integrations: Google Calendar (OAuth, a cached copy, events created from conversation and
  undoable), email over IMAP and SMTP (replies from known people only, approved outreach is
  really sent), Telegram pairing. Secrets are never returned or logged.
- Proactive communication: a morning briefing, alerts, deadline countdowns and reminders to
  Telegram, with quiet hours and a daily cap; replies in Telegram go to the assistant, with
  `/undo`, `/yes`, `/no`, `/brief` and `/mute`.
- The Agents and Applications pages, the Integrations and Notifications cards in Settings,
  and today's events on the home screen.
- Remaining: the external accounts have to be set up (Telegram bot, Google Cloud project,
  Gmail App Password; steps in `docs/phases/PROGRESS.md`) and the items marked VERIFY there
  checked against the live services; the 15-minute GitHub schedule is shipped switched off
  (see ADR-0001), so nothing runs while the Render instance is asleep; a read-only ICS
  fallback for the calendar; Web Push as a second channel; voice notes in Telegram; one
  app-wide "today" in the user's time zone; a Postgres run of the suite for migrations
  m014–m022.
Specification: `docs/phases/PHASE_6_SPEC.md`; decisions: `docs/adr/`; build sequence:
`docs/phases/BUILD_ORDER.md`. Patterns both phases follow: `docs/CONVENTIONS.md`.

## Phase 7 — Model switching, money ledger, live second brain, proactive delivery · in progress
Four things and nothing else. Not merged: nothing below is done until the `phase-7` pull
request is.
- **Model router.** Every model call through one gateway that looks up a route per task
  (provider, model, parameters). OpenAI and Claude behind two small adapters. Routes changed by
  conversation or from Settings, with undo. One retry on the other provider when a call fails.
  Spend by provider and by task, and a monthly cap that moves the expensive routes to the
  cheapest model at 80 percent.
- **Personal finance ledger.** Buckets, transactions in whole ngwee, allocation rules. "Spent
  K50 on lunch, got K800 from Bel" is one batch and one Undo. A client payment from the CRM is
  the same money, recorded once. A Budget view.
- **Live vault.** A GitHub push webhook that re-reads only the files that changed, the time of
  the last sync in Settings, and a daily export of the assistant's notes into the vault folder
  `Founder OS/`.
- **Proactive delivery.** The GitHub Actions tick three times a day (05:45, 12:30, 19:45
  Lusaka), Web Push as the first channel, the briefing, an evening check, deadline, exam and
  payment reminders, quiet hours. The two open approval-gate items closed first.
- **Claude connector (Stage 5).** Only after Bandile approves the authentication design;
  otherwise it moves to Phase 8.
Specification: sections 14 to 17 of `docs/FOUNDER_OS_2_SPEC.md`.

## Phase 8 — Connector, Brain workspace, Telegram · planned
- The Claude connector (MCP), if it was not built in Phase 7: Founder OS as a connector that
  Bandile's Claude projects query directly (search the brain, briefing, tasks, finance summary,
  append a note; writes behind a switch that is off by default).
- A Brain workspace: one place to browse and search the vault, notes and memory, and to see
  what the assistant knows and where it came from.
- Telegram, once it is unpaused: set up the bot, check the VERIFY items against the live
  service, and run it beside Web Push.
- Later, not scheduled: WhatsApp as a channel and outbound voice calls. Both need a paid
  provider and Bandile's explicit yes.
