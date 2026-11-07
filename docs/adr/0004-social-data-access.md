# ADR-0004 — Access to social-platform and messaging data

Date: 2026-11-05 · Status: accepted · Applies to: Phase 5D (brand analytics), 5B
(outreach channels), 6C (integrations)

## Context
Personal-brand analytics must show content performance from **real platform data only**:
no estimates, no manually typed metrics, nothing the platform did not report. Outreach
must be able to reach prospects on the channels they use (WhatsApp, email, LinkedIn).
Platform APIs vary from free and open (YouTube public data) to approval-gated or paid
(X read access, LinkedIn member analytics, Instagram insights, TikTok, WhatsApp Business).
The user is a solo founder with no budget for API tiers and no appetite for app-review
processes before there is content worth measuring.

## Options considered
| Approach | Verdict |
|---|---|
| Scraping profile pages or analytics screens | Rejected: against platform terms, brittle, and the numbers would be unverifiable |
| Paid API tiers (X Basic, data vendors) | Rejected on cost for a single user |
| Approval-gated APIs now (LinkedIn Marketing, Instagram Graph with app review, TikTok Display) | Deferred: the effort is justified only once there is a posting cadence to measure |
| Manual entry of metrics | Rejected: violates "never maintain anything by hand" and makes the numbers untrustworthy |
| **Official API where it is free and key-based** (YouTube Data API v3 for the user's own channel) | Chosen |
| **The platform's own analytics export files** imported through an upsert importer (LinkedIn content export; X export only if the tier allows) | Chosen — the data is the platform's, the friction is one file drop |
| WhatsApp Business Cloud API / Twilio for outreach | Rejected for now: Meta Business verification, template approval, per-message fees. `wa.me` deep links deliver the same message with one tap and no account |
| LinkedIn messaging automation | Rejected: no permitted API for personal accounts; manual channel with copy-to-clipboard |

## Decision
- **Real data only** means: an official API response or a platform export file. The UI and
  the assistant say "not connected" or "no data" instead of estimating. Metrics are stored
  as timestamped snapshots with their `source` (`api` / `export`).
- **v1 adapters**: `YouTubeAdapter` (API key, public statistics of the user's channel) and
  `LinkedInExportAdapter`; `XExportAdapter` only if the account can export analytics.
  Instagram and TikTok are not connected in v1; the adapter interface is ready for them.
- **Outreach channels**: `whatsapp` via `wa.me` links (the tap that opens the link is the
  send), `email` via the Gmail app-password SMTP path (6C), `linkedin`/`call`/`other` as
  manual channels with the text copied. Every send passes the human approval gate.
- Platform OAuth is used only where a free, non-restricted scope exists and a write is
  needed (Google Calendar, 6C). Gmail uses IMAP/SMTP with an app password to avoid
  restricted-scope verification.
- No scraping anywhere, including the market-intelligence web adapter's targets beyond
  the search page it already uses.

## Consequences
- Brand analytics will be partial at first (YouTube live; LinkedIn when the user drops a
  file). That is accepted: partial real data beats complete estimates.
- Adding a platform later is a new adapter class plus (possibly) an OAuth flow in 6C.
- Outreach needs one tap per WhatsApp message; there is no bulk send, by design.

## Verify before relying on it
- YouTube Data API v3: `channels.list` `forHandle` parameter, quota costs, and whether
  Shorts can be identified other than by duration.
- LinkedIn: that the user's account type offers the content/analytics export and the
  current column names (the importer fixture must mirror a real export).
- X: whether analytics export is available on the free tier in 2026.
- Gmail app passwords and IMAP availability (also in ADR-0002 / Phase 6C).
