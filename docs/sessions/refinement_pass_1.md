# Refinement Pass 1 — Spec & Audit

Targeted refinement on top of the three-pass rebuild (`rebuild_iteration_log.md`). Frontend only; no backend, orchestrator, or routing changes.

---

## Change 0 — "Market research agent" verification (blocking check for Change 4)

**Finding: the market research agent does not exist in the backend.**

Evidence:
- `backend/orchestrator_tools.py` `TOOL_SCHEMAS` contains exactly 8 tools: `route_brain_dump`, `get_dashboard_state`, `detect_off_track`, `synthesize_weekly_review`, `surface_weakest_subtopic`, `query_war_room`, `add_todos`, `query_cold_archive`. None performs market or web research.
- No endpoint in `backend/main.py` matches `research` or `market`. The only orchestrator endpoints are `POST /orchestrator` (SSE stream) and `GET /orchestrator/alerts`.
- The only "market" hits in the repo are an economics syllabus task (`econ-market`) and a seeded todo ("Market Structures").
- Typing "research X" in the terminal today reaches the orchestrator model, which has no research tool to route to — it would answer from the system prompt or decline.

**Decision (per the brief's own instruction):** Change 4 (Agents nav tab) is **not built**. The brief says "do not build a UI for a feature that doesn't exist yet" and "no placeholder agents". Building the tab around the orchestrator's internal tools would misrepresent them as user-facing agents. When a real research agent tool lands in `orchestrator_tools.py`, the Agents page can be built around its actual config/history surface.

---

## Change 1 — Typography

IBM Plex Mono dropped. IBM Plex Sans dropped with it — the iteration log's stated reason for Plex Sans was superfamily agreement with Plex Mono ("body and data agree on skeleton"); with the mono gone that rationale is gone.

**Proposed stack (two fonts):**
- **Display: Bricolage Grotesque (retained).** The iteration log defends it explicitly as the anti-generic identity choice ("chosen over the saturated AI-default cluster"); this pass respects that reasoning — replacing it would contradict the log without cause.
- **Body + technical voice: Geist.** Engineered for data-dense product UIs, ships true tabular figures (so `font-variant-numeric: tabular-nums` keeps trade tables, timers and R-multiple columns aligned), and its cool precision matches the cold-glass surface where Plex Mono read as warm terminal.

Implementation notes:
- `--font-mono` token renamed `--font-tech` (it is no longer a monospace; the name would lie) and remapped to Geist. All ~60 usages updated. The technical voice survives through what always carried it: uppercase, 9.5–10.5px, 0.14–0.22em tracking, tabular numerals.
- New `--font-code` token (`ui-monospace` system stack) used only for literal code: `.md-body code`, `.guide-p code`, `.guide-li code`, `.guide-pre`. Loads nothing; code samples stay genuinely monospaced.
- Radar SVG axis labels (inline `font-family` string in `drawRadar`) updated to Geist.
- /guide Type System card rewritten to document the two-font system.

## Change 2 — Dashboard above-the-fold density

The hero was already one band, but the countdown rendered as a 2×2 grid, making the band ~2 rows tall, and inner padding was above the card scale. Numerals untouched (`.hero-num` clamp 40–64px, `.cd-val` clamp 22–28px preserved).

- Countdown 2×2 → **1×4 single row** on desktop (`repeat(4, minmax(0,1fr))`) — the band becomes one horizontal line: $10K block left, four countdown numerals right.
- Padding audited against the existing scale (card = 20/22, hero was 26/30 — an invented value): hero padding → **20px 24px**, band gap 28 → 24, margin-bottom 18 → 16 (the card rhythm value).
- Micro-spacing inside: label margin 10→6, sub margin 8→6, bar margin 14→10, pct margin 7→5, cd-box padding 8→2.
- ≤960px keeps the stacked single-column layout (already `repeat(4,1fr)`), ≤640px keeps 2×2.
- Combined with Change 3 removing the Command card from the flow, Off-Track Alerts / Execution Stack move up ~150px; on 1080p the hero band plus the top of the next section are visible without scrolling.

## Change 3 — Terminal → floating command dock

The Command card leaves the dashboard flow and becomes a global fixed dock, docked **bottom-centre** (`width: min(720px, 100%)`), not full-width: the content grid is column-based and a centred pill matches the reading measure of the main column, occludes less of the 348px right column, and is the established pattern (Claude/ChatGPT/Perplexity).

- Markup moved to body level (outside `#content` page-switching) — visible and functional on every page. Inner bar keeps `id="unified-cmd-card"`, and `#ucmd-input`, `#ucmd-mode-badge`, `#ucmd-result` keep their ids, so **`submitUnifiedInput`, `ucmdKey`, SSE parsing and all event handling are byte-identical — zero JS changes to the streaming path.**
- `#ucmd-result` sits *above* the input row so the dock expands upward while streaming; capped at `min(38vh, 320px)` with internal scroll — it never takes over the screen.
- Collapse-when-idle: additive listeners only — Escape or a click outside the dock hides the result panel, guarded so it never collapses mid-stream (`#ucmd-input.disabled` = streaming).
- New z token `--z-cmd: 180` — above page content, deliberately *below* the nav (200) and its backdrop (199) so the open mobile drawer covers the dock instead of the dock floating over the drawer.
- Occlusion: `.page` bottom padding 48 → 128px desktop, 140px mobile; toast container lifted above the dock.
- Mobile ≤768px: same pattern, full-width minus 12px gutters, `env(safe-area-inset-bottom)` respected. No bottom navigation exists in this app (mobile nav is the hamburger drawer), so no collision.

## Change 4 — Agents nav tab

**Not built.** See Change 0.

---

## Audit — results

- [x] **Desktop 1920×1080** (headless Chrome, width-accurate) — hero renders as one horizontal band ($10K block left, 4-across countdown right); Off-Track Alerts, Today's Execution Stack, Domain Radar and Non-Negotiables all visible above the fold; dock floats bottom-centre without covering content. Also verified live at the machine's real 1280×720 CSS viewport (1080p @ 150% scaling) — hero + alerts + radar above the fold there too.
- [x] **Mobile 375px** (headless Chrome) — hero stacks, countdown 2×2, dock full-width with gutters at the bottom, no collision (no bottom nav exists; mobile nav is the drawer, which deliberately covers the dock when open via z-order).
- [x] **Terminal from non-dashboard pages** — live click-through in Chrome: dock present and focusable on Timetable, Trading Desk, and Aether Command.
- [x] **SSE streaming post-move** — live round trip against the Render backend from the Aether Command page: badge → Thinking…, streamed reply rendered in the expanded panel ("5 active todos."), badge → Reply, input cleared/re-enabled. `submitUnifiedInput` and the SSE frame parser are byte-identical; only the container moved. Collapse verified: outside click hides the result panel; guard keeps it open while streaming.
- [x] **Console** — no errors or exceptions during the session.
- [x] **Full pytest** — 261 passed (backend untouched).

### Notes
- The impeccable detector re-flagged the same findings triaged in `rebuild_iteration_log.md` (gradient-text in /guide prose, `--ease-spring`, nav-rail width transition) — all pre-existing and already classified intentional/false-positive there; left unchanged.
- `CLAUDE.md`'s "IBM Plex Mono + Syne" description was already stale before this pass (it described the pre-rebuild skin); the live stack is now Bricolage Grotesque + Geist.
