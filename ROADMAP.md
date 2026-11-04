# Founder OS — Roadmap

Six phases. Phases 1–4 have their core built (the intelligence-layer rebuild, Stages 1–6).
Phases 5 and 6 are planned and deliberately not started.

## Phase 1 — Foundation and security · core built
- One OpenAI gateway with a lazy client; the app and tests run with no key.
- Single-password login with signed session tokens; every route protected except health,
  ping and login; CORS restricted to the app's origin.
- Re-runnable migrations on SQLite and Postgres, RLS on every table, schema check at startup.
- Change log on every write, with undo; deletes need an explicit yes.
- Usage logging and cost estimates for every model call.
- Remaining: rotate the password-derived secret to a dedicated `FOUNDER_OS_SECRET`; rate
  limiting on login; backfill embeddings once a key is set (`retrieval.index_notes/memories`).

## Phase 2 — Conversational intelligence · core built
- Orchestrator with 22 tools, server-side conversation shared across devices, history budget
  with rolling summary, long-term memory recalled every turn.
- Parser with intent classification (commitment / idea / discussion) and confidence; low
  confidence becomes a one-tap suggestion.
- Meaning-based retrieval across the Obsidian vault, notes and memory, searched proactively.
- Real-time voice over WebRTC with the same tools, change log and confirmation rules; the
  record → transcribe → speak loop as fallback.
- Remaining: verify the realtime model id against OpenAI's docs; a live smoke test; voice
  selection by ear.

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

## Phase 5 — Business · planned
Prospecting, outreach, CRM, personal-brand analytics and a project portfolio. The Business
tab and tiles were removed from the UI until this phase is designed around conversation
rather than forms. Revenue (with currency) and clients remain in the database.

## Phase 6 — Long-term goals, agents, integrations, proactive communication · planned
Applications such as Wharton (deadlines, gaps, materials), agent management, external
integrations (calendar, email, push), and the system reaching out on its own when something
needs attention. The market-intelligence agent from the earlier build is the seed of the
agent track.
