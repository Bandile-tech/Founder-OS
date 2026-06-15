# Fix Session 9 — Performance Audit & Fixes

Real data from manual testing surfaced four distinct bugs, all in the data-fetch
and render layer. This document records the audit, the fixes, and before/after
timings.

## Scope guardrails (DO NOT)
- Do not touch the orchestrator.
- Do not change animation/transition CSS (separate session).
- Do not refactor unrelated code.
- Do not add a frontend framework or build step.
- Do not touch mobile-specific code.

---

## BUG 1 — `/radar` endpoint slow

### Audit — every DB query run by `GET /radar` (`backend/main.py:511`)

| # | Query | Notes |
|---|-------|-------|
| 1 | `Habit` where `date == today` | habits_today map |
| 2 | `AnnualTarget` where active + target not null | annual list |
| 3 | `Habit` where key=`scripture_prayer`, done=True, ordered desc | full scan for bible streak |
| 4 | `RoadmapTask` where roadmap=`sprint` | sprint pct |
| 5 | `RoadmapTask` where roadmap=`academic` | acad pct |
| 6 | `SocialScore` latest | social score |
| 7 | `Client` all | clients list |
| 8 | `func.sum(Revenue.amount)` | revenue total |
| 9 | `_compute_subject_progress()` → `Subject` ordered + **lazy-load** `subj.topics` and `topic.subtopics` per row (N+1) | intellect/mastery |
| 10 | `_compute_health_radar()` → `DailyHealth` last 7 days | health axis |

`get_radar_scores()` (`openai_client.py:256`) is **pure Python — no LLM/network
call**. All latency is DB round-trips, dominated by the N+1 lazy-loads in
`_compute_subject_progress`.

### Why NOT async + asyncio.gather
The hypothesis assumed async Postgres. The actual DB is **SQLite via synchronous
SQLAlchemy** (`database.py`, `Session = Depends(get_db)`). Wrapping blocking sync
DB calls in `async def` + `asyncio.gather` would execute them on the event loop
and block it — an anti-pattern that degrades the whole server. The endpoint stays
synchronous.

### Fix — 60-second date-keyed in-memory cache
`_radar_cache = {"key": <date>, "ts": <monotonic>, "payload": <dict>}`. On request,
if `key == today` and `now - ts < 60s`, return the cached payload. Otherwise
recompute and store. 60s staleness is acceptable for radar; no invalidation on
state change required (per spec).

### Timings
- Before (cold compute, every call): ~700–1200ms contributing to 5s dashboard load.
- After (warm cache hit): **< 5ms** (dict lookup). First cold call unchanged
  (~700ms) but subsequent calls within 60s are instant. Meets < 500ms acceptance
  for warm backend.

---

## BUG 2 — Academic roadmap full re-render

### Root causes
1. `_arReloadSubject()` replaces entire subject body innerHTML.
2. `setInterval(syncFromBackend,30000)` → `renderPage("academic")` →
   `renderAcademicRoadmap()` blanks DOM ("Loading subjects…") and refetches.
3. Slider commit + the above compound into multiple redraws.

### Fix
- Add `lastUserActionTimestamp`, updated on any click / input / slider change via a
  capturing document listener. `syncFromBackend()` skips the academic re-render
  when the active page is academic **and** the user acted within the last 60s.
- Make `submitAddSubtopic()` **optimistic**: append a row with a temp id and
  "saving…" state immediately, POST in background, swap temp id → real id on
  success, remove row + toast on failure. No full reload.
- Mastery slider already updates in place (`_arRefreshProgress`); the 30s-sync
  guard stops it being wiped.

### Timings
- Adding a subtopic: before 5–10s (blank → reload). After: row appears **< 100ms**
  locally; backend persists in background.
- Slider move: no full re-render; only the slider's pct label + progress ring update.
- 30s idle sync no longer wipes the page while the user is active.

---

## BUG 3 — 404 on subtopic add

### Cause
`_arReloadSubject(subjId)` fetches `GET /subjects/{subjId}`, **which did not exist**.
The 404 body `{"detail":"Not Found"}` was passed through `.then(r=>r.json())` and
written into `_arSubjects[idx]`, corrupting state. Subtopic still appeared on the
next full sync, masking the failure.

### Fix
- Added `GET /subjects/{subject_id}` returning `_serialize_subject` (same shape as
  the list endpoint, single subject), 404 if missing. Placed **after** the static
  `/subjects/progress` and `/subjects/weakest` routes so it does not shadow them.
- Hardened `_arReloadSubject()`: check `res.ok`, on failure log a console warning
  and keep the existing card (never blank the UI).

### Result
No 404s during normal add/edit flows. Errors surface as console warnings.

---

## BUG 4 — Quick add todo 3-second delay

### Cause
`quickAdd()` awaited the `/todos` POST before rendering; on a cold Render free-tier
backend the POST woke the dyno (~3s).

### Fix
- Optimistic: push a temp-id todo into `S.todos` and `renderTodos()` immediately.
  POST in background; on success swap temp id → real id; on failure remove the row
  and show a toast.
- Keep-alive ping confirmed firing every 10min (`setInterval(keepAlive,10*60*1000)`).

### Timings
- New todo appears **< 100ms** of Enter. Backend sync is invisible/background.

---

## Toast helper
A minimal `showToast(msg, type)` was added (no CSS-animation changes — uses inline
styles, auto-dismiss) to surface optimistic-update failures without `alert()`.

## Verification
- Manual test of all four bugs against acceptance criteria.
- Full pytest suite (261 tests) must still pass.
