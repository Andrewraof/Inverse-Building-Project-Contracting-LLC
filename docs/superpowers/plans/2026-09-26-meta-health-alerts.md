# Meta Connector Health and Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Meta managers a company-safe health screen and deduplicated internal alerts that distinguish authenticated live webhook receipt from processing and historical imports.

**Architecture:** Extend the existing `meta.account` and `meta.page` records with local health evidence; keep incident lifecycle in one new `meta.health.alert` model. An opt-in five-minute cron assesses stored evidence and queue/message aggregates, while the existing Diagnostics action remains the only remote check in this batch.

**Tech Stack:** Odoo 19 Community ORM, PostgreSQL 15, XML views/security/data, `mail.activity`, Python Odoo tests.

**Spec:** `docs/BATCH3_HEALTH_DESIGN.md` (approved in commit `dd1a7a2`).

## Global Constraints

- No new paid module, AI API, SaaS service, Graph polling in the health cron, or automatic Messenger resend.
- No production database tests or deployment without a separate explicit request; pushing `main` deploys automatically.
- No tokens, PSIDs, message bodies, customer contact details, or raw Graph responses in health views, alerts, logs, or activity notes.
- Scope all reads and writes by account, page and company, including every `sudo()` query; preserve record rules and manager checks on actions.
- Leave monitoring disabled on upgrade, and do not infer historical live webhook receipt from `received_at`.
- Refresh `origin/main` and review worktree state before implementation; the local design branch was based on `47574a5` when inspected.

## Review Focus

1. A valid signature for a page belonging to another account must not refresh the requested account's live-receipt timestamp; Task 1 tests this.
2. A Graph response with no permission data must remain unknown, not report denied access or success; Task 2 tests this.
3. Future-dated retries must not enter overdue backlog while stuck `processing` jobs must; Task 3 tests both.
4. Parallel cron passes must not create duplicate incidents or activities; Task 4 tests a real unique-key conflict and repeated passes.
5. A disabled or quiet account must not be labeled disconnected; Task 3 tests disabled, silence-disabled, and silence-enabled cases.

---

### Task 0: Prepare the isolated branch and test harness

**Files:** No product files; inspect `.github/workflows/odoo-meta-tests.yml` and existing worktrees.

**Interfaces:** Establish the current base revision and the disposable Odoo 19 test command used by later tasks.

- [ ] **Step 1: Refresh the base.** Run `git fetch origin main`, inspect `git worktree list`, `git status --short --branch`, and compare the design branch to `origin/main`. Preserve existing worktrees and changes; rebase/cherry-pick the approved design onto an isolated branch if `main` moved.
- [ ] **Step 2: Baseline.** Run `python -m unittest discover -s tests` and `python deploy/validate_addon.py crm_meta_lead_ads`. Run the full Odoo test workflow on a disposable database if Docker/CI is available; record the exact baseline result, not a historical count. Stop and diagnose any baseline failure before attributing later failures to this batch.

### Task 1: Record authenticated live webhook evidence

**Files:** Modify `crm_meta_lead_ads/models/meta_page.py`, `crm_meta_lead_ads/controllers/main.py`; create/modify `crm_meta_lead_ads/tests/test_meta_health.py` and `tests/__init__.py`.

**Interfaces:** `meta.page._mark_live_webhook_received(kind: str, at: datetime)` records receipt; page fields `last_live_webhook_at`, `last_live_lead_webhook_at`, `last_live_message_webhook_at` are nullable. Processing-success fields `last_live_lead_enqueued_at`, `last_live_message_recorded_at` advance only on successful enqueue/record.

- [ ] **Step 1: Write failing Odoo tests** for valid signed lead and message events, invalid signature, unknown recipient, wrong account-specific webhook key, duplicate event, and handler failure. Assert receipt may advance despite processing failure, but success cannot; historical sync never advances either. Name the production methods whose removal makes each test fail.
- [ ] **Step 2: Run the targeted Odoo 19 tests on a disposable database and verify the expected failures.** Use the workflow's Docker/Postgres procedure; do not run on `inverse_elite`.
- [ ] **Step 3: Implement the minimal model fields and controller hooks.** Mark receipt only after HMAC validation and an account-scoped page match. Mark success after the existing queue/message operation succeeds. Keep existing webhook response and idempotency behavior.
- [ ] **Step 4: Rerun the targeted tests and connector suite; commit this task after green verification.**

### Task 2: Make Diagnostics outcomes truthful

**Files:** Modify `crm_meta_lead_ads/models/meta_account.py`, `crm_meta_lead_ads/views/meta_account_views.xml`; test in `tests/test_meta_health.py`.

**Interfaces:** Stored `diagnostic_status` (`unknown`, `success`, `warning`, `failure`) and `diagnostic_checked_at`; `action_run_diagnostics()` returns a notification consistent with connection, permission and active-page subscription evidence.

- [ ] **Step 1: Write failing tests** for connection failure with no missing scopes, failed/incomplete subscription, stale prior scopes after a permission-check exception, an empty permission response, and a successful full check. Assert the records persist their findings without throwing or leaking mock secret markers.
- [ ] **Step 2: Run targeted tests and observe expected failures.**
- [ ] **Step 3: Implement outcome aggregation and safe persistence.** Unknown permission evidence remains unknown; a user-token scope list is not treated as definitive denial of page-token operations. Use the existing sanitization helpers.
- [ ] **Step 4: Run targeted and full connector tests; commit after green verification.**

### Task 3: Compute local health by account and page

**Files:** Modify `crm_meta_lead_ads/models/meta_account.py`; create `crm_meta_lead_ads/models/meta_health.py`; test in `tests/test_meta_health.py`.

**Interfaces:** Account settings `health_monitor_enabled` (default `False`), `health_owner_id`, `health_due_minutes` (default 15), `health_silence_minutes` (disabled by default), `health_token_warning_days` (default 7), `health_enabled_at`, `health_checked_at`; `meta.account._health_snapshot(now)` returns scoped numeric metrics and static issue codes, never raw records/PII.

- [ ] **Step 1: Write failing tests** for two accounts in one company and a third in another, overdue pending/retry/processing jobs, future retries, failed/ambiguous jobs, recent failed outbound, archived/disabled pages, token expiry known/unknown, stale diagnostics, quiet traffic by default, and optional silence measured from enablement.
- [ ] **Step 2: Run targeted tests and observe expected failures.**
- [ ] **Step 3: Implement bounded aggregate queries.** Scope every aggregate by account/page/company, return only counts/durations/codes, and classify Disabled/Unknown/No detected issues/Warning/Error without claiming a quiet page is disconnected.
- [ ] **Step 4: Validate the owner on settings changes.** Require an active internal Meta manager allowed in the account's company; preserve an alert but suppress notification if the owner later loses access.
- [ ] **Step 5: Run targeted and full connector tests; commit after green verification.**

### Task 4: Deduplicated incident and activity lifecycle

**Files:** Complete `models/meta_health.py`; modify `models/__init__.py`, `security/meta_security.xml`, `security/ir.model.access.csv`, `data/ir_cron_data.xml`, `crm_meta_lead_ads/__manifest__.py`; test in `tests/test_meta_health.py`.

**Interfaces:** `meta.health.alert` keyed uniquely by `(account_id, page_id, check_code)` with `open/acknowledged/resolved`, first/last seen and resolved timestamps and numeric summary; `meta.account._cron_assess_health(limit=...)` uses bounded account traversal and per-account savepoints. Actions `action_acknowledge()` and `action_resolve()` enforce Meta manager access.

- [ ] **Step 1: Write failing tests** for create/reuse/acknowledge/recover/recur, two simultaneous transactions hitting the same key, one dedicated activity per episode, no duplicate chatter or reminder, no external followers, owner access revocation, cross-company denial and preserving unrelated To-Dos.
- [ ] **Step 2: Run targeted tests and observe expected failures.**
- [ ] **Step 3: Implement the alert model and transaction-safe upsert.** Use a database uniqueness constraint and a savepoint conflict path; recheck the incident after conflict. Never swallow unrelated `IntegrityError`.
- [ ] **Step 4: Add the five-minute cron and dedicated activity type.** Keep the cron local-only and opt-in, bound each pass and isolate account failures; activity completion targets only the incident's own activity.
- [ ] **Step 5: Run targeted and full connector tests; commit after green verification.**

### Task 5: Health screen, scoped drill-down and rollout

**Files:** Create `crm_meta_lead_ads/views/meta_health_views.xml`; modify `views/meta_menus.xml`, `views/meta_account_views.xml`, `__manifest__.py`, `README.md`, `docs/CURRENT_TASK.md`, `docs/PROJECT_STATUS.md`; test in `tests/test_meta_health.py`.

**Interfaces:** CRM > Meta Lead Ads > Health is manager-only. Opening list/form uses local fields only; `Run Diagnostics` is the explicit remote action. Drill-down action domains retain account/page/company and normal rules.

- [ ] **Step 1: Write failing tests** for menu/action manager restriction, readable local metrics without Graph calls, drill-down domains, and no sensitive fields rendered. Check the form/list XML against Odoo 19 RNG.
- [ ] **Step 2: Run targeted tests and observe expected failures.**
- [ ] **Step 3: Implement the views and scoped actions; register data in manifest.** Include wording that subscription checks do not prove end-to-end delivery, and show the last assessment time.
- [ ] **Step 4: Document opt-in behavior, unknown initial state, operational limits, and no production test claims.** Keep the deployment process separate from implementation.
- [ ] **Step 5: Run the full Odoo 19 install/upgrade suite on a disposable database, repository tests, validator, XML checks and `git diff --check`; inspect the whole diff for PII/secrets. Commit only after fresh evidence.**
- [ ] **Step 6: Open a review PR, attach it to this task and stop before any `main` push/deployment.** Report exact test counts and residual risks. Production enabling and live webhook testing require a later authorized step.
