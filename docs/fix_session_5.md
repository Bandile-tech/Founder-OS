# Fix Session 5 — Founder OS

Date: 2026-06-12

---

## FIX 1 — Non-Negotiables War Room → Dashboard Sync

**Problem:** Non-negotiables added via the War Room page don't appear on the dashboard's
Non-Negotiables panel.

**Root cause diagnosed:**
- `renderNonNeg()` (line ~1892) reads from `S.habits`, not from `/non-negotiables`
- `syncFromBackend()` does not fetch `/non-negotiables` — no `S.nonNeg` exists
- `_wr.nonNeg` is local state inside `renderWarRoom()`, isolated from `S`
- War Room mutations call `renderWarRoom()` only, never `syncFromBackend()`

**Fix:**
1. Add `{key:"nonNeg", url:"/non-negotiables"}` to `syncFromBackend` ENDPOINTS
2. Assign `if(data.nonNeg) S.nonNeg = data.nonNeg;` in the data processing block
3. Update `renderNonNeg()` to render from `S.nonNeg` (when populated), matching done state
   from `S.habits` by key; fall back to pure `S.habits` when `S.nonNeg` is empty
4. After `addNonNegotiable`, `toggleNN`, `deleteNN` — call `syncFromBackend()` (non-blocking)
   so dashboard re-renders with fresh state

**Status:** [ ] Implemented [ ] Tested

---

## FIX 2 — Header Disappears on Mobile

**Problem:** On some pages on mobile, the header gets stuck or disappears until page refresh.

**Root cause diagnosed:**
- `#header` has `z-index:100` but `position` is not set (defaults to `static`), so `z-index`
  is ignored — any `position:fixed` overlay can paint over the header area
- On mobile `@media(max-width:768px)`, the sidebar `#nav` is `position:fixed; z-index:200`
  and `nav-backdrop` is `position:fixed; z-index:199`; if `closeSidebar()` fails to clear
  the backdrop the header is obscured
- No defensive guard ensures the header is visible after page navigation

**Fix:**
1. Add `position:relative; z-index:300` to `#header` — creates a stacking context that paints
   above page-level fixed elements
2. Add a guard in `nav()` and at the end of `closeSidebar()` that forces the header visible:
   `const h=document.getElementById("header"); if(h&&h.style.display==="none") h.style.display=""`

**Status:** [ ] Implemented [ ] Tested

---

## FIX 3 — Backend Keep-Alive Ping

**Problem:** Render free tier spins down after 15 min of inactivity; first request takes 30-60 s.

**Fix:**
- Backend `main.py`: add `GET /ping` → `{"status":"ok","ts": datetime.utcnow().isoformat()}`
  (no DB, no auth)
- Frontend `index.html`:
  - `keepAlive()` function: call `GET /ping`; on success after gap trigger `syncFromBackend()`; 
    on failure do nothing (silent)
  - Call `keepAlive()` once on page load
  - `setInterval(keepAlive, 10 * 60 * 1000)`

**Status:** [ ] Implemented [ ] Tested

---

## FIX 4 — Font Change (IBM Plex Mono → Inter for body text)

**Problem:** IBM Plex Mono is monospace — hard to read on mobile and feels heavy for body copy.

**Fix:**
- Replace Google Fonts import: swap `IBM+Plex+Mono:ital,wght@0,400;0,500;0,700;1,400` with
  `Inter:wght@400;500;600`; keep `Syne:wght@700;800`
- Update `body { font-family }` from `'IBM Plex Mono',monospace` to `'Inter',sans-serif`
- Replace all inline `font-family:'IBM Plex Mono',monospace` occurrences with
  `font-family:'Inter',sans-serif`
- Replace SVG `font-family="IBM Plex Mono,monospace"` with `font-family="Inter,sans-serif"`
- Keep Syne on: `.h-logo`, countdown numbers, KPI numbers, section headers (already explicit)

**Syne stays on:** `.h-logo` (`.h-logo{font-family:'Syne',sans-serif}`), any element that
already has `font-family:Syne` set explicitly.

**Status:** [ ] Implemented [ ] Tested

---

## Session Audit Checklist

- [x] Fix 1 implemented — `/non-negotiables` added to syncFromBackend; `S.nonNeg` is source of
      truth for `renderNonNeg()`; War Room mutations call `syncFromBackend()` after `renderWarRoom()`
- [x] Fix 2 implemented — `#header` gets `position:relative; z-index:300`; `closeSidebar()`
      defensive guard forces header visible if hidden
- [x] Fix 3 implemented — `GET /ping` added to main.py; `keepAlive()` fires on load + every
      10 min; re-syncs if gap > 12 min
- [x] Fix 4 implemented — Inter replaces IBM Plex Mono everywhere; Syne unchanged on logo,
      countdown values, KPI numbers, section headers; SVG radar updated
- [x] `pytest` run — 229 passed (228 original + 1 new `/ping` test)
- [x] `git diff --stat` shown — no commit made
