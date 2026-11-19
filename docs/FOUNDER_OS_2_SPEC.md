# Founder OS 2.0 — Complete Functional Specification

The objective: Build a proactive, conversational, AI-powered personal operating system that understands your life, manages your responsibilities, coordinates your projects and executes work on your behalf.

The fundamental difference from your current Founder OS:

- Old Founder OS: You open tabs, enter information, update trackers and ask the system for help.
- New Founder OS: It understands what's happening, maintains your information, identifies what needs doing, recommends priorities and takes authorised action.

The specification below separates the capabilities demonstrated or suggested by those videos from additional capabilities needed to support your ambitions.

# 1. The central AI assistant

**Status: Partial.** Evidence: typed and real-time voice conversation, one server-side thread, long-term memory and 62 tools that act (`backend/orchestrator.py`, `orchestrator_tools.py`, `voice_realtime.py`, `conversation.py`, `memory_service.py`). Workspaces opened by voice or text exist for seven views only (syllabus, brief, deadlines, note, changes, progress, history: `show_view`); business metrics, the pipeline and the agents open as pages, not as workspaces beside the conversation. A live voice check with a real key is still open (`ROADMAP.md`, Phase 2).

The heart of Founder OS

This is the most important component.

### Conversational interface

- Natural text conversations.
- Real-time voice conversations.
- Speech-to-speech interaction, not voice-note transcription.
- Interruptions and natural turn-taking.
- Context retained across conversations.
- Access to your personal, academic and business information.
- Ability to understand incomplete thoughts and brain dumps.
- Ability to ask clarifying questions when necessary.
- Ability to remember decisions and commitments.

### Execution capabilities

You should be able to tell it:

- "Plan my day."
- "Add this to my business pipeline."
- "Research this opportunity."
- "Update my Further Maths progress."
- "Follow up with that prospect tomorrow."
- "Show me what's missing from my Wharton application."
- "Analyse my business performance."
- "Delegate this research to an agent."

It should actually perform the relevant action, not simply explain how you can do it.

### Dynamic workspace and window management

This is particularly important.

The assistant must be able to open contextual workspaces without forcing you to navigate through menus.

For example:

| Your instruction               | What Founder OS does                  |
| ------------------------------ | ------------------------------------- |
| Show me business metrics       | Opens business analytics              |
| Show my pipeline               | Opens the sales pipeline              |
| Analyse my content             | Opens brand analytics                 |
| Let's work on academics        | Opens academic workspace              |
| Show my projects               | Opens project portfolio               |
| What's happening with Wharton? | Opens application tracker             |
| Show me the agents             | Opens agent control room              |
| Prepare my weekly review       | Opens an interactive review workspace |

These windows should be interactive, not static reports.

You can ask questions while viewing them, make changes conversationally and have the assistant update the underlying records.

Important: Opening a workspace should not interrupt the conversation. The assistant remains available alongside it.

# 2. Proactive intelligence and personal management

**Status: Partial.** Evidence: the daily command centre and task extraction are built (`backend/briefing.py`, `apply_parse_updates` in `main.py`, `actions.py`). Proactive communication is built for Telegram (`notify.py`, `agents/briefing_courier.py`) but no account is set up and the external schedule ships switched off (`docs/adr/0001`, amendment), so nothing reaches Bandile while the app is closed. Weekly planning is not built (`ROADMAP.md`, Phase 3). Outbound voice calls are not built.

**Phase 7 (branch `phase-7`):** the schedule now runs three times a day and Web Push is the first channel: the morning briefing, an evening check, reminders 7, 3 and 1 days out, payments due and the cost-cap alert reach Bandile with the app closed (`backend/push.py`, `notify.py`, `.github/workflows/jobs-tick.yml`, `tests/test_phase7_push.py`). The VAPID keys and the two repository secrets still have to be set.

Core functionality

This is what separates Founder OS from an ordinary chatbot.

### Daily command centre

- Automatically generate daily briefings.
- Present today's priorities.
- Show upcoming commitments.
- Identify overdue tasks.
- Highlight important deadlines.
- Track progress against goals.
- Identify neglected responsibilities.
- Recommend the highest-value next action.

### Proactive task management

- Automatically extract tasks from conversations.
- Convert brain dumps into structured actions.
- Identify deadlines and dependencies.
- Automatically carry unfinished tasks forward.
- Detect duplicate tasks.
- Prioritise based on urgency and importance.
- Update task statuses through conversation.
- Generate weekly execution plans.
- Conduct weekly reviews.
- Identify tasks that are no longer relevant.

### Proactive communication

- Initiate scheduled conversations.
- Deliver morning and evening briefings.
- Remind you of upcoming commitments.
- Alert you when something important changes.
- Follow up on outstanding responsibilities.
- Initiate voice calls where explicitly authorised and supported.

This is directly inspired by the Jarvis-style interaction in your videos: the assistant shouldn't always wait for you to open the application.

# 3. Business intelligence and commercial operations

**Status: Partial.** Evidence: market intelligence, lead qualification, outreach behind an approval gate, the CRM and brand analytics are built (`backend/market_intel/`, `leads.py`, `outreach.py`, `crm.py`, `brand/`; `docs/phases/PROGRESS.md` S1 to S11). Leads arrive by CSV or conversation: nothing discovers prospects on its own. Profitability and revenue forecasts are not built. Brand data covers YouTube and a LinkedIn export only.

Major functional department

This is not merely a CRM. It is your entrepreneurial operating system.

### A. Market intelligence

- Research industries and markets.
- Identify business opportunities.
- Research competitors.
- Identify customer pain points.
- Analyse market demand.
- Evaluate potential niches.
- Score opportunities.
- Identify emerging trends.
- Generate market intelligence reports.

### B. Prospecting

- Discover potential customers.
- Research individual businesses.
- Identify decision-makers and contact information.
- Qualify leads.
- Segment prospects.
- Score prospects based on fit.
- Build targeted prospect lists.
- Identify potential outreach opportunities.

### C. Outreach and sales

- Generate personalised outreach.
- Manage outreach campaigns.
- Track messages and responses.
- Schedule follow-ups.
- Track sales conversations.
- Identify stalled deals.
- Recommend the next sales action.
- Monitor conversion rates.
- Analyse outreach effectiveness.

### D. CRM and client management

- Contact and company records.
- Lead sources and segmentation.
- Deal stages and history.
- Client communication history.
- Client onboarding.
- Agreements and deliverables.
- Project delivery tracking.
- Client follow-ups.
- Retention and recurring revenue.

### E. Financial and business analytics

- Revenue tracking.
- MRR and recurring revenue.
- Payments and outstanding balances.
- Profitability tracking.
- Sales performance.
- Revenue forecasts.
- Business targets.
- Performance dashboards.
- Business health reports.

### F. Personal brand intelligence

- Social media performance monitoring.
- Content analytics.
- Identify top-performing posts.
- Identify successful content formats.
- Generate video ideas.
- Research content trends.
- Recommend content improvements.
- Maintain a content calendar.
- Track audience growth.
- Connect content performance to business opportunities.

For example, you should be able to ask:

"What should I post this week based on what has performed best over the last 30 days?"

Founder OS should analyse your actual performance data and give you an evidence-based answer.

# 4. Project and strategic management

**Status: Partial.** Evidence: the project portfolio with focus, maintain, pause and close recommendations, blockers and a weekly review is built (`backend/portfolio.py`; `PROGRESS.md` S8), and annual targets feed the briefing (`assistant_state.py`). Quarterly objectives, dependencies between projects and a check of daily work against long-term goals are not built.

Core functionality

Founder OS should understand everything you're working towards, not just the tasks you're doing today.

### Project management

- Central project portfolio.
- Project objectives.
- Milestones and deliverables.
- Task allocation.
- Deadlines.
- Progress tracking.
- Dependencies.
- Bottleneck identification.
- Project health assessments.
- Automatic progress reports.

### Strategic prioritisation

- Evaluate projects against long-term objectives.
- Identify projects with the highest potential impact.
- Identify neglected projects.
- Recommend which projects to prioritise.
- Recommend which projects to pause.
- Identify resource constraints.
- Monitor whether your daily activities align with your larger goals.

### Long-term mission tracking

- Annual objectives.
- Quarterly objectives.
- Long-term milestones.
- Financial targets.
- Personal development objectives.
- Business growth targets.
- Strategic progress reviews.

The assistant should understand your greater mission and use it as context when recommending priorities.

# 5. Academic and personal development

**Status: Partial.** Evidence: the four-state syllabus tracker, exam dates and weakest topics are built (`backend/syllabus.py`), as are applications with a requirements checklist and gap review (`applications.py`; `PROGRESS.md` S20, S21), notes for books, Bible and journal (`notes.py`) and the daily non-negotiables. A notes screen and past-paper score trends are not built (`ROADMAP.md`, Phase 4).

Core functionality

### Academics

- Syllabus tracking.
- Simple topic and subtopic status controls.
- Automatic progress calculations.
- Examination preparation.
- Revision planning.
- Weak-topic identification.
- Study schedules.
- Examination deadlines.
- Performance analytics.
- Study recommendations.

The syllabus tracker should use simple statuses, not tedious percentage sliders.

For example:

| Status      | Colour |
| ----------- | ------ |
| Not started | Red    |
| Read        | Yellow |
| Revised     | Blue   |
| Bulletproof | Green  |

### University applications

- University application database.
- Application requirements.
- Essay tracking.
- Recommendation tracking.
- Supporting document tracking.
- Application deadlines.
- Missing requirements.
- Profile gap analysis.
- Extracurricular achievements.
- Application readiness reports.

Example: "What is missing from my Wharton application?"

It should produce a proper gap analysis and action plan.

### Personal development

- Health and fitness tracking.
- Training schedules.
- Personal routines.
- Non-negotiables.
- Bible reading and reflections.
- Book reading and notes.
- Personal development goals.
- Weekly reflections.

These should largely be managed through conversation and connected documents rather than forcing you to maintain numerous dashboards.

# 6. Knowledge and memory management

**Status: Partial.** Evidence: vault search by meaning, notes, long-term memory and the conversation history are built (`backend/vault_sync.py`, `retrieval.py`, `memory_service.py`, `conversation.py`). The vault is only pulled at startup, every six hours while the app is awake, or by hand. Finding contradictions or outdated information is not built. See section 16.

Founder OS needs a persistent understanding of your information.

- Search your Obsidian vault.
- Retrieve previous decisions.
- Remember project context.
- Maintain conversation history.
- Store important commitments.
- Connect information across departments.
- Retrieve relevant documents.
- Summarise long documents.
- Identify contradictions or outdated information.
- Update knowledge records when instructed.

For example, if you ask about a business decision, it should be able to retrieve the previous market research, the relevant client information and the decision you made.

# 7. AI agent ecosystem

**Status: Built.** Evidence: as jobs, not as chatbots: one registry, one runner, thirteen agents, an activity log with Undo per run and the Agents page (`backend/agents/`; `PROGRESS.md` S13, S14 and the Agents UI). See "As built" below.

Advanced capability

This is inspired by the agent-management system in your references.

You should eventually have specialised agents working under a central orchestrator.

| Agent              | Responsibility                              |
| ------------------ | ------------------------------------------- |
| Executive Agent    | Overall coordination and priorities         |
| Research Agent     | Market and opportunity research             |
| Business Agent     | Prospecting, outreach and sales             |
| Brand Agent        | Content and performance analytics           |
| Academic Agent     | Syllabus and study management               |
| Projects Agent     | Project monitoring and execution            |
| Applications Agent | University and opportunity management       |
| Knowledge Agent    | Documents, memory and information retrieval |

### Agent management

- View available agents.
- Assign tasks to agents.
- Delegate research.
- Monitor progress.
- View completed work.
- Review reports.
- Coordinate multiple agents.
- Share relevant context.
- Require approval for sensitive actions.
- Maintain an activity history.

The important distinction: agents should be able to work together, not operate as eight disconnected chatbots.

### As built

The agents are not eight chatbots. They are jobs. One conversational assistant (the orchestrator) is the
only thing Bandile talks to. Each agent is a plain function with a declaration of when it runs, which tools
and tables it may touch, whether it may notify, and how many model calls it may make. One runner starts the
agents that are due, one after another, records each run, checks afterwards that the run only wrote to the
tables it declared, and undoes the run if it did not. Agents pass work to each other through events
(a payment falls overdue, a lead replies), not through conversation.

Thirteen agents ship: market intelligence, lead qualifier, follow-up sentinel, CRM housekeeper, portfolio
reviewer, brand sync, application watch, calendar sync, email sync, vault export (Phase 7), and three
couriers (briefing, alerts, reminders). The departments in the table above map onto them and onto the assistant's tools: the Executive
role is the assistant itself with the daily briefing; Research is the market intelligence agent; Business is
the lead qualifier, the follow-up sentinel and the CRM housekeeper; Brand is brand sync; Projects is the
portfolio reviewer; Applications is application watch. The Academic and Knowledge roles have no agent: they
are tools the assistant calls (`update_syllabus`, vault and notes search, `remember`).

# 8. Visual reporting and dynamic workspaces

**Status: Partial.** Evidence: the full-screen stage shows seven views as KPI cards, a list and next actions (`frontend/index.html`, `renderStageView`), and the Business page has Pipeline, Clients, Portfolio and Brand tabs. Interactive charts, drill-down and a report workspace are not built.

This is where the Rose-style experience from your references becomes important.

The assistant shouldn't just give you walls of text. It should be able to produce visual, interactive outputs.

Examples:

- Business performance reports.
- Market research reports.
- Project progress reports.
- Academic performance reports.
- Personal brand analytics.
- Application readiness reports.
- Weekly executive reviews.
- Agent activity reports.

These should support:

- Interactive charts.
- Tables.
- Progress indicators.
- Comparisons.
- Source references.
- Recommendations.
- Action buttons.
- Drill-down into underlying information.

You should be able to say:

"Show me a report of my business performance this month."

The assistant opens a proper visual report, explains the findings and lets you investigate further.

# 9. Integrations and infrastructure

**Status: Partial.** Evidence: Google Calendar, email over IMAP and SMTP, Telegram and YouTube are built in code (`backend/integrations_google.py`, `integrations_email.py`, `integrations_telegram.py`, `brand/adapters.py`; `PROGRESS.md` S15 to S19) but none has been set up or checked against the live service (`PROGRESS.md`, "External setup" and "VERIFY items"). WhatsApp is a link Bandile taps, not a connection. Supabase and OpenAI Realtime are in use.

For the system to become genuinely proactive, it needs connections to external services.

| Integration         | Purpose                                 |
| ------------------- | --------------------------------------- |
| Google Calendar     | Schedule and deadlines                  |
| Gmail               | Emails and follow-ups                   |
| WhatsApp            | Business communication, where supported |
| Social platforms    | Content analytics                       |
| Obsidian            | Personal knowledge                      |
| Supabase            | Persistent data                         |
| OpenAI Realtime     | Live voice conversations                |
| Research services   | Market intelligence                     |
| External automation | Scheduled and event-triggered actions   |

### Proactive channels

How Founder OS reaches Bandile when the app is closed, in the order they are built:

| Channel        | State   | Notes                                                                              |
| -------------- | ------- | ---------------------------------------------------------------------------------- |
| Web Push       | First   | Notifications from the installed web app. No account to set up, no cost            |
| Telegram       | Paused  | Built in Phase 6 (pairing, briefing, replies). Not set up; resumes in Phase 8      |
| WhatsApp       | Later   | Needs a business account and a paid provider                                       |
| Outbound calls | Later   | Needs a telephony provider; only ever where explicitly authorised                  |

Security, authentication, permissions, audit logs, backups and reliable error handling are foundational requirements.

# 10. Testing Founder OS against the videos

**Status: Partial.** Evidence: of the acceptance tests below, groups 1, 4 and 5 are met in code; group 2 waits on a schedule that runs while the app is closed and on a delivery channel; group 3 is met for the seven stage views only; group 6 is met except that agents do not hand work to one another beyond events (`backend/agents/runner.py`).

This is the most important part of the specification. We need to test whether we're recreating the capabilities and experience demonstrated, rather than simply building a futuristic-looking interface.

| Video reference                   | What it demonstrates or suggests                                     | Founder OS requirement                                               |
| --------------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- |
| AI assistant / visual interface   | An immersive AI environment                                          | Central AI presence and conversational workspace                     |
| Agent management reference        | Coordinated AI agents and specialised work                           | Agent control room, delegation and monitoring                        |
| Proactive assistant reference     | Remembers previous work, tasks and commitments                       | Persistent context, automatic task management and proactive planning |
| Jarvis driving reference          | Assistant initiates conversations and handles work while you're away | Outbound voice, hands-free interaction and authorised execution      |
| Rose / visual reporting reference | Presents information and reports visually                            | Dynamic reports, interactive dashboards and conversational analysis  |

## Acceptance tests

These are the actual tests I'd use to decide whether the rebuild has achieved the vision.



1\. Conversational intelligence

I can speak naturally without recording and waiting for a transcription.

I can interrupt the assistant and it responds naturally.

It remembers relevant previous conversations.

I can ask it to perform actions, not just answer questions.

2\. Proactive execution

It prepares my daily briefing automatically.

It identifies unfinished tasks and carries them forward.

It alerts me to approaching deadlines.

It can initiate a scheduled check-in.

3\. Dynamic workspaces

I can ask it to open business metrics.

I can ask it to open my academic tracker.

I can ask it to display a visual report.

I can interact with an open workspace while continuing the conversation.

It updates the underlying data when I request changes.

4\. Business intelligence

It can research and qualify prospects.

It can prepare targeted outreach.

It tracks deals, clients and revenue.

It analyses content performance when connected to social data.

It recommends business priorities based on evidence.

5\. Strategic intelligence

It understands my active projects and objectives.

It can identify project bottlenecks.

It can identify missing university application requirements.

It can recommend what deserves my attention next.

6\. Agent ecosystem

I can delegate research to a specialised agent.

I can see what agents are working on.

I can review their outputs.

Agents can share relevant information and coordinate work.


# 11. The minimal-friction operating principle

**Status: Partial.** Evidence: conversation and one-tap controls are the way in everywhere (`CLAUDE.md`, product principles 1 and 2). Automation is limited by the schedule being switched off (`docs/adr/0001`, amendment).

This is the most important design constraint of the entire project.

| Old approach                          | Founder OS 2.0                                 |
| ------------------------------------- | ---------------------------------------------- |
| Manually create tasks                 | Extract tasks from conversations               |
| Manually update progress              | Update through conversation or simple controls |
| Navigate through multiple tabs        | Ask the assistant to open a workspace          |
| Manually prepare daily plans          | Automatic daily briefing                       |
| Manually research prospects           | Delegate prospect research                     |
| Manually analyse business performance | Request an automatically generated report      |
| Manually track deadlines              | Central deadline monitoring                    |
| Maintain separate notes everywhere    | Unified searchable knowledge                   |
| Remember every commitment             | Persistent context and reminders               |
| Decide everything yourself            | AI-supported prioritisation                    |

There should be three ways to interact with the system:

1. Conversation: The default way to communicate and delegate.
2. Visual workspaces: For inspecting, editing and analysing information.
3. Automation: For work that should happen without you initiating it.

The assistant should intelligently decide when to use each.

# 12. Final product hierarchy

**Status: Partial.** Evidence: seven of the eight departments have a page or a tab (`frontend/index.html`: Command Centre, Business, Syllabus and Health, Portfolio, Brand, Applications, Agents). Knowledge has search and a Conversations page but no workspace of its own (`ROADMAP.md`, Phase 8).

FOUNDER OS INTELLIGENCE

Conversation · Memory · Reasoning · Orchestration

01

Executive

Daily execution and priorities

02

Business

Research, sales and operations

03

Personal

Academics, health and development

04

Projects

Strategy and project portfolio

05

Brand

Content and analytics

06

Applications

Universities and opportunities

07

Knowledge

Documents and memory

08

Agents

Delegation and coordination

Shared execution infrastructure

Database · Integrations · Permissions · Scheduling · Notifications · Audit logs

# 13. Governing rule

**Status: Built.** Evidence: adopted as product principle 1 (`CLAUDE.md`, "Founder OS must never need maintaining").

If a feature requires more effort to maintain than the value it creates, redesign it or remove it.

Founder OS should give more time to study, build Aether AI, develop the personal brand and pursue long-term ambitions. It must never become another project competing for that time.

# 14. Model routing and cost

**Status: Built.** Built on the `phase-7` branch; on `main` once its pull request is merged. Evidence: `backend/llm.py`, `backend/llm_adapters/`, `backend/model_routes.py`,
migration `m026`, `tests/test_phase7_model_router.py`; `docs/phases/PROGRESS.md`, Phase 7, Stage 1.

Founder OS should not be tied to one model provider or one model.

- Every model call goes through one gateway, which looks up the route for the task: which provider, which
  model, which parameters.
- Two providers, OpenAI and Claude, each behind a small adapter the project owns. Each works with only its
  own key set. With no key, everything that is not AI keeps working.
- Routes are changed by conversation ("use Claude for chat") or from Settings, and every change can be undone.
- When a provider fails, times out or refuses, the call is retried once on the other provider at the
  matching tier.
- Every call is logged with its provider, task and estimated cost. A monthly cap moves the expensive routes
  to the cheapest model at 80 percent and says so once in the briefing.
- Real-time voice stays on OpenAI.

# 15. Personal finance ledger

**Status: Built.** Built on the `phase-7` branch; on `main` once its pull request is merged. Evidence: `backend/finance.py`, migration `m027`, the Budget page in
`frontend/index.html`, `tests/test_phase7_finance.py`; `docs/phases/PROGRESS.md`, Phase 7, Stage 2.

Bandile says what he spent or received, and every balance is up to date.

- Money sits in buckets (tithe, business, invest, save, personal, or whatever he names).
- "Spent K50 on lunch and K120 on data, got K800 from Bel" is one entry in the conversation and one Undo.
- Income is split across the buckets by rules he sets in conversation. Spending comes out of the bucket he
  names, or the default one.
- A client payment recorded in the CRM is the same money, recorded once.
- Balances are always worked out from the transactions. Amounts are whole ngwee. The model never does the
  arithmetic.
- A Budget view shows the buckets, the month's money in and out, spending by category and the transactions.

# 16. Second brain sync

**Status: Built.** Built on the `phase-7` branch; on `main` once its pull request is merged. The webhook, the token and Obsidian's auto-push still have to be set up
(`docs/DEPLOY.md`, Phase 7). Evidence: `backend/vault_webhook.py`, `vault_export.py`, `vault_sync.sync_paths`,
migration `m028`, `tests/test_phase7_vault.py`; `docs/phases/PROGRESS.md`, Phase 7, Stage 3.

The Obsidian vault and Founder OS should agree within minutes, in both directions.

- Obsidian commits and pushes on its own every ten minutes. GitHub tells Founder OS about each push, and
  only the files that changed are read and indexed again.
- Once a day, the notes the assistant created or changed are written into one vault folder, `Founder OS/`.
  Only the assistant writes there, so nothing conflicts.
- Settings shows when the vault was last synced. The manual sync stays as a fallback.

# 17. Claude connector (MCP)

**Status: Missing.** Evidence: nothing in `backend/` serves the Model Context Protocol. Planned as Phase 7,
Stage 5, and only after Bandile approves the authentication design; otherwise Phase 8 (`ROADMAP.md`).

Founder OS becomes a connector. Bandile's Claude projects ask it directly instead of him uploading
documents to them.

- Tools: search the brain (vault, notes and memory), get the briefing, get tasks, get the finance summary,
  append a note.
- Reads only by default. Anything that writes goes through the change log and stays behind a switch that is
  off until he turns it on.
