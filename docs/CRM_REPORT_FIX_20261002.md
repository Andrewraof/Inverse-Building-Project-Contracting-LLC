# CRM visibility and export repair — 2026-10-02

## Original customer requirements

1. Converted leads must remain findable as opportunities.
2. Export the assigned salesperson, not the creator.
3. Include Notes and activity details in readable exports.
4. Export the actual lead Source.

## Evidence and decisions

- Baseline: origin/main `47574a5`; isolated branch `crm-report-fix-20261002`.
- Standard Odoo 19 Leads action excludes `type=opportunity`; Pipeline has a
  separate opportunity domain and may apply Assigned to Me. This is not evidence
  of deleted records. We do not change standard actions or record rules.
- Odoo distinguishes `user_id` from `create_uid`, and UTM `source_id` from
  Meta page/form. A saved export template selects the correct fields explicitly.
  Existing user-created export templates are not overwritten.
- Notes tab (`description`) and internal chatter notes are different data.
  The new Notes column combines both, using caller-visible messages only.
- Completed activity messages retain the rendered summary and feedback.
  Open activities explicitly require active=True, even with active_test=False.
- No sudo in new compute code; computations keyed by caller uid and company scope, parent access
  checked, batched message/activity queries, no changes to CRM ACLs or assignment.
- Ruling: the shared export template omits Meta page/form relation subfields,
  because ordinary CRM sales users may lack Meta model ACLs. Actual Source,
  Meta platform and campaign/ad attribution text remain included.
- Ruling: do not invent missing salesperson/source/history or rewrite old data.

## Changes

- CRM > Sales > Meta Leads & Opportunities: one list across both record types,
  no implicit assignee filter. Archived/lost records remain opt-in.
- Three non-stored readable export columns: Notes, Open Activities, Completed
  Activities. Notes preserve original descriptions and internal notes.
- Shared normal Odoo export template: CRM Leads & Opportunities — Salesperson,
  Source, Notes & Activities. It works from either normal CRM or the new list.
- Version `19.0.4.2.0`; no migration/backfill required and no new dependencies.

## Verification ledger

- Clean baseline local suite: 14/14 passed.
- Red CI `37000338015` on `e4009ee`: 9 failures + 1 fixture error / 229 tests.
  Failures prove the missing action/template/export fields. The fixture error
  attempted to create another salesperson's lead as an own-leads-only seller.
  Fixed fixture: create own record then reassign as administrator; retain creator.
- CI `37000820343`: 1 failure / 6 errors. Fixed the unsupported active_test
  context dependency on a Text field. Fixtures now grant the ordinary export
  permission and use Markup for real HTML chatter (plain strings are escaped).
- CI `37001153992`: 230/230 passed, but the new cache test was inconclusive:
  Odoo's assertRaises(AccessError) cleared the cache before evaluating it.
- Cache reproduction CI `37001434171` on `4ce3d31`: all three subtests failed
  with AccessError not raised, proving the cross-company cache reuse finding.
  Fix: company + allowed_company_ids + lang context keys on all three fields.
  The regression uses stdlib assertRaises to preserve the existing cache.
- Independent review: company cache isolation was the sole important finding;
  fixed after the reproducing test. No CRM ACL/rule changes.
- Final CI + double prior-release upgrade: pending. Production deployment: not performed.

## Limits / delivery

No guarantee that previously deleted notes can be recovered. No invented source
for old blank records. Standard own-record/company rules still apply; this does
not grant access to other salespeople's leads. New menu is available to normal
CRM sales users, not restricted to Meta administrators. Current-user export of
hidden records remains forbidden. No customer/Meta data used in fixtures.

ملخص: قائمة موحّدة للّيدز والفرص، وتصدير البائع المسؤول والمصدر الفعلي
والملاحظات والأنشطة، دون تغيير صلاحيات المستخدمين أو البيانات التاريخية.
لا يُعلن نجاح الإنتاج قبل الاختبارات والنشر والتحقق الفعلي.
