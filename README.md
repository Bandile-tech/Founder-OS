# Founder OS

A personal operating system for disciplined execution, KPI tracking, and systems thinking.

This is not a motivation app.
This is a command interface for daily execution.

⸻

What this is

Founder OS is a lightweight, browser-based system that combines:
	•	Daily execution logging
	•	Weekly objectives & KPI tracking
	•	Visual progress indicators (arc-based KPIs)
	•	A built-in AI chat interface for reflection and reporting

It is designed to scale psychologically from student → founder → group CEO without needing redesign.

⸻

Core Principles
	•	Systems over vibes
	•	Discipline over dopamine
	•	Execution over intention
	•	Long-term clarity over short-term motivation

If it doesn’t improve decision-making or execution, it doesn’t belong here.

⸻

Tech Stack
	•	HTML / CSS / Vanilla JavaScript
	•	SVG for KPI visualisation
	•	localStorage for persistence
	•	Optional FastAPI backend for chat (/chat endpoint)

No frameworks. No dependencies. Full control.

⸻

How it works

1. Weekly Objectives
	•	Define weekly objectives with target execution counts
	•	Stored in localStorage
	•	Used as the backbone for all KPI calculations

2. Daily Execution Logs
	•	Log daily actions and contributions
	•	Each log links to a weekly objective
	•	Automatically feeds KPI metrics

3. KPI System
	•	Weekly KPI aggregation (last 7 days)
	•	Visual arc indicators (open-bottom, executive-style)
	•	Text KPI summary (counts + averages)

4. AI Chat
	•	Session-based chat with backend
	•	Can query KPI status using natural language (e.g. “KPI”, “on track”)
	•	Designed for future expansion into a full personal assistant

⸻

Running the project

Option A — Open locally

Just open index.html in a browser.

Option B — With backend

Ensure your backend is running at:
http://127.0.0.1:8000/chat
Then open the frontend normally.

Option C — Replit (mobile-friendly)
	•	Import the GitHub repo into Replit
	•	Use for light edits, UI tweaks, and review from phone

⸻

Data persistence (important)

All execution data is stored in browser localStorage.

Implications:
	•	Data is browser-specific
	•	Phone ≠ laptop data
	•	Replit preview ≠ local browser

Planned upgrade:
	•	Manual export/import of data as JSON
	•	Eventual backend sync

⸻

What NOT to touch casually
	•	KPI aggregation logic
	•	Weekly objective structure
	•	Execution log schema

These are system-critical components.

⸻

Roadmap (high level)
	•	Manual data export/import
	•	Dark-mode optimisation (already palette-ready)
	•	KPI trend history (weekly/monthly)
	•	Assistant-driven insights (“You’re drifting”, “Execution down 20%”)
	•	Transition from localStorage → backend persistence

⸻

Status

v0.1 — Stable
	•	KPI arcs working
	•	Execution logging stable
	•	UI aligned with founder-grade palette
	•	Safe to build on

⸻

Philosophy (non-negotiable)

This system is not meant to inspire you.
It is meant to command you.

If it becomes pretty but ineffective, it has failed.

