# Phase 6 — Orchestrator

Single orchestrator endpoint replacing the unified-input keyword router, with SSE
streaming, six tools, gate-awareness, and off-track alerts on dashboard load.

## Hard constraints (DO NOT violate)

- **Model**: read from env `ORCHESTRATOR_MODEL`, default `gpt-5.4`. Never hardcode the model name.
- **Do not touch**: `/parse`, `/input`, `/chat` endpoints (kept alive; orchestrator calls
  parse logic internally), `apply_parse_updates()`, `_compute_gate()` / HTTP 423 enforcement,
  any existing test file.
- **No WebSockets** — SSE only (`StreamingResponse`, `media_type="text/event-stream"`).
- Orchestrator has **no autonomous authority** to write todos, KPIs, or priorities — it can
  only log via `route_brain_dump` (which routes through the existing, audited parse path) when
  the user is explicitly logging.
- Tests **mock OpenAI** — no real API calls.

## New files

| File | Role |
|------|------|
| `backend/orchestrator.py` | SSE stream generator + alerts runner; bridges request ↔ tool loop |
| `backend/orchestrator_tools.py` | The six tools, their JSON schemas, and `run_tool` dispatcher |
| `context/core/orchestrator_prompt.md` | System prompt (contains verbatim gate rule) |
| `context/core/off_track_rules.json` | Data-driven alert rules (each with `enabled` flag) |
| `tests/test_orchestrator.py` | Full suite (tool-level, integration, SSE) |

## Modified files

| File | Change |
|------|--------|
| `backend/main.py` | Register `POST /orchestrator` (SSE) and `GET /orchestrator/alerts` (JSON) |
| `backend/openai_client.py` | Add `get_orchestrator_response()` — `stream=True` + tool-call loop; `orchestrator_model()` env helper |
| `frontend/index.html` | Replace `submitUnifiedInput()` with SSE reader; add alerts card; delete `_QUERY_STARTERS` + routing |

## Six tools

1. `route_brain_dump(text)` — calls existing `apply_parse_updates` via `main.get_parse_response`. Use when logging.
2. `get_dashboard_state()` — KPIs, habits done/total, todos active, gate status, 10K progress, weakest subject. **Must include `gate_status`.**
3. `detect_off_track()` — loads rules from JSON, returns `[{severity, message, rule_id}]`. Data-driven.
4. `synthesize_weekly_review(week_start)` — four sections: SHIPPED, REVENUE, NEGOTIATED, NEXT.
5. `surface_weakest_subtopic(limit=1)` — wraps `/subjects/weakest`.
6. `query_war_room(q)` — wraps `/context/search`.

## Off-track rules (initial)

Each rule: `{id, severity, enabled, check, params, message}`. `check` maps to an evaluator in
`orchestrator_tools._RULE_CHECKS`; adding a rule that reuses an existing `check` needs **no code change**.

- `no_revenue_30d` (critical) — no revenue in 30 days after 2026-06-01
- `habit_drift_7d` (warn) — habit completion under 60% over 7 days
- `annual_target_critical` (critical) — any annual target status = critical
- `backtest_stalled` (warn) — no new backtests in 7 days while gate LOCKED
- `zero_mastery_high_weight` (info) — subtopic mastery=0 in topic with weight>=7

## SSE event types (on `data:` lines)

- `{"type": "reasoning", "content": "..."}`
- `{"type": "tool_call", "tool": "...", "args": {...}}`
- `{"type": "tool_result", "tool": "...", "summary": "..."}`
- `{"type": "final", "content": "..."}`

`GET /orchestrator/alerts` — plain JSON array (empty if none), runs `detect_off_track()`.

## Gate rule (verbatim in system prompt)

> The trading gate is LOCKED or CLEARED. When LOCKED, live trades cannot be logged — enforced
> in code at HTTP 423. You can explain gate status. You cannot bypass it. Do not call any tool
> that would log a live trade when LOCKED.

## Frontend flow

Replace `submitUnifiedInput()`: POST `/orchestrator`, read SSE via fetch + ReadableStream.
- `reasoning` → append live to `#ucmd-result`
- `tool_call` → inline `⚙ [tool]…` indicator
- `tool_result` → clear indicator
- `final` → render; if `route_brain_dump` ran show applied-chips, else `mdHtml()`
- after stream → `syncFromBackend()`

Delete `_QUERY_STARTERS` + routing. Add alerts card above command card; fetch on init + every
5 min; hide if no alerts; per-alert dismiss (frontend-only `Set` of dismissed rule_ids).

## Execution order

1. `orchestrator_prompt.md` + `off_track_rules.json`
2. `orchestrator_tools.py`
3. Tool-level tests → pytest green
4. `orchestrator.py` + `get_orchestrator_response()`
5. Register routes in `main.py`
6. Integration + SSE tests → pytest green
7. Replace `submitUnifiedInput()` + alerts card
8. Manual smoke test
9. Full pytest — existing suite must still pass

Git diff stat after each major step. No commit until all green.

## Test inventory

Tool-level:
- "did scripture today" via `route_brain_dump` → habit toggled
- `get_dashboard_state` returns `gate_status`
- empty revenue after 2026-06-01 → critical alert
- rule `enabled: false` → alert not returned
- `synthesize_weekly_review` has all four sections
- two zero-mastery subtopics — higher topic weight surfaces first

Integration:
- "logged 50 push-ups" → `route_brain_dump` called, parse ran
- "what should I focus on" → `route_brain_dump` NOT called
- Gate LOCKED + "log live trade +2R" → refuses, no DB write
- "what should I study tonight" → `get_dashboard_state` then `surface_weakest_subtopic`

SSE:
- reasoning events precede final
- each tool_call followed by tool_result

## Step log (filled during implementation)

- [x] Step 1 — `context/core/orchestrator_prompt.md` (gate rule verbatim) + `off_track_rules.json` (5 rules, each `enabled`)
- [x] Step 2 — `backend/orchestrator_tools.py` (6 tools, `_RULE_CHECKS` registry, `TOOL_SCHEMAS`, `run_tool`)
- [x] Step 3 — tool-level tests green (6/6)
- [x] Step 4 — `backend/orchestrator.py` + `get_orchestrator_response()` / `_stream_completion()` / `orchestrator_model()` in openai_client.py
- [x] Step 5 — `POST /orchestrator` (SSE) + `GET /orchestrator/alerts` (JSON) registered in main.py
- [x] Step 6 — integration + SSE tests green (13/13 in file)
- [x] Step 7 — `submitUnifiedInput()` replaced (SSE reader); alerts card added (init + 5-min poll, per-alert dismiss Set)
- [x] Step 8 — smoke: alerts endpoint returns JSON array; SSE returns `text/event-stream` with reasoning→final
- [x] Step 9 — full pytest: **257 passed**, no regressions

## End-of-session audit

| Item | Location | Tests | Notes |
|------|----------|-------|-------|
| System prompt (gate rule verbatim) | `context/core/orchestrator_prompt.md` | — | Loaded via `_load_orchestrator_prompt()` |
| Rules (data-driven, `enabled`) | `context/core/off_track_rules.json` | disabled-rule test | Adding a rule reusing an existing `check` needs no code |
| 6 tools + dispatch + schemas | `backend/orchestrator_tools.py` | 6 tool-level | `main` imported lazily (no circular import) |
| Stream loop + model env | `backend/openai_client.py` (appended) | SSE + integration | `ORCHESTRATOR_MODEL` default `gpt-5.4`, never hardcoded |
| SSE bridge + alerts runner | `backend/orchestrator.py` | integration | Persists ChatMessage like /input |
| Routes | `backend/main.py` (+22 lines, 2 imports) | alerts endpoint | `/parse`,`/input`,`/chat`,`apply_parse_updates`,`_compute_gate` untouched |
| Frontend SSE + alerts card | `frontend/index.html` | manual | ReadableStream reader; dismiss via frontend-only Set |

**Not touched (verified):** `/parse`, `/input`, `/chat`, `apply_parse_updates()`, `_compute_gate()`, HTTP 423 path, all pre-existing test files.
**Skipped/deviated:** Live OpenAI smoke (brain-dump/question against the real model) not run — would require a real `gpt-5.4` call, which the spec forbids in tests and which needs a live key; substituted a mocked-stream SSE round-trip. The on-disk `founder_os.db` has pre-existing schema drift (missing `annual_targets.display_value`) unrelated to this work — tests use a fresh in-memory schema and pass.
