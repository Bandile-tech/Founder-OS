# Fix: Dashboard Academic KPI Bridge

**Symptom:** Dashboard → Performance KPIs → Academics shows stale/wrong values
(e.g. Maths 9709 at 700%/100%) instead of live subtopic mastery data.

**Date diagnosed:** 2026-06-10

---

## Root Cause

Two compounding problems:

### Layer D — Stale value in `localStorage`

The old `toggleAcadTask()` function wrote calculated topic-completion
percentages directly into `S.kpis` and called `saveState()`, persisting
them to `localStorage`. Now that the old Academic Roadmap code is gone,
those numbers (e.g. `maths_syllabus.value = 700`) sit frozen in the
user's `localStorage` and are loaded on every page open via:

```js
// frontend/index.html — loadState()
const s = JSON.parse(localStorage.getItem("founderOSv2"));
return s ? deepMerge(DEFAULT_STATE, s) : JSON.parse(JSON.stringify(DEFAULT_STATE));
```

`deepMerge` deep-merges saved state over defaults, so the stale
localStorage value wins over `DEFAULT_STATE.kpis.maths_syllabus.value = 62`.

### Layer A — `syncFromBackend()` never fetches `/subjects/progress`

`syncFromBackend()` calls `GET /kpis` and maps the response into `S.kpis`:

```js
// frontend/index.html — syncFromBackend()
Object.entries(kpisRaw).forEach(([key, kpi]) => {
  if (S.kpis[key]) {
    S.kpis[key].value = kpi.value;   // ← reads backend _kpi_state, not subtopic mastery
    S.kpis[key].target = kpi.target;
  }
});
```

`GET /kpis` returns the backend's in-memory `_kpi_state`, which is seeded
from the old `KPISnapshot` table and updated only by the `/parse` endpoint.
It has **no connection** to the new `Subject → Topic → Subtopic` tables.

`syncFromBackend()` has no call to `GET /subjects/progress` at all.

### Why `_arRefreshProgress()` doesn't help

After a subtopic mastery PATCH, `_arRefreshProgress()` correctly fetches
`/subjects/progress` and updates the Academic Roadmap page's progress
rings, then calls:

```js
// frontend/index.html — _arRefreshProgress()
if (document.getElementById("p-dashboard").classList.contains("active")) {
  renderKPIs();          // ← called correctly
  fetchAndRenderRadar(); // ← called correctly
}
```

But `renderKPIs()` reads from `S.kpis`:

```js
// frontend/index.html — kpiRow()
const kpi = S.kpis[k];   // ← still the stale value
```

`S.kpis` was never updated with the new progress values, so
`renderKPIs()` re-renders the same stale number.

### Radar is already correct

`fetchAndRenderRadar()` calls `GET /radar` on the backend, which calls
`_compute_subject_progress()` — reading live from the Subject/Topic/Subtopic
tables. The radar does not go through `S.kpis` and is not affected.

---

## Files to Edit

Only one file needs changes: **`frontend/index.html`**

---

## Fix

### 1. Add the CODE_TO_KPI map (module-level constant)

Place this near the other academic module-level constants (around line 1888,
alongside `_arSubjects`, `_arProgress`, etc.):

```js
const CODE_TO_KPI = {
  "9709": "maths_syllabus",
  "9231": "further_maths",
  "9609": "business",
  "9708": "economics",
};
```

### 2. Update `_arRefreshProgress()` to write into `S.kpis`

After building `_arProgress`, map each subject's `weighted_pct` into
`S.kpis` before calling `renderKPIs()`:

```js
async function _arRefreshProgress() {
  try {
    const progress = await fetch(`${API_BASE}/subjects/progress`).then(r => r.json());
    _arProgress = {};
    progress.forEach(p => {
      _arProgress[p.code] = p.weighted_pct;
      // Bridge: keep S.kpis in sync so renderKPIs() shows live values
      const key = CODE_TO_KPI[p.code];
      if (key && S.kpis[key]) S.kpis[key].value = p.weighted_pct ?? 0;
    });
    // ... rest of function unchanged (update circles, badges, call renderKPIs, etc.)
  }
}
```

### 3. Update `syncFromBackend()` to also fetch `/subjects/progress`

Add `/subjects/progress` to the parallel fetch and apply the same mapping
so the dashboard is correct on initial page load, not just after mastery changes:

```js
// Before (line ~1191):
const [kpisRaw, habits, todos, annual, sprint, bible, books, socialRaw] = await Promise.all([
  fetch(`${API_BASE}/kpis`).then(r => r.json()),
  // ...
]);

// After:
const [kpisRaw, habits, todos, annual, sprint, bible, books, socialRaw, acadProgress] = await Promise.all([
  fetch(`${API_BASE}/kpis`).then(r => r.json()),
  fetch(`${API_BASE}/habits`).then(r => r.json()),
  fetch(`${API_BASE}/todos`).then(r => r.json()),
  fetch(`${API_BASE}/annual-targets`).then(r => r.json()),
  fetch(`${API_BASE}/roadmap/sprint`).then(r => r.json()),
  fetch(`${API_BASE}/bible`).then(r => r.json()),
  fetch(`${API_BASE}/books`).then(r => r.json()),
  fetch(`${API_BASE}/social-score`).then(r => r.json()),
  fetch(`${API_BASE}/subjects/progress`).then(r => r.json()),
]);

// Then after the existing kpisRaw loop, add:
acadProgress.forEach(p => {
  const key = CODE_TO_KPI[p.code];
  if (key && S.kpis[key]) S.kpis[key].value = p.weighted_pct ?? 0;
});
```

### 4. Clear stale `localStorage` values

The quickest safe fix is to reset the four academic KPI values to `0` in
`DEFAULT_STATE`. Since `deepMerge` gives localStorage values priority,
this alone doesn't fix the stale-localStorage problem — but step 3
(syncFromBackend) will overwrite them with live data on every page load,
making the stale localStorage value irrelevant after the first sync.

Alternatively, bump the localStorage key from `"founderOSv2"` to
`"founderOSv3"` in `loadState()` and `saveState()` to force a clean slate.
That is a more aggressive reset and will lose any locally-saved todos/habits
state that hasn't been synced to the backend.

**Recommendation:** do step 3 first (syncFromBackend fix). That will
override the stale value within a second of page load. Only bump the
localStorage key if the 700% value is still visible in that first second
and is causing user confusion.

---

## Summary

| Layer | Problem | Fix |
|-------|---------|-----|
| D | Stale `localStorage` value from old `toggleAcadTask()` | Overwritten by step 3 on every sync |
| A | `syncFromBackend()` never calls `/subjects/progress` | Step 3: add to parallel fetch |
| — | `_arRefreshProgress()` fetches live data but doesn't bridge to `S.kpis` | Step 2: add CODE_TO_KPI mapping |
| ✓ | Radar reads from `/radar` → `_compute_subject_progress()` | Already correct, no change needed |
