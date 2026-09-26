# Meta Lead Ads CRM Connector — Odoo 19 Community

Technical module: `crm_meta_lead_ads`

## Included
- Meta OAuth connection for managed Facebook Pages
- Page webhook subscription (`leadgen`)
- HMAC-SHA256 verification of incoming webhooks
- Buffered ingestion queue and exponential retry
- PostgreSQL-level idempotency for queue rows and CRM leads
- Dynamic per-form field mapping
- Facebook / Instagram UTM sources
- Recovery polling for missed leads with cursor pagination, server-side
  time filtering (`filtering/time_created` — live-verified on Graph v25.0;
  plain `since` is silently ignored by Meta), and a bulk pre-check enqueue
  that never relies on unique-constraint violations as the normal path
- Multi-company record rules and company-bound configuration
- Token invalidation handling and administrator activity alert
- Immutable-style audit interface / payload logs
- Meta user-data deletion callback and status URL
- Odoo CRM lead traceability fields
- Ad attribution on CRM leads: campaign/ad set/ad IDs and names fetched
  with the lead itself (no extra permissions); optional and never
  blocking lead creation. Legacy `meta_adgroup_id` stays in sync with
  the canonical `meta_adset_id` (backfilled by the `19.0.3.0.0`
  idempotent migration).

## Meta Inbox (Messenger conversations)
- One `meta.conversation` per (Page, Page-Scoped User ID); `channel = messenger`
  prepares the model for Instagram Messaging later.
- Incoming Messenger webhooks upsert the conversation, keep idempotency by Meta
  message ID, track unread counts, and schedule a single To-Do activity for the
  assignee (or the default Inbox user configured in Settings > Meta Lead Ads).
- Replies are sent from the conversation form via the Messenger Send API.
  Replies are blocked locally when the conversation is closed, the account is
  disconnected, the page token is missing, the customer never wrote first, or
  more than 24 hours passed since the last inbound message (Meta's standard
  messaging window — Meta remains the final authority).
- Attachments are stored as link/metadata only; files are never fetched by the
  server, which avoids SSRF exposure from external URLs.
- **Create Lead** converts a conversation into a CRM lead with the
  *Meta Messenger* UTM source; each conversation can create a single lead, and
  both records stay linked for navigation.
- Legacy messages recorded before this feature are grouped into conversations
  automatically by the `19.0.2.0.0` post-migration, without deleting or
  modifying original message content.
- Runs on Odoo 19 Community only: no Enterprise modules, no paid Odoo Apps,
  no external SaaS. The Messenger API currently has no per-message fee, but
  Live mode + App Review (`pages_messaging`) are required for customers who
  are not app admins, and Meta policies may change.

## Connector Health (Batch 3A)

- CRM > Meta Lead Ads > Health is visible to Meta Lead Ads managers only.
  Opening it reads local Odoo evidence; **Run Diagnostics** is the explicit
  remote Graph API check. Subscription checks are not proof of end-to-end
  webhook delivery.
- Authenticated live webhook receipt, successful lead enqueue and successful
  message recording are separate timestamps. Historical imports do not fill
  these fields, so old pages initially show Unknown rather than a fabricated
  live connection.
- Monitoring is **off by default**, including upgrades. A manager may opt in
  per account, select an active internal Meta manager with company access as
  owner, and adjust overdue queue (15 minutes), optional traffic silence
  (disabled by default) and known-token-expiry (7 days) thresholds.
- The five-minute local-only assessment records scoped numeric counts and
  deduplicated internal incidents. It does not call Meta, resend Messenger
  replies, expose customer messages or tokens, or notify when the owner loses
  eligibility. Acknowledging suppresses repeat reminders for that episode;
  recovery completes only its dedicated Meta Health activity.
- A quiet page is not classified as disconnected unless an operator explicitly
  enables an expected-traffic silence warning. A stopped Odoo host cannot run
  its own cron; external host monitoring remains separate.

## Installation
1. Copy `crm_meta_lead_ads` into an Odoo 19 addons path.
2. Restart Odoo and update Apps List.
3. Install **Meta Lead Ads CRM Connector**.
4. Add administrators to **Meta Lead Ads Manager**.
5. Open Settings > Meta Lead Ads and set App ID, App Secret, Verify Token and Graph API version.
6. Set Meta Webhook Callback URL to `https://YOUR_DOMAIN/meta_crm/webhook` and subscribe to `leadgen` and `messages` / `messaging_postbacks`.
7. Set Meta Data Deletion Callback URL to `https://YOUR_DOMAIN/meta_crm/data_deletion`.
8. Save settings and click **Connect Facebook Page**.
9. Open CRM > Meta Lead Ads > Pages; subscribe the desired Page and sync forms.
10. Open Forms & Mapping; generate mappings and adjust custom-question destinations.

## Production notes
- HTTPS is mandatory for public Meta callbacks.
- On self-hosted Odoo, enable `proxy_mode = True` behind Nginx.
- Keep `web.base.url` stable; OAuth callback URLs must match exactly.
- Never expose App Secret or Page Access Tokens to non-system users.
- Graph API version is configurable so the connector can be upgraded without rewriting business logic.

## Meta app review
Live production access is controlled by Meta. Configure only the permissions that your current Meta app/use-case requires and verify them against Meta's current official documentation before submitting App Review.
