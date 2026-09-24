# Meta Inbox Conversations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the approved Meta Inbox so Messenger messages are grouped into conversations, can be replied to inside Odoo, linked to CRM, and managed with standard Odoo Community views.

**Architecture:** Port the reviewed phase-one data model from the older Downloads checkout onto the current repository head, then build each remaining behavior behind model methods with thin webhook/view integrations. Preserve webhook idempotency, isolate each messaging event with a database savepoint, and route all Graph API failures through existing secret sanitization.

**Tech Stack:** Odoo 19 Community, Python, PostgreSQL ORM, XML views/security/data, Meta Graph API, Odoo TransactionCase/HttpCase tests.

**Spec:** `docs/META_INBOX_DESIGN.md` (approved design currently present in the Downloads checkout and copied into this repository before implementation)

## Global Constraints

- Odoo 19 Community only; no Enterprise, paid modules, JavaScript chat UI, SaaS, WhatsApp, or Instagram messaging.
- Never store tokens, app secrets, passwords, or unsanitized Graph API errors in logs, chatter, activities, or test fixtures.
- `meta.message.conversation_id` remains optional during this release.
- Attachments are metadata and external URLs only; never download or server-fetch them.
- Do not push, deploy, or modify production unless the user explicitly requests it after tests pass.

## Review Focus

- A duplicate webhook delivery must not create a second message or increment unread twice.
- The same PSID on two pages must create two separate conversations.
- A failing event/page must roll back only its own savepoint and allow sibling events to finish.
- A reply outside 24 hours must be rejected before calling Meta; Meta errors must remain the final authority inside the window.
- Legacy migration reruns must reuse an existing conversation and never violate `(page_id, psid)` uniqueness.

---

### Task 1: Rebase the completed phase-one work onto the current checkout

**Files:**
- Create: `docs/META_INBOX_DESIGN.md`
- Create: `crm_meta_lead_ads/models/meta_conversation.py`
- Create: `crm_meta_lead_ads/migrations/19.0.2.0.0/post-migration.py`
- Create: `crm_meta_lead_ads/tests/test_meta_inbox.py`
- Modify: phase-one manifest, model imports/fields, settings view, security files, and test imports listed in the approved summary.

**Interfaces:**
- Produces: `meta.conversation._get_or_create(page, psid, values=None)` and `_link_legacy_messages()`.

- [x] Copy the approved spec and phase-one changes from the Downloads checkout using patches, preserving current commit `4c450d8`.
- [x] Add a regression test where a conversation already exists and orphan legacy messages for the same `(page_id, psid)` are migrated.
- [x] Fix `_link_legacy_messages()` to call `_get_or_create` so repeated/partial migrations are idempotent.
- [x] Run `python -m py_compile` on changed Python files and `git diff --check`; expect success.
- [x] Run the available addon validation and Odoo tests; record any environment limitation exactly.

### Task 2: Integrate conversations into inbound webhook processing

**Files:**
- Modify: `crm_meta_lead_ads/controllers/main.py`
- Modify: `crm_meta_lead_ads/models/meta_conversation.py`
- Test: `crm_meta_lead_ads/tests/test_meta_inbox.py`

**Interfaces:**
- Consumes: `_get_or_create(page, psid, values=None)`.
- Produces: `meta.conversation._record_inbound_message(event)` and `_schedule_inbox_activity()`.

- [x] Add tests for first inbound message, repeated message id, closed-conversation reopening, attachment-only input, unread increments, and activity deduplication.
- [x] Implement one savepoint per messaging event and resolve the page by recipient/page id and company.
- [x] Upsert the conversation, create the message only when its Meta message id is new, update preview/time/unread, and store attachment metadata without fetching URLs.
- [x] Schedule at most one open activity for `assigned_user_id`, otherwise the configured default user, otherwise nobody.
- [ ] Run the focused tests; expect all inbound tests to pass.

Static validation completed on 2026-09-24 (`py_compile`, addon validator, and
`git diff --check`). The focused Odoo runtime tests remain deliberately open
because this workstation has no Odoo runtime or Docker; they must run in CI
before Task 2 is considered fully verified.

### Task 3: Add outbound replies and the 24-hour guard

**Files:**
- Modify: `crm_meta_lead_ads/models/meta_conversation.py`
- Modify: `crm_meta_lead_ads/models/meta_message.py`
- Test: `crm_meta_lead_ads/tests/test_meta_inbox.py`

**Interfaces:**
- Produces: `action_send_reply()` and helper `_send_reply(text)` using the existing Meta account request wrapper.

- [x] Add failing tests for empty reply, disconnected account, missing page token, closed conversation, no inbound message, expired 24-hour window, successful send, and sanitized API failure.
- [x] Validate eligibility before the API call and POST `/me/messages` with `messaging_type=RESPONSE` and the conversation PSID.
- [x] On success create one outbound `meta.message`, update preview/time, reset unread, and complete the open inbox activity.
- [x] On failure create a failed outbound message with sanitized reason and raise a user-facing error without leaking secrets.
- [ ] Run focused reply and secret-leak tests; expect pass. (blocked: no local Odoo/Docker — runs in CI after push)

### Task 4: Build the standard Odoo Inbox interface

**Files:**
- Create: `crm_meta_lead_ads/views/meta_conversation_views.xml`
- Modify: `crm_meta_lead_ads/views/meta_menus.xml`
- Modify: `crm_meta_lead_ads/__manifest__.py`
- Test: `crm_meta_lead_ads/tests/test_meta_inbox.py`

**Interfaces:**
- Consumes: conversation actions from Tasks 2–3.

- [ ] Add view-loading tests that resolve the Inbox action, list, form, and search views.
- [ ] Add list columns for sender, preview, time, unread, assignee, state, page, and channel.
- [ ] Add form buttons for assign, mark read, send reply, close/reopen, create lead, and open linked lead; render messages readonly in chronological order.
- [ ] Add page/assignee/state/unread/channel filters and Arabic-compatible labels using standard Odoo layout only.
- [ ] Upgrade the module in a disposable/test database and confirm all views load without XML errors.

### Task 5: Complete CRM and UTM integration

**Files:**
- Modify: `crm_meta_lead_ads/models/meta_conversation.py`
- Modify: `crm_meta_lead_ads/models/crm_lead.py`
- Modify: `crm_meta_lead_ads/views/crm_lead_views.xml`
- Modify/Create: `crm_meta_lead_ads/data/utm_data.xml`
- Test: `crm_meta_lead_ads/tests/test_meta_inbox.py`

**Interfaces:**
- Produces: `action_create_lead()`, `action_open_lead()`, and bidirectional conversation/lead navigation.

- [ ] Add failing tests for lead creation, Meta Messenger UTM source, duplicate prevention, multi-company isolation, and opening an existing link.
- [ ] Create the lead from sender/PSID, assign the conversation user, link both records, and post only a metadata summary to chatter.
- [ ] Prevent a second lead while `lead_id` exists and expose smart navigation on both records.
- [ ] Run focused CRM tests; expect pass.

### Task 6: Finish migration, documentation, and whole-module verification

**Files:**
- Modify: `crm_meta_lead_ads/migrations/19.0.2.0.0/post-migration.py`
- Modify: `crm_meta_lead_ads/README.md`
- Modify: `docs/PROJECT_STATUS.md`
- Modify: `docs/CURRENT_TASK.md`
- Test: all addon tests.

**Interfaces:**
- Consumes: all previous tasks.

- [ ] Add migration coverage for mixed companies, pages, archived conversations, partial prior migration, and repeated execution.
- [ ] Confirm the migration never deletes or changes original message content and creates no duplicate conversation.
- [ ] Update README and project status with setup, permissions, 24-hour restriction, free-cost scope, and operational test steps.
- [ ] Run `py_compile`, XML parsing, `git diff --check`, addon validator, and all Odoo tests; capture exact results.
- [ ] Review the complete diff for secrets, unrelated changes, and backward compatibility; do not commit/push/deploy until separately authorized.
