# Refinement Pass 2 — Spec & Audit

Follow-up to `refinement_pass_1.md`. Frontend only.

---

## Step 0 — Agents tab unblock verification

**The market research agent now exists and is fully wired.** Commit e690142 added `backend/market_intel/`:

- `agent.py` — five-role pipeline: Research Planner → parallel Researchers (evidence from web / War Room / cold-archive adapters) → Analyst (merge + 5-axis scoring) → Founder Advisor (founder_fit + speed_to_mvp against `context/core/founder_profile.md`) → adversarial Verifier with a hard "no evidence → reject" floor. Surfaces findings with `status='surfaced'`; never writes to the opportunity pipeline itself.
- `memory.py` — DB-backed lesson notes (`ResearchMemoryNote`), upserted by slug, injected into the Planner prompt.
- REST surface in `main.py`: `GET /research/projects`, `GET /research/projects/{id}`, `GET /research/pipeline`, `POST /research/promote`, `PATCH /research/pipeline/{id}` (stages: discovered/researching/validating/building/rejected), `GET /research/memory`. Migration `m009_market_intel` runs on startup.
- Orchestrator tools 9 (`run_market_research_agent`) and 10 (`promote_research_opportunity`) — terminal invocation ("research X") already routes correctly; the Agents tab does not duplicate this.

**Verdict: fully wired, no backend work needed → Agents tab is built in this pass**, read/manage only (list projects + findings, promote surfaced findings, move pipeline stages, view lesson memory). Invocation stays in the command dock per pass 1.

The same commit's voice UI (mic → Whisper → TTS) was already merged into the working-tree dock by a parallel session; left as-is.

## Change 1 — Hero scroll bug

**Diagnosis (actual cause, confirmed in CSS):** not `position: sticky`/`fixed`. The dashboard used a nested-scroll architecture from the original rebuild: `#p-dashboard.active{display:flex;flex-direction:column}` + `.dash-grid{flex:1;min-height:0;height:100%}` + `.dash-left`/`.dash-right{overflow-y:auto}`. The page never scrolls — the hero is pinned and each column scrolls independently in the remaining viewport height. Pass 1 amplified the symptom: `.page` bottom padding 48→128px (dock clearance) shrank that inner window further, so content below the hero rendered in a squeezed band.

Not documented as deliberate in `refinement_pass_1.md` → treated as a bug per the brief.

**Fix:** dashboard becomes a normal flowing page — removed the flex-column/`height:100%` rules and both columns' `overflow-y:auto`; `.page` (the standard scroll container) now scrolls the hero away naturally. The ≤960px overrides that undid the nested scroll became the universal behaviour.

## Change 2 — Command dock design pass

Composer treatment (the Claude/ChatGPT/Perplexity pattern), all with existing tokens:

- **Surface**: the bar is the input — `.cmd-input` goes transparent/borderless; the bar carries the focus ring via `:focus-within`. Inner padding up (10/12 → 12/14), input padding up, controls pill-shaped.
- **Elevation**: deeper drop shadow + the established `.card--metric`-style top accent line (`::before` gradient hairline) so the dock speaks the "signature glass" language; hover brightens the border like every card.
- **Idle**: compact single row, quiet border. **Focused**: border + accent line brighten, soft outer glow ring (same rgba(167,235,242,…) alphas used elsewhere), 1px lift.
- **Streaming/expansion**: `#ucmd-result` is wrapped in `.cmd-result-wrap` — a grid whose `grid-template-rows` animates 0fr→1fr (300ms `--ease-out`), so the dock grows upward smoothly instead of snapping open. A `MutationObserver` watches the result's inline `display` (which existing JS toggles) and flips the wrapper class — **zero changes to `submitUnifiedInput`, SSE parsing, or event handling**. Close stays instant (house rule: exits faster than enters).
- Viewport margins kept (18px bottom desktop / safe-area mobile); never edge-to-edge.

## Change 3 — Right column reorder

`.dash-right` DOM order changes to: **Performance KPIs → Non-Negotiables → Domain Radar** (Mission card remains last, untouched). Mobile inherits DOM order — `@media(max-width:960px)` only stacks the same elements, there is no separate mobile ordering logic — so the one HTML move fixes both.

## Change 4 — Agents tab (built)

- New "Agents" nav item (System group), SVG icon in the established 16px/1.5-stroke family.
- **Registry view**: one card — Market Intelligence — because that is the only agent that exists. Shows status (Active while a project is running, else Available), one-line description, live counters (runs, surfaced findings, lessons).
- **Detail view**: invocation hint (points at the command dock — routing untouched); research projects newest-first with expandable findings (scores, discovery, founder fit, evidence, first-customer path); Promote button on `surfaced` findings → `POST /research/promote`; opportunity pipeline entries with stage select → `PATCH /research/pipeline/{id}`; lesson memory list.
- No placeholder agents, no invented settings — every element maps to a real endpoint.

---

## Audit — results

- [x] **Hero scroll fix** — live Chrome scroll test: the hero band scrolls fully out of view with the page; academics/trading KPIs, Non-Negotiables etc. below render at full size in the page flow (previously squeezed into an inner scroll window). Above the fold at desktop width: hero + Off-Track Alerts + Performance KPIs.
- [x] **Dock states** — idle: compact bar with quiet accent hairline. Focused: border/hairline brighten, glow ring, 1px lift (verified visually). Streaming: live SSE round trip from the Agents page — dock expanded upward via the grid-rows animation, streamed answer rendered ("Revenue is furthest behind: $25 / $10,000…"), badge Ready→Reply, input re-enabled. `submitUnifiedInput`/SSE parsing untouched — expansion is driven by a MutationObserver on the result panel's display.
- [x] **Right column order** — desktop 1920 headless capture: KPIs → Non-Negotiables → Radar → Mission. Mobile 375 headless capture: same order stacked (single DOM move; mobile has no separate ordering logic).
- [x] **Agents tab (live Render backend)** — registry card with real counters (1 run, 1 finding, 0 pipeline, 3 lessons; status Available). Detail: real project "Pain Points in AI Accounting Services for Emerging Markets" (completed), expanded finding shows tags, all 7 score axes, first-customer path, Reddit evidence link, and the Promote button; pipeline empty state; 3 real lesson-memory notes rendered. Promote/stage-change wired to `POST /research/promote` and `PATCH /research/pipeline/{id}` (not exercised against production data — they mutate state).
- [x] **Pytest** — 261 passed. Backend untouched by this pass (the agent backend shipped in commit e690142, which already existed).

### Notes
- The dashboard's nested-scroll removal also removes the "columns scroll independently" behaviour from the original rebuild — that behaviour was the bug's cause and was never documented as a design decision.
- The voice mic button / status UI inside the dock came from commit e690142 (merged by a parallel session); restyled sizes only, logic untouched.
