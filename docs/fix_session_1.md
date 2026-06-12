SESSION 1 — TWO TRUST-CRITICAL FIXES

Save this spec to docs/fix_session_1.md before implementing. Re-read 
it after each fix to prevent spec drift.

BUG 1 — BRAIN DUMP MISROUTING (highest priority)

ROOT CAUSE (confirmed in previous diagnosis):
reading_updates does not exist in the parse schema or apply_parse_updates(). 
"read 12 week year" gets misrouted to todos because there's no books 
extraction category. Feature exists in the UI (toggleCurrentlyReading 
button) but is unreachable from brain dump.

FIX:

1. Add reading_updates key to parse schema in openai_client.py:
   reading_updates: [{"title_fragment": "string"}]

2. Add explicit example in the GPT extraction prompt:
   Input: "read 12 week year"
   Expected output should populate reading_updates: [{"title_fragment": "12 week year"}]
   NOT todos_add.

3. Add reading_updates branch in apply_parse_updates() in main.py:
   - For each title_fragment, query Book table with case-insensitive 
     partial match (use ILIKE on Postgres, LIKE LOWER on SQLite, or 
     SQLAlchemy's func.lower)
   - On match: set the matched Book's is_currently_reading=True. 
     Set all other Books' is_currently_reading=False (only one current 
     at a time)
   - On no match: append to a new updates field reading_match_failures 
     with the unmatched fragment

4. Update the response payload from /input and /parse to include:
   - reading_updates_applied: list of Book titles successfully marked
   - reading_match_failures: list of fragments that didn't match

5. Frontend (index.html, submitUnifiedInput handler):
   - When reading_match_failures is non-empty, surface a warning in 
     the result area: "Couldn't find a book matching: [fragments]"
   - When reading_updates_applied is non-empty, refresh the books 
     panel so the NOW badge updates

6. Regression test (backend/tests/test_warroom.py):
   - Seed a Book with title "The 12 Week Year"
   - Submit /input with text "read 12 week year"
   - Assert: Book.is_currently_reading = True
   - Assert: NO Todo row was created
   - Assert: response contains reading_updates_applied with the book title

BUG 2 — ANNUAL TARGETS PROGRESS DISPLAY (second priority)

ROOT CAUSE (confirmed):
Frontend has two issues:
a) addAnnualTarget() hardcodes progress_pct:0 and status:"on_track" 
   when pushing new target to local state
b) The fallback uses ?? which doesn't catch 0 as a fallthrough trigger

FIX:

1. In index.html addAnnualTarget():
   - Remove the hardcoded progress_pct:0 and status:"on_track"
   - Either compute progress_pct locally from current/target before 
     pushing, OR call syncFromBackend() immediately after add to fetch 
     authoritative values from /annual-targets

2. In index.html, lines 1893 and 1920:
   - Change `at.progress_pct ?? Math.min(100,(at.current/at.target*100))` 
     to `(at.progress_pct ?? null) !== null ? at.progress_pct : Math.min(100, at.current/at.target*100)`
   - OR cleaner: always compute locally if at.target is set and 
     at.current is set, regardless of progress_pct value

3. atStatusBadge() function:
   - If at.status is missing, compute it locally:
     pct = current/target * 100
     year_pct = _year_pct equivalent on frontend (or call backend)
     gap = pct - year_pct
     status = "ahead" if gap > 10, "on_track" if gap > -10, "behind" otherwise
   - Don't default to "on_track" blindly

4. Verify progress bar fills correctly:
   - The bar's CSS width should be `${pct}%` based on the computed pct
   - Check the actual CSS rule for the bar element

5. Test on live deploy:
   - Add target: current=2000, target=5000
   - Bar should show 40% fill
   - Status should compute correctly based on year_pct (currently June, 
     year_pct ≈ 45%, so 40% < 45% means "behind" by 5%)

EXECUTION RULES:
- After each fix, run pytest backend/tests/ -v to verify no regressions
- Show git diff stat after each fix individually, not bundled
- Do NOT touch Bugs 3, 4, 5, 6 — those are separate sessions
- Do NOT add features beyond what's specified
- If you discover a related issue, note it but do not fix it

GIT:
Do not commit. I will review and commit manually after both fixes.

End-of-session audit (REQUIRED before final diff):
Re-read docs/fix_session_1.md and confirm for each fix:
- Was it implemented? (file/line reference)
- Was the regression test added and passing?
- Any spec items skipped or modified?

Report the audit before showing the final diff stat.
