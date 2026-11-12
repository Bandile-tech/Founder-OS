# ADR-0002 — Notification channel for proactive communication

Date: 2026-11-05 · Status: accepted · Applies to: Phase 6D, 6C (pairing)

## Context
Phase 6D needs the system to reach the user when the app is closed: a morning briefing,
alerts, deadline countdowns and reminders, with replies flowing back into the
orchestrator. Browser notifications were removed in Stage 6.5 because they only work
while the tab is open. The user is one person on a phone in Zambia; cost must stay at
zero and nothing may require business verification or a platform review.

## Options considered
| Channel | For | Against |
|---|---|---|
| **Web Push** (VAPID, service worker) | free; no third-party account; works when the tab is closed | iOS requires installing the site as a PWA and still throttles; needs a service worker file at the Netlify root and a `pywebpush` dependency; one-way (no reply) |
| **Email** (Gmail SMTP app password) | free, trivially available | slow to notice, no reliable two-way, briefings get lost in an inbox |
| **SMS / WhatsApp via Twilio or Meta Cloud API** | ubiquitous in Zambia | per-message fees; WhatsApp Business needs a Meta Business account and template approval; sender registration |
| **Telegram bot** | free; instant push on every phone; webhook or polling; **two-way** so a reply is just another orchestrator turn; commands (`/undo`, `/yes`) map onto existing confirmation and undo machinery; message length 4 k, formatting optional | one more app on the phone; the bot must be paired securely |
| **Discord / Slack webhook** | free, simple | team tools for a single user; two-way needs an app install and OAuth |

## Decision
- **Telegram is the primary channel.** One bot (BotFather), one webhook
  (`POST /integrations/telegram/webhook`, verified by Telegram's secret-token header), one
  paired chat (pairing code shown in Settings, derived from `FOUNDER_OS_SECRET`). Outbound
  through `notify.py` (`queue` → `dispatch`), inbound through `telegram_inbound.py` into a
  shared `orchestrator.run_turn`.
- **Web Push is the documented second channel**, not built in Phase 6: `notify.py` is
  written channel-agnostic (`channels_json`, a `Channel` interface) so adding it is a new
  class, a service worker and a VAPID key pair.
- Quiet hours, a daily cap and severity thresholds live in `notification_settings` and are
  enforced in `dispatch`, not in each sender. Suppressed items roll into the next morning
  briefing so nothing is lost.
- Replies and explicit confirmations (`/yes`) from the paired chat are treated exactly like
  typed input in the dock: same tools, same change log, same undo.

## Consequences
- Zero cost; no verification process; setup in minutes.
- Only the paired chat is ever answered; all other senders are ignored.
- Voice notes are not transcribed in v1.
- If Telegram is unacceptable later, the channel interface allows Web Push or email
  without touching the couriers.

## Verify before relying on it
- Bot API: `sendMessage` 4096-character limit and splitting; `setWebhook` with
  `secret_token` and the `X-Telegram-Bot-Api-Secret-Token` request header; how long the
  webhook may take to respond before Telegram retries (dedupe by `update_id` if needed).
- Hosting: the webhook URL is the Render service (HTTPS, public), which serves both the app
  (at `/app/`) and the API. There is no separate frontend host.
