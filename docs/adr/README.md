# Architecture decision records

Short records of decisions that shape Phases 5–6. One file per decision. Status is
`accepted` unless stated. Each record ends with what must be verified against external
documentation before the decision is relied on in production.

| # | Decision |
|---|---|
| [0001](0001-background-jobs-on-render-free-tier.md) | Background jobs: external tick from GitHub Actions into `POST /jobs/tick`, in-process scheduler as best effort |
| [0002](0002-notification-channel.md) | Notification channel: Telegram bot, two-way; Web Push as the documented second channel |
| [0003](0003-agent-architecture.md) | Agents: declarative registry of plain Python modules run sequentially by one runner, permissions enforced through the change log |
| [0004](0004-social-data-access.md) | Social data: official APIs or platform exports only; no scraping, no estimates, no paid API tiers |
