# Fable 5 prompt — Command Centre redesign + voice

Draft for review before execution. Paste this as the task prompt when running Claude Fable 5 on this repo.

---

## Effort / operating instructions

Run at `high` effort. This is a first-shot, well-specified frontend + backend feature — sustain a long single-pass run rather than checkpointing constantly.

When you have enough information to act, act. Do not re-derive facts already established below, re-litigate a decision already made, or narrate options you won't pursue in user-facing messages. If you're weighing a choice, give a recommendation, not a survey.

Don't add features, refactor, or introduce abstractions beyond what this task requires. Don't design for hypothetical future requirements. Don't add error handling, fallbacks, or validation for scenarios that can't happen — trust this codebase's existing guarantees and only validate at real boundaries (user input, the OpenAI API).

Pause for the user only when the work genuinely requires them: a destructive action, a real scope change, or input only they can provide (e.g. picking a TTS voice after hearing samples). Otherwise proceed end to end.

---

## Context: what Founder-OS is

Founder-OS is a personal command centre — a single-user FastAPI + vanilla-JS app tracking KPIs (sprint times, academic syllabus %), habits, a roadmap, annual targets, and an AI brain-dump/chat feature backed by GPT-4o-mini. Full architecture is in `CLAUDE.md` at the repo root — read it first. Key facts:

- Frontend is one self-contained file: `frontend/index.html` (inline CSS + JS, no build step, no framework).
- Backend: FastAPI in `backend/main.py`, models in `models.py`, OpenAI integration in `openai_client.py`.
- Current design system ("Luna palette"): dark background `#0A0B0D`, cyan glow `#A7EBF2`, IBM Plex Mono / Syne / Inter, `--ease-ios` / `--ease-spring` cubic-beziers already defined as CSS variables.
- `POST /parse` — brain dump text → GPT-4o-mini → atomically applies updates to KPIs/todos/habits/roadmap/targets.
- `POST /chat` — stores history in `chat_messages`, injects `AIMemory` into system prompt.

## Why this matters

This is Bandile's personal daily-use tool — he wants it to feel like his own Jarvis: something he can glance at and talk to, not just click through. The visual and voice work below should make the app feel alive and premium, not just "more animated."

## Visual reference

A screenshot set exists at `frontend/design-reference/` (attach the images from the `AI` Google Drive folder before running — `IMG_6201–6207.PNG`, the "chase.h.ai — build your Fable 5 OS in 5 steps" carousel). Pull the following motifs, not the literal content:

- A terminal-style card (dark, monospace, thin cyan-ish border) sitting on a warm terracotta/amber background for hero or header moments — adopt this warm+dark contrast as a new accent layer on top of the existing dark Luna palette, not a full palette replacement. The base app stays dark; the warm tone is used deliberately for emphasis surfaces (e.g. a hero stat, a headline card), the way the reference uses it against otherwise dark screens.
- Big, animated counter numbers for key stats (e.g. `135,000` ticking up) — apply this treatment to 1–2 of Founder-OS's own most important live numbers (e.g. overall KPI completion, current sprint PB, syllabus %).
- A glowing particle/constellation visualization as a secondary panel next to dense stat readouts — repurpose this as a visualization of Founder-OS's own live data (e.g. habit streak density, weekly target completion nodes, or the AI memory graph), not literal social-media metrics.
- Dense, left-aligned monospace stat panels (small caps labels, tight line-height) — apply to the KPI/habit dashboard panels.

Keep every other current feature and information architecture. This is a full visual overhaul of *how existing panels look and move*, not a rebuild of what they contain.

## Motion

Apply Emil Kowalski–style polish (invoke the `emil-design-eng` skill/philosophy already documented in this repo — see the `78` "Frontend animation and transition system catalogued" note if present) across the whole app: transitions already use `--ease-ios` / `--ease-spring` — extend that consistency to every interactive surface (panel open/close, KPI updates, nav, chat send/receive, the new voice indicator). No motion should exceed ~300ms for direct-manipulation feedback; longer, more theatrical easing is reserved for hero/counter reveals only.

## Voice

Add voice as an alternative input/output channel to the existing AI chat panel — not a replacement for typing, and not tied to brain dump specifically. Behavior:

1. A mic button in the chat UI. Tap to start recording, tap again (or on a pause) to stop.
2. Recorded audio is sent to the backend, transcribed via OpenAI's speech-to-text API (Whisper via the OpenAI API, consistent with the existing `OPENAI_API_KEY` setup in `backend/.env` — do **not** build a local faster-whisper/Kokoro pipeline; cloud APIs only, per decision below).
3. The transcript is submitted through the existing `POST /chat` flow exactly as if typed.
4. The reply is spoken back via OpenAI's text-to-speech API and played in the browser, in addition to being shown as text. Make the TTS voice a single named constant/config value (e.g. in `openai_client.py`) rather than hardcoding it inline in multiple places — leave a comment noting it's a placeholder pending the user picking a voice by ear. Do not ship a voice-selection UI; that's a follow-up.
5. Typing must continue to work exactly as today, untouched.

New backend surface needed: endpoint(s) for audio upload → transcript, and text → speech audio. Follow the existing endpoint/schema conventions in `main.py` and `schemas.py`.

## Boundaries

- Don't touch KPI aggregation logic (`_kpi_state`, `KPI_DEFAULTS`, `_at_progress_pct`, `kpi_pct()`), the weekly target/snapshot/freeze lifecycle, or the `DailyLog → LogImpact → WeeklyTargetSnapshot` chain — these are called out as critical in `CLAUDE.md` and are out of scope for a visual/voice task.
- Don't change the `/parse` extraction schema or the seeded defaults.
- Don't introduce a frontend build step or framework; keep `frontend/index.html` self-contained.
- Don't add a local STT/TTS pipeline (no faster-whisper, no Kokoro) — cloud APIs only, decided above.

## Verification

Before reporting progress, audit each claim against a tool result from this session — if something isn't verified, say so explicitly. Start the app (`uvicorn main:app --reload` from `backend/`) and check in a browser: the redesigned panels render, the new visualization pulls real data (not placeholder numbers), the mic button records/transcribes/gets a spoken reply, and typing still works. Report what you actually saw, including any failures.
