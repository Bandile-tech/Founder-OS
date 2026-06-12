SESSION 2 — TWO PHASE 5 IMPLEMENTATION GAPS

Save this spec to docs/fix_session_2.md before implementing. Re-read 
it after each fix to prevent spec drift.

BUG 4 — READING QUEUE REORDER (smaller, do first)

ROOT CAUSE (confirmed in previous diagnosis):
- renderBooks() in index.html has no reorder UI (no up/down arrows, 
  no drag handles) — never built
- POST /books/reorder endpoint exists in backend and works
- Book.position column exists in model
- addBook() doesn't set position on creation, so existing rows likely 
  all have position=0 or NULL

FIX:

1. Backfill position values for existing Book rows:
   - Backend: add a one-time migration or startup task that sets 
     position = id for any Book with position IS NULL or position = 0 
     when other rows have non-zero positions
   - Safer: run a single UPDATE setting position = ROW_NUMBER() 
     ordered by created_at for any Book where position is null/zero
   - Make this idempotent — running it twice should be safe

2. Update addBook() in main.py:
   - On create, set position = (max existing position + 1)
   - New books always go to the end of the queue

3. Update renderBooks() in index.html:
   - Sort books by position field before rendering
   - Add up/down arrow buttons OR drag handles to each Book row
   - Place the arrows on the LEFT side of the row, before the title
   - Up arrow disabled on first row; down arrow disabled on last row
   - On click, call POST /books/reorder with the new ordering

4. Reorder API contract:
   - POST /books/reorder accepts: [{id: int, position: int}, ...]
   - Backend updates each book's position in a transaction
   - Returns updated book list

5. Frontend reorder handler:
   - Compute new order based on arrow click direction
   - POST the full updated ordering
   - On success: update S.books locally with new positions and 
     re-render
   - On failure: don't update local state, show error

6. Regression test:
   - Seed 3 books with positions 0, 1, 2
   - Move book 1 up — assert positions become 1, 0, 2
   - Move book 0 down — assert positions become 1, 0, 2

BUG 6 — BIBLE PLAN PAGE REBUILD (larger, do second)

ROOT CAUSE (confirmed in previous diagnosis):
- renderBible() still reads from S.bible (old BibleEntry array)
- New ReadingPlan / ReadingPlanEntry models, endpoints, schemas all 
  exist in backend
- Frontend was never migrated to use the new structure

FIX:

1. Backend endpoints — verify or add if missing:
   - GET    /reading-plans          — list all active plans
   - POST   /reading-plans          — create new plan
   - PATCH  /reading-plans/{id}     — update plan (progress, name, 
                                       dates, status)
   - DELETE /reading-plans/{id}     — soft archive (status=archived)
   - POST   /reading-plans/{id}/mark-today
                                    — increment current_chapter by 
                                      daily_target_chapters, OR mark 
                                      today's reading complete

2. Frontend: replace renderBible() with renderReadingPlan()
   - Sidebar item label remains "Bible Plan"
   - On click, route to new render function
   - Page layout:
     * Top: list of ACTIVE reading plans as cards
     * Each card shows: plan name, current book + chapter, 
       daily target (chapters or verses), days remaining to 
       target_completion_date, status
     * Each card has: "Mark today complete" button, "Edit" button, 
       "Archive" button
     * Below: "+ Add Plan" form
       Fields: plan_name (string), current_book (string), 
       current_chapter (int default 1), daily_target_chapters (int), 
       start_date (date, default today), 
       target_completion_date (date, optional), notes (text)
     * Below add form: collapsible "Archived Plans" section

3. State management:
   - Add S.readingPlans = [] to DEFAULT_STATE
   - syncFromBackend() fetches /reading-plans and assigns to 
     S.readingPlans
   - REMOVE the fetch to /bible from syncFromBackend (the BibleEntry 
     endpoints stay live but the frontend stops calling them)
   - Keep S.bible in DEFAULT_STATE as empty array for backward compat 
     during transition — can be removed later

4. Brain dump integration (defer to a later session):
   - Note in code: "TODO: integrate bible_progress with /reading-plans 
     in next session"
   - Do not add bible_progress to parse schema in this session — 
     keep the scope tight

5. Migration of old data:
   - Existing BibleEntry rows stay in the database, untouched
   - User creates new ReadingPlan(s) from scratch via the new UI
   - If user has old Bible entries they want to preserve, they can 
     manually re-enter as a ReadingPlan
   - Note this in a comment for the user

6. Test:
   - Create a plan via the UI
   - Mark today complete — assert current_chapter increments
   - Verify it persists across syncFromBackend
   - Backend test: PATCH /reading-plans/{id}/mark-today increments 
     correctly

EXECUTION RULES:
- Do Bug 4 first (smaller, faster), commit checkpoint, then Bug 6
- After Bug 4 implementation, run pytest backend/tests/ -v before 
  starting Bug 6
- Do NOT touch Bug 3 or Bug 5 — those are Session 3
- Do NOT add brain dump integration for reading plans this session
- Do NOT touch any other module

GIT:
After each bug, show git diff stat. Do not commit. I will review and 
commit manually.

End-of-session audit (REQUIRED):
Re-read docs/fix_session_2.md and confirm for each bug:
- Implementation locations (file/line references)
- Tests added and passing
- Spec items skipped or modified

Report the audit before showing the final combined diff stat.
