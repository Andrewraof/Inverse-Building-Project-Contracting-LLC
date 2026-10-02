# New Meta enquiries: management review first

Requested 2026-10-02. Scope: future Lead Ads enquiries; existing CRM records
and historical enquiries stay on their existing path. Messenger is unchanged.

Upgrade to 19.0.4.3.0 records a UTC cutover once in
`crm_meta_lead_ads.contact_review_from`. New queue events are private from
creation; after fetching, Meta created_time distinguishes historical enquiries.
Missing/invalid dates are conservatively held for management. Old queue rows
and existing leads/contacts are not backfilled or altered. Fresh installations
must configure this parameter to their desired UTC start time; the migration
automatically activates it for this production upgrade.

New enquiries create an unassigned res.partner pending review, no CRM lead,
sales activity or routing. Global company-aware rules hide pending/rejected
contacts and their queue/audit data from non-reviewers, including Meta managers
without the dedicated review group. Normal contacts stay visible under normal
rules. Each enquiry gets an isolated contact even when email matches an existing
customer; management can resolve that relationship later without hiding the
existing customer. Redeliveries of the same Meta ID reuse the same contact.

Management opens Meta Lead Ads > Contacts — Management Review, chooses a
salesperson, then Approve and Create Lead or Reject. Approval executes CRM create
with the reviewer's real rights (including the separate lead-creator restriction).
It retains answers, source and ad attribution, and releases the contact only
after successful lead creation in the same transaction. Repeated approval is a
no-op. Rejected contacts remain private. No automatic sales routing overrides
the management selection.

The upgrade assigns the review group to already-designated lead creators from
inverse_crm_lead_control when installed, otherwise the known active Hafez/Michael
logins. System administrators may also review. No other sales groups are upgraded.

Verification: CI run 37011472613 on 37953e0 passed 239/239 Odoo tests and
both upgrade passes (CONTACT_REVIEW_UPGRADE_OK); local repo tests 14/14
and addon validator passed. The first run exposed system-only raw audit field
permissions; corrected by non-sudo CRM creation followed by a narrow audit-only
write. Independent read-only review found no remaining blocker.
Dedicated Odoo tests cover intake, restricted reads/approvals,
company isolation, replay, failed approval, rejection, original records and
historical dates. Disposable upgrade fixture runs twice and verifies the stable
cutover and contact-only processing. Actual results are recorded after CI.

Deployment through reviewed GitHub Actions only. Rollback of code must not
remove the privacy rules while pending contacts exist. Use a forward fix or
retain this privacy layer; a blanket downgrade can expose pending contacts.
