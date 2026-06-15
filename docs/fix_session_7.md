# Fix Session 7

## Fix 1 — Derive habits from non-negotiables

**Root cause**: `DEFAULT_STATE.habits` hardcodes 5 keys. New non-negotiables added via War Room land in `S.nonNeg` but never get a `S.habits` entry, so `toggleHabit()` crashes silently and `renderHeader()` ignores them.

**Backend change** — `backend/main.py` `/habits GET` (line ~319):
- Old: seeds from `HABIT_DEFAULTS`, returns only those rows.
- New: fetches all active non-negotiables, builds return list — for each active NN, return its habit row if it exists for today, else `{done: false}`. Does NOT auto-create rows (only created on toggle).

**Frontend changes** — `frontend/index.html`:
- `DEFAULT_STATE.habits` → `habits: {}` (line ~1485)
- `syncFromBackend()` (line ~1610): after both `nonNeg` and `habits` arrive, rebuild `S.habits` from active non-negotiables with done state overlay.
- `renderNonNeg()` (line ~1941): simplify — iterate `S.habits` directly, drop merge logic with `S.nonNeg`.

## Fix 2 — Header drift counter

Resolved by Fix 1. `renderHeader()` uses `Object.values(S.habits)` which now reflects all active non-negotiables.

## Fix 3 — Books drag-and-drop reorder

**Frontend changes** — `frontend/index.html`:
- `renderBooks()` (line ~2568): on desktop (`window.matchMedia("(max-width: 640px)").matches === false`), render drag handle `⋮⋮` instead of arrow buttons. Each `.book-row` gets `draggable="true"` and `data-book-id`.
- New drag event handlers: `_bookDragStart`, `_bookDragOver`, `_bookDragLeave`, `_bookDrop`, `_bookDragEnd` (delegated from `books-card`).
- On drop: compute new order, optimistically update `S.books`, POST `/books/reorder`, revert on failure.
- Mobile (≤640px): keep existing arrow buttons unchanged.
- CSS additions: `.book-row.dragging { opacity:0.4 }`, `.book-row.drag-over-top / .drag-over-bot { border-top/bottom: 1px solid var(--glow) }`, drag handle styling.

## Files modified

- `backend/main.py` — `/habits GET` endpoint (~line 319)
- `frontend/index.html` — DEFAULT_STATE, syncFromBackend, renderNonNeg, toggleHabit guard, renderBooks, drag CSS, drag handlers
