# Session 8 Fix Spec

## FIX 1 — Dashboard Execution Stack Filter

**Problem**: Completed todos from previous days remain visible in "Today's Execution Stack."

**Changes:**
- `backend/models.py` — Add `completed_at` (Date, nullable) to `Todo`
- `backend/migrations/m006_todo_completed_at.py` — idempotent migration: adds column, backfills `completed_at = due` for existing done rows
- `backend/main.py` — `toggle_todo`: set `completed_at = date.today()` on done, `null` on un-done; include `completed_at` in serialised response
- `frontend/index.html` — `renderTodos()`: filter sorted list before render: keep all `!t.done`; for `t.done`, only keep if `t.completed_at === todayStr()`

## FIX 2 — add_todos Orchestrator Tool

**New tool**: `add_todos(items: list[str], category: str = "personal", priority: int = 5)`

- Creates one `Todo` row per item (`due=today`, `source="orchestrator"`, `done=false`)
- Returns `{created: N, todos: [{id, text}, ...]}`
- Only callable when user explicitly instructs (add/log/stack/save language)

**Changes:**
- `backend/orchestrator_tools.py` — implement `add_todos`, add to `_TOOL_DISPATCH`, add schema to `TOOL_SCHEMAS`
- `context/core/orchestrator_prompt.md` — append strict usage rules
- `backend/tests/test_orchestrator.py` — 4 new tests

## New tests
1. `test_add_todos_creates_rows` — 3 items → 3 DB rows
2. `test_add_todos_respects_category_and_priority` — category/priority propagated
3. `test_orchestrator_refuses_add_todos_on_query` — "what should I focus on" → `add_todos` NOT called
4. `test_orchestrator_calls_add_todos_on_explicit_instruction` — "add these to my stack: X, Y, Z" → `add_todos` called with exactly those items
