# Session 3 — Polish Fixes

## BUG 3 — Dashboard Responsive at Narrow Widths

**Root cause:** `.dash-grid` uses `grid-template-columns: 1fr 320px` with no breakpoint between 640px and full width. At ~640–960px the right column squeezes the left column and children overflow.

**Fix:**
1. Add `@media (max-width: 960px)` → `.dash-grid` becomes single-column (`1fr`); right column stacks below.
2. Add `@media (max-width: 640px)` rule for `.countdown-strip` → `repeat(2, 1fr)` (2×2 grid instead of 4×1).
3. Confirm no fixed widths on direct dash children cause overflow.

**Files touched:** `frontend/index.html` (CSS only, lines ~160–174 + media query block ~711)

---

## BUG 5 — Reading Queue Row Layout

**Root cause:** NOW button and status dropdown were stacked vertically in a `flex-direction:column` with only `gap:5px`, causing them to appear cramped/overlapping visually.

**Fix (Option A — horizontal row):**
Restructure right-side column of each book row:
- Row 1: `[☆ Now]` `[status dropdown]` side by side (`flex-direction:row`)
- Row 2: `[✕ delete]` alone, right-aligned

**Files touched:** `frontend/index.html` (`renderBooks()` JS only)

---

## Audit Checklist
- [ ] 1200px — two-column layout intact
- [ ] 1000px — single-column, right panels below left
- [ ] 800px — single-column, no overflow
- [ ] 600px — countdown 2×2
- [ ] 400px — countdown 2×2, all panels full-width
- [ ] Book rows: NOW + dropdown on same line, delete below
- [ ] No backend behaviour changed
- [ ] pytest 228 tests pass
