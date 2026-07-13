# UI Rebuild — Iteration Log

Skin-only rebuild of `frontend/index.html`: dark glassmorphism over an ambient light field, Bricolage Grotesque / IBM Plex Sans / IBM Plex Mono, motion on `--ease-ios` / `--ease-spring` / a new `--ease-out` quart. Every fetch call, endpoint, state shape and business rule preserved byte-identical. This log records what each iteration pass found and changed.

---

## Pass 1 — Design critique

Method: full-page headless-Chrome captures of all 12 pages at 1440×900 with live backend data, plus interactive inspection at narrow widths. Each finding below was fixed in the same pass.

### Fixed

1. **Radar composite value collided with the chart.** The 34px centre numeral sat directly on the grid rings and data polygon — at low scores the polygon spiked through the digits. Added a dark radial "hub" disc behind the centre (`.radar-composite::before`, radial-gradient to transparent) and reduced the numeral to 29px. The number now reads as the chart's hub instead of a collision. Also enlarged the chart 210 → 232px — it is the soul metric of the right column and was underscaled.

2. **Bible-log delete buttons were invisible (pre-existing bug).** `.td-del` was styled `opacity: 0` with reveal on `tr:hover` — but the Bible page renders those buttons inside plain `div` rows, not table rows, so they could never appear (true in the old skin too). Scoped the hover-hide to `tr .td-del` only; elsewhere `.td-del` now rests at 0.5 opacity. Touch devices always see table delete buttons at 0.45 (`@media (hover: none)`) — hover-only affordances don't exist on coarse pointers.

3. **Emoji used as icons — replaced with the SVG set.** Nav had 💪 / 💹 glyph icons; academic subtopic rows used 📝/📄 for notes. All replaced with hand-drawn 16px stroke icons (1.5px, round caps — one family, one weight). The notes button now signals "has notes" by colour (`.has-notes` → accent cyan) instead of by swapping pictograms. `saveNotes()` toggles the class instead of rewriting emoji.

4. **Annual Targets form placeholders truncated.** "Current value (leave blank for descriptive)" clipped mid-word in its 120px grid column. Shortened both placeholders; the explanatory tip line under the form already carries the detail.

5. **Books row controls were ragged.** Status select and NOW toggle sat in a row with the delete button orphaned beneath. Merged all three into one aligned control row under the status label, and bumped all three to comfortable hit sizes.

6. **Alert rows used the banned side-stripe.** Off-track alerts rendered with `border-left: 2px solid <severity>`. Rebuilt as fully-bordered tinted chips (`border: 1px solid <sev>44; background: <sev>0f`) with a mono severity label. Also fixed `_alertSeverityColor` returning `var(--muted)` for info severity, which broke the derived alpha colours — now returns a hex like the others.

7. **Nav icons were 3px off the rail's optical centre.** Collapsed rail is 64px; icon centre landed at 29px. Adjusted item margin/padding (12+12+8) so icon centre = 32px = rail centre.

8. **Timetable NOW indicator** redesigned from a `border-left` stripe + "▶ NOW" to a lit row: gradient wash, inset 1px cyan ring, pulsing dot, mono NOW tag. Same information, no stripe, calmer.

9. **Trading gate status** dropped the "⚠ /✓" prefix glyphs — "GATE LOCKED / GATE CLEARED" at display size in the state colour carries it. The tinted glass card + top glow line does the shouting.

10. **Readability floor raised in secondary panels.** Conversation history and recent-activity rows were 10px; now 11.5px body with 9.5px mono metadata, and hairlines moved to the standard `rgba(167,235,242,0.05)` token.

11. **Undefined CSS variables (pre-existing bugs) now resolve.** The old sheet referenced `var(--card)`, `var(--accent)`, `var(--accent2)` without defining them (transparent full-log modal background, invisible em-styling in markdown). All three are defined in the new token set, and the full-log modal was moved onto the shared glass-modal classes.

12. **Danger actions unified.** ~10 inline `background: var(--danger)` button styles replaced with a `.btn-danger` variant of the button system (tinted glass, red border, correct hover/press) — destructive actions now share one visual voice, visibly separated from primary actions.

### Judged fine, left alone

- Settings slider "strikethrough" seen in a low-res capture — verified at 2× zoom to be JPEG artefacting; the control renders correctly.
- Nav group spacing at collapsed width (hidden group labels keep their height) — reads as intentional rhythm between groups.
- Sched-row edge-bleed (−12px margins) — deliberate: the lit NOW row runs wider than the text column, which is what makes it read as a highlight rather than a fill.

### Known constraint noted

- The `dark-glow` detector flags cyan glows on dark. The brief explicitly specifies "restrained neon-cyan accent against near-black" with "soft glowing accent lines" — glow is kept, but budgeted: text-shadow only on the logo, radar hub and hero numerals; box-glow only on live signal (online dot, active toggles, progress heads).

---

## Pass 2 — Interaction & motion audit

Method: live click-through in Chrome against the deployed backend — todo toggle round-trip (optimistic + POST, then reverted), health mobility toggle round-trip (PATCH + re-render, reverted), notes modal open/dismiss, trading form validation, drawer open/close, toast lifecycle, and a full orchestrator SSE round trip. Plus a control-by-control audit of hover/press/disabled/loading coverage in the stylesheet.

### Fixed

1. **The mobile drawer rendered blurry and dim — stacking-context bug I introduced.** `#app { z-index: 1 }` (added for ambient layering) trapped the fixed drawer inside `#app`'s stacking context, so the full-screen backdrop (z 199, a body-level sibling) painted *over* the drawer and frosted it. Fix: ambient moved to `z-index: -1` (negative-z child still paints above the body background), `#app` keeps `position: relative` for the rail but no z-index. Drawer now crisp over a blurred page — the physics the design intends.

2. **The 30-second background sync replayed the radar draw choreography.** Watched it happen live: sync → `renderPage` → `fetchAndRenderRadar` → re-inject SVG → rings/polygon/dots re-animate. Violates the "data updates must not look like page loads" rule. Fix: the injected SVG gets a `no-anim` class whenever the fetched scores are identical to the previous render (`JSON.stringify` key comparison); CSS zeroes the choreography under `.no-anim`. Changed scores still draw — that replay is meaningful.

3. **Command send button had no loading state.** During an orchestrator call only the badge changed. The send button now takes `.btn-loading` (content fades out, 12px spinner) for the duration of the SSE stream, removed in `finally`. Verified live: spinner during stream, arrow restored, focus returned to the input.

4. **Escape didn't close the notes modal** (the other two modals already supported it). Added the same Escape handler to the notes textarea. The full-log modal remains click-outside + ✕ only — it contains no focusable inputs to hang a key handler on; logged as a known minor gap.

5. **Glyph-as-icon cleanup in dynamic strings:** the ⚡ prefixes (header drift indicator, drift callouts, AI-source todo marker, command echo) and 📥 in logged-confirmation replaced with system elements — a pulsing amber dot for drift, a mono `AI` pill for source, a `›` prompt glyph for command echoes.

### Verified conforming (no change)

- Every pressable scales 0.97 (buttons) / 0.96–0.98 (rows, chips, toggles) at 120ms ease-out; hovers brighten borders at 190ms; all hover rules gated behind `(hover:hover) and (pointer:fine)`.
- Disabled states: gate-locked live-trade submit at 0.4 opacity with tooltip; parse/send buttons while busy.
- Field validation: R-multiple/account/risk/P&L errors appear under their fields with a red ring (verified by submitting empty).
- Toasts: 300ms rise-in, 180ms fade-down exit (exit faster than enter), auto-dismiss 2.5s/4.5s.
- Modals animate in at 260ms ease-out from scale 0.965; instant close is deliberate (frequently-repeated dismissal — snappier than a fade).
- Optimistic writes verified round-trip: todo toggle and health toggle persisted to the backend and restored.
- Accordion expansions (subjects, history panels) stay instant by design — high-frequency actions don't animate.

---

## Pass 3 — Polish opportunities (added 3)

1. **The radar sweep — the signature moment.** When fresh scores draw in, a single conic-gradient sweep arm rotates once around the chart (1.5s, ring-masked so it only lives in the grid band, fading in and out). It plays on first reveal and on genuine score changes; the `no-anim` guard keeps background syncs silent, and `prefers-reduced-motion` removes it entirely. One rotation, never looping — a radar that actually sweeps, once, when it has something new to say.

2. **Academic loading skeleton.** "Loading subjects…" text replaced with three ghost subject-cards (ring circle + two text bars, shimmer, staggered opacity 1 / 0.6 / 0.35 to suggest depth). The wait on a cold backend is multi-second — now it holds the layout it's about to fill.

3. **Live-trades empty state.** The gate-locked table's "No live trades yet." row became a centred empty state: padlock glyph in dim cyan + "No live trades yet — clear the backtest gate to unlock this log." It explains *why* the log is empty and what unlocks it, in the interface's voice.

---

## Post-pass detector triage

The impeccable anti-pattern detector was re-run over the final file. Remaining findings, each judged intentional or false-positive (none suppressed via config — that requires owner sign-off):

| Finding | Verdict |
|---|---|
| `gradient-text` (guide page) | False positive — the /guide *documents the ban* ("never `background-clip: text`") in prose; no gradient text exists in the UI. |
| `bounce-easing` ×3 (`--ease-spring`) | Intentional — the brief mandates the codebase's established spring curve (`cubic-bezier(0.34, 1.56, 0.64, 1)`). Budgeted to sub-200ms micro-pops only: checkmarks, status dots, radar dots. Never used on panels, pages, or anything large. |
| `layout-transition` (nav rail width) | Intentional — the rail was rebuilt as an overlay precisely so its width animation reflows only its own handful of children, never page content. Reasoning documented in the /guide motion section. |
| `layout-transition` (radar legend) | Fixed — was dead code (an unused style string left over from the old skin); removed. |
| `em-dash-overuse` (guide page) | Accepted — editorial punctuation in the written documentation page, not UI copy. |
| `dark-glow` (from earlier session scan) | Intentional per brief ("restrained neon-cyan accent", "soft glowing accent lines"). Glow budget: text-shadow on logo/hero/radar hub only; box-glow on live signal only. |
