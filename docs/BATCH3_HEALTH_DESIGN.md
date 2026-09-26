# Batch 3A — Meta connector health and alerts

Date: 2026-09-26
Status: concrete design for review; no implementation or deployment yet.

## Purpose and evidence

Give the responsible operator one place to determine whether incoming Meta
events are being received, processed, or awaiting intervention. Preserve
Odoo 19 Community, existing lead creation restrictions, company isolation,
and the working Messenger reply/CRM linkage flows.

The user has confirmed receipt of a Messenger reply and removal of the
conversation from Awaiting First Response. The later permission issue came
from the separate inverse_crm_lead_control module; do not bypass it.

Local inspection found the checkout at cd1711b, while the locally recorded
origin/main is 47574a5 (PR #10), including linkage hardening after PR #8.
These are local Git observations, not a fresh remote or production audit.
Implementation must refresh remote state and branch from current main,
preserving other worktrees and their changes.

## Existing facilities to reuse

- meta.account.action_run_diagnostics: connection, permission and page
  subscription checks. Improve its outcome reporting rather than create
  a competing diagnostic implementation.
- meta.page: subscription status and check time, last form-sync time.
- meta.lead.queue: received/processed timestamps, retry schedule,
  failed/ambiguous outcomes and existing manager recovery actions.
- meta.sync.run: resumable sync, completion/warnings and step outcomes.
- meta.message: inbound/outbound records and failed send states.
- mail.activity: internal task notifications without a paid service.

## Approach selection

Chosen: extend the existing account/page records with health metrics and
add an internal alert record. This reuses the connector's existing ACLs
and operational actions without introducing a separate monitoring stack.

Alternative: only improve the Diagnostics tab. Smaller, but lacks periodic
alerts and stored incident lifecycle. External monitoring can later cover
host outages, but introduces another service and is outside this batch.

## Health screen

Add CRM > Meta Lead Ads > Health, visible to Meta managers within their
allowed companies. Provide account list and detail views with:

- account state and last diagnostic time/outcome;
- subscription status and freshness per active, sync-enabled page;
- last authenticated inbound webhook event observed for each page;
- last successfully recorded live inbound message and enqueued live lead;
- due queue backlog, oldest due age, failed and ambiguous counts;
- failed outbound count in the last 24 hours;
- last sync result and time;
- check time and monitoring enabled/disabled state.

Use aggregate queries scoped by company AND account/page. Drill-down
actions must retain the same scope and normal record rules. Counts may
include customer records but the screen shows no message bodies, tokens,
PSIDs, raw API responses or contact information.

Opening the screen uses stored/local data only. The existing Run
Diagnostics button explicitly performs remote checks. A subscription
check is not proof of end-to-end message delivery; show this distinction.

## Evidence semantics

Update webhook timestamps only after signature validation and matching a
configured page under the applicable account. Invalid signatures,
unmatched recipients and unrelated account callbacks cannot update health.
Store receipt time separately from processing success. A historical sync
must never advance the live webhook timestamp or make a stale live
connection look active. Repeated deliveries do not create repeated alerts.

Do not backfill live-receipt timestamps from received_at: historical import
also creates records with that timestamp. Existing pages start Unknown
until a new authenticated event supplies evidence.

Health levels: Disabled, Unknown, No detected issues, Warning, Error.
Never label a quiet page disconnected just because no customer wrote.
Missing/expired diagnostic evidence yields Unknown or a freshness warning,
not a fabricated current Meta permission or token status.

## Monitoring and alert lifecycle

Account settings: enabled (default false on upgrade), owner, due-backlog
threshold (default 15 minutes), optional expected-traffic silence interval
(default disabled), and token-expiry warning interval (default 7 days).
Owner must be an active internal Meta manager with access to the company.
Validate this on configuration and before notification; if access changes,
retain the visible alert and show that no eligible recipient is configured.

Run local assessment every five minutes with a bounded batch/cursor over
enabled accounts and per-account savepoints. No Graph API polling in this
cron. Manual diagnostics is the remote evidence refresh for this batch.

Alert conditions:

- known account error/disconnection while monitoring is enabled;
- known token expiry or impending expiry (unknown expiry remains unknown);
- freshly checked failed/incomplete subscription; checks older than 24h
  become stale-check warnings rather than assertions of a current failure;
- pending/retry jobs overdue beyond the configured threshold (future retry
  dates do not count), or processing records without progress beyond it;
- failed or ambiguous queue records requiring operator intervention;
- outbound failures in the last 24h, labeled recorded failures rather than
  proof the recipient did not receive a message;
- optional silence: "No recent webhook observed — verify expected traffic".
  Start its clock at monitoring enablement when no live receipt is known.

Use meta.health.alert keyed by account/page/check code. Keep one record per
key and reuse/reopen it for recurring incidents. States: open, acknowledged,
resolved. Store first/last seen, resolved time and a numeric summary. A
unique key and transaction-safe upsert prevent duplicate incidents.

Create a dedicated Meta Health activity type. At most one open activity per
incident; unchanged cron passes do not repost chatter or create activities.
Acknowledging an incident suppresses reminders for that episode. Recovery
resolves the incident and completes only its own dedicated activity. A new
episode after recovery may notify again. Never complete unrelated To-Dos.
Do not subscribe external contacts to incidents.

## Diagnostics outcome correction

Run Diagnostics currently chooses the notification from missing_permissions
alone. A failed connection or subscription must not produce a success banner.
Aggregate connection/permission/subscription outcomes and expose success,
warning, failure or unknown explicitly. Empty permission results remain
unknown, not denial. A user-token scope report does not prove page-token
operations are forbidden. Preserve results without raising an exception
that would roll back recorded findings. Handle permission-check failures
without displaying old permissions as freshly verified.

## Safety, cost and boundaries

Use native Odoo Community models, views, cron and mail.activity only; no new
paid module, AI API or SaaS dependency. Hosting/development costs remain.
Enforce manager checks on public actions, not only menu visibility.
All alert records have company rules. Any cron sudo use explicitly scopes
every query to the account/company. Store static messages and numbers;
remote errors use existing secret sanitization before exposure.

An Odoo cron cannot notify while Odoo itself is entirely stopped. Surface
stale monitor timestamps on reopening; host outage monitoring is separate.
WebSocket diagnosis is read-only separate infrastructure work and is not
fixed by adding this health screen.

Automatic Messenger resend is excluded: a timeout can follow successful
remote delivery. A later batch must distinguish definitive rejection from
uncertain delivery and avoid blind duplicate sends.

Other later batches: Inbox UI/quick replies and collision prevention,
safe attachment presentation, scheduled form discovery, Ads Insights
ingestion/CPL, Instagram/WhatsApp. Do not mark any of these complete here.

## Files and rollout

Expected changes: new models/meta_health.py and tests/test_meta_health.py;
account/page fields and controller receipt hooks; models/tests imports;
health views/menu, ACLs/rules, dedicated activity and cron data; manifest;
README, CURRENT_TASK and PROJECT_STATUS.

Choose the next unused module version after refreshing main. Additive
fields initialize as unknown; monitoring stays disabled until configured.
No deletion, user permission changes, inferred historical live timestamps,
or rewriting of messages/leads. Preserve current production comparison
and normal reviewed PR deployment process.

## Acceptance and verification

1. Signed live events update only their page/account; bad signatures,
   wrong account, historical imports and processing failures obey the
   receipt/success distinction.
2. Quiet traffic stays unknown/non-error by default; optional silence
   produces an explicitly uncertain warning after its threshold.
3. Due backlog respects retries scheduled in the future and is isolated
   from another account in the same company and other companies.
4. Repeated and concurrent monitor passes produce one incident/activity;
   acknowledge, recover and recur behave as defined; unrelated activities
   are preserved.
5. Diagnostics failures cannot produce a success notification or silently
   overwrite unknown permission evidence with an inferred denial.
6. Expiry and stale-check boundaries, disabled monitoring, invalid owner,
   archived pages and revoked recipient access have tests.
7. Manager/user and multi-company tests cover actions, counts and alerts.
   Secret markers in mocked exceptions never reach alerts or notification.
8. Run Odoo 19 installation and upgrade tests on a disposable database,
   including the existing connector suite, view validation and repository
   validation. Report actual counts/results rather than reusing Batch 2.
9. Deployment needs explicit approval. Post-deploy read-only verification
   precedes enabling alerts and the user-assisted live test.

## Remaining design gate

Review this design, then produce the implementation plan and execute its
test-first stages. This document itself is not evidence of implementation,
tests passing, deployment or a live server inspection.
