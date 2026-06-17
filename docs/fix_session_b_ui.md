# Fix — Session B: Visual Overhaul (Zentra Aesthetic)

**Scope:** CSS + HTML structure only. Zero backend changes. Zero JS logic changes. Zero new features.
**File touched:** `frontend/index.html` (single self-contained file).
**Date:** 2026-06-17

---

## Goal

Re-skin Founder OS from the navy "Luna palette" to a near-black "Zentra" aesthetic:
near-black background, generous card radius, brutal typography hierarchy, iOS-feel
transitions. The cyan accent system (`--glow: #A7EBF2`) is preserved.

The UI/UX Pro Max plugin skill was read in full before any code was written. Relevant
constraints applied from it are listed in the **Plugin compliance** section below.

---

## Changes (execution order)

1. **`:root` colour system** — replaced entire block with the Zentra token set
   (`--bg: #0A0B0D`, layered surfaces, easing curves, radius scale). Added
   `--nav-w-open`, `--radius-*`, and `--ease-*` tokens. `--purple` retained (still
   referenced by todo category dots / academic tags — not in the new spec but in active use).

2. **Typography pass**
   - `body` gets `font-feature-settings:"cv02","cv03","cv04","cv11"` for sharper Inter.
   - Metric numbers (`.cd-val`, `.radar-composite-val`, `.sprint-val`, `.td-gate-status`)
     keep Syne 800 with `letter-spacing:-0.02em`.
   - Card titles (`.card-title`, `.radar-title`) → `10px / 0.18em / uppercase / muted / 500`.
   - **All 66 `font-size:8px` → `font-size:10px`** (10px floor enforced globally).

3. **Cards** — `.card` rebuilt: `--surface` bg, `--radius-md`, `18px 20px` padding,
   `border-color` hover transition. A `.card--metric` modifier adds the
   `linear-gradient(135deg,#111318,#0D1520)` for primary-metric cards (radar, countdown
   wrapper, 10K scoreboard). Applied via class on those cards.

4. **Sidebar** — icon-only collapsed (`--nav-w: 64px`), CSS-only hover expand to
   `220px`. Each nav-item label wrapped in `<span class="nav-label">`. Collapse +
   hover-expand logic is scoped to `@media(min-width:769px)` so the **mobile drawer
   keeps showing labels and its existing behaviour is untouched**. Group labels and the
   War Room phase box also fade out when collapsed (CSS opacity only, no new elements).
   Active item → `3px solid var(--glow)` left border + `rgba(167,235,242,0.06)` bg.

5. **Badges** — pill shape (`border-radius:100px`), `10px / 600 / 0.06em`, all five
   variants (`b-glow/warn/suc/muted/danger`) recoloured to the rgba token system.
   `.b-urg` kept (pulsing urgent variant in active use) on the new pill base.

6. **Transitions** — all `transition:all 0.15s` → `transition:all 0.25s var(--ease-ios)`.
   Toggle elements (`.nn-dot`, `.cb`, `.rt-cb`) use the spring/smooth easing combo with a
   `scale(1.15)` pop on done. Entrance: `@keyframes slideUp` replaces `fadeUp`;
   `.fade-up` class now plays `slideUp 0.35s var(--ease-ios)`.

7. **Header** — frosted glass: `rgba(10,11,13,0.8)` + `backdrop-filter:blur(20px)
   saturate(180%)` (with `-webkit-` prefix). Content scrolls behind it.

8. **Progress bars** — `.pbar` 5px / pill / `rgba(167,235,242,0.08)` track; `.pfill`
   pill + `0.6s var(--ease-ios)` width transition; gradient fills `.pg/.pm`, solid `.pl/.pd`.

9. **Non-negotiable rows** — Zentra pill rows: `--surface-hi` bg, `--radius-sm`,
   transparent border → border on hover, green tint when `.nn-done`. `renderNonNeg()`
   markup adds the `nn-done` class on the row when the habit is done (only HTML touched
   in JS region — no logic change; class string already toggled per-habit there).

10. **Buttons / inputs** — buttons (`.btn-parse/.btn-send/.at-add-btn/.ar-btn`) get
    `--radius-sm`, spring hover lift + active press-scale. Inputs/textarea/select get
    `--radius-sm`, `--surface-lo` bg, glow focus ring (`0 0 0 3px rgba(167,235,242,0.1)`).

11. Hardcoded `#011C40` (old navy, used as dark-text-on-bright-button colour, 9×)
    → `#0A0B0D` to match the new near-black ground.

---

## Plugin compliance (UI/UX Pro Max)

- **§1 Accessibility / contrast** — `--text:#EAEEF2` on `--bg:#0A0B0D` ≈ 16:1; `--muted`
  at 0.45 alpha reserved for non-essential labels only. Focus rings kept (glow ring on inputs).
- **§4 Style-match / dark-mode-pairing** — single dark theme, consistent elevation scale
  via `--surface / --surface-hi / --surface-lo`; no pure `#000000`.
- **§6 Typography & colour** — semantic tokens, 10px minimum (raised from 8px), weight
  hierarchy (Syne 800 metrics, Inter 500 labels, 400 body), tabular feel kept for data.
- **§7 Animation** — durations 150–350ms, `transform/opacity` only, spring/iOS easing,
  exit not longer than enter, motion conveys state (toggle pop, slideUp entrance).
- **Plugin overrides / notes:** body-text was **not** force-set to 12px globally — the
  dashboard is intentionally data-dense; forcing 12px everywhere would break the grid.
  Component-level sizes were raised to the 10px floor instead. Noted per the skill's
  `visual-hierarchy` + `whitespace-balance` guidance.

---

## Verification (results)

- **`pytest` — 261 passed** (11.5s). Only pre-existing `datetime.utcnow()` deprecation
  warnings; no backend file was touched.
- **Computed-style inspection** (live preview, backend serving):
  - `--bg` → `#0A0B0D`; body bg `rgb(10,11,13)`; `font-feature-settings` applied.
  - `.badge` → `border-radius:100px`, `font-size:10px`.
  - `.card` → `border-radius:14px`, `padding:18px 20px`; `--radius-md` = 14px.
  - `#header` → `backdrop-filter:blur(20px) saturate(1.8)`, bg `rgba(10,11,13,0.8)`.
  - Desktop: `#nav` width `64px`, nav-label opacity `0`; active item left border
    `rgb(167,235,242)` ≈ `2.67px` (3px). `.nn-row` bg `rgb(25,28,35)` (`--surface-hi`).
  - **Min font-size across all elements = 10px** (incl. radar SVG axis labels, bumped
    from `font-size="8"` → `"10"` in the chart builder).
- **Mobile (375px):** `#nav` width `280px`, nav-label opacity `1` (labels visible),
  hamburger `display:block` — drawer behaviour unchanged, collapse correctly scoped to
  `@media(min-width:769px)`.

## Extra notes

- The hard 10px floor (spec: "minimum is 10px") was applied beyond the explicit
  `font-size:8px` rule — all `9px` and `7px` declarations (CSS + inline style strings +
  the one radar SVG label) were raised to `10px`. No JS *logic* was touched; only style
  string values.
- `--border` became a faint `rgba(...,0.12)`. Where it had doubled as a *text* colour
  (`.sec-head`, `.dump-label-top`, `.lt`, `.mc-label`, `.ar-topic-name`) it was repointed
  to `--glow-dim` to stay legible. Interactive checkbox borders (`.cb`, `.rt-cb`) use
  `--border-hi` so they remain visible against the new faint border default.
- All `border-radius:6px` card containers were bumped to `--radius-md` for the
  "generous radius" goal; button/chip `3px` radii on `.at-add-btn`/`.ar-btn` → `--radius-sm`.
- Added a `@media(prefers-reduced-motion:reduce)` block (plugin §7 `reduced-motion`).
- `.claude/launch.json` was added to run the backend for live preview review (dev tooling,
  not app code).
