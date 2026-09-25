import importlib.util
import json
import os
import threading
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.modules.registry import Registry
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.crm_meta_lead_ads.models.meta_dedup import normalize_email, normalize_phone

QUEUE_LOGGER = 'odoo.addons.crm_meta_lead_ads.models.meta_lead_queue'
SECRETS = ('pagetok-DEDUP-SECRET', 'usertok-DEDUP-LEAK', 'appsecret-DEDUP-LEAK')


class TestMetaDedup(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.country_id = cls.env.ref('base.ae')
        cls.account = cls.env['meta.account'].create({
            'name': 'Dedup Meta', 'company_id': cls.env.company.id,
            'app_id': 'app', 'app_secret': 'secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Dedup Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '700',
            'page_access_token': 'pagetok-DEDUP-SECRET',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Dedup Form', 'company_id': cls.env.company.id,
            'page_id': cls.page.id, 'meta_form_id': '500',
        })
        cls.Queue = cls.env['meta.lead.queue']
        cls.Lead = cls.env['crm.lead']
        cls.Identity = cls.env['meta.lead.identity']
        cls.Log = cls.env['meta.lead.log']

    def _payload(self, leadgen_id, email=None, phone=None, name='Test Person'):
        field_data = []
        if name:
            field_data.append({'name': 'full_name', 'values': [name]})
        if email:
            field_data.append({'name': 'email', 'values': [email]})
        if phone:
            field_data.append({'name': 'phone_number', 'values': [phone]})
        return {
            'id': leadgen_id, 'created_time': '2026-09-24T10:00:00+0000',
            'ad_id': 'ad-1', 'ad_name': 'Ad One',
            'adset_id': 'adset-1', 'adset_name': 'Adset One',
            'campaign_id': 'camp-1', 'campaign_name': 'Campaign One',
            'form_id': '500', 'platform': 'facebook', 'is_organic': False,
            'field_data': field_data,
        }

    def _enqueue(self, leadgen_id):
        return self.Queue.enqueue_event(self.env.company, self.page, leadgen_id, '500', {'test': True})

    def _process(self, queue, payload):
        with patch.object(type(self.account), '_request', return_value=payload):
            queue.process_one()
        queue.invalidate_recordset()
        return queue

    def _make_lead(self, **vals):
        vals.setdefault('name', 'Existing Lead')
        return self.Lead.create(vals)

    # 1. Email normalization: outer spaces and mixed case.
    def test_email_normalization_case_and_spaces(self):
        self.assertEqual(normalize_email('  Person@Example.COM '), 'person@example.com')
        lead = self._make_lead(email_from='Person@Example.com')
        self.assertEqual(lead.meta_norm_email, 'person@example.com')
        queue = self._process(self._enqueue('dedup-e1'), self._payload('dedup-e1', email=' person@example.com '))
        self.assertEqual(queue.match_result, 'matched_email')
        self.assertEqual(queue.crm_lead_id, lead)

    # 2. Empty or invalid email is never a match key.
    def test_invalid_email_never_a_match_key(self):
        for bad in ('', 'not-an-email', 'a@b', 'a b@c.com', '@x.com', 'a@.com', 'a@x..com'):
            self.assertEqual(normalize_email(bad), '', bad)
        lead = self._make_lead(email_from='not-an-email')
        self.assertFalse(lead.meta_norm_email)
        queue = self._process(self._enqueue('dedup-e2'), self._payload('dedup-e2', email='not-an-email'))
        self.assertEqual(queue.match_result, 'created')
        self.assertNotEqual(queue.crm_lead_id, lead)

    # 3 + 7. The four UAE phone formats all canonicalize and match in-company.
    def test_uae_phone_formats_all_match(self):
        for fmt in ('+971501234567', '00971501234567', '971501234567', '050 123 4567'):
            self.assertEqual(normalize_phone(fmt, uae_context=True), '+971501234567', fmt)
        variants = ('+971 55 555 010%s', '0097155555010%s', '97155555010%s', '055555010%s')
        for i, variant in enumerate(variants):
            lead = self._make_lead(phone=variant % i)
            queue = self._process(
                self._enqueue('dedup-p%s' % i),
                self._payload('dedup-p%s' % i, phone='055 555 010%s' % i))
            self.assertEqual(queue.match_result, 'matched_phone', variant)
            self.assertEqual(queue.crm_lead_id, lead)

    # 4. A non-UAE international number is preserved, never UAE-guessed.
    def test_non_uae_number_never_uae_guessed(self):
        self.assertEqual(normalize_phone('+14155552671', uae_context=True), '+14155552671')
        self.assertEqual(normalize_phone('4155552671', uae_context=True), '4155552671')
        self.assertEqual(normalize_phone('0501234567', uae_context=False), '0501234567')
        self.assertEqual(normalize_phone('+44 20 7946 0958'), '+442079460958')
        lead = self._make_lead(phone='+1 (415) 555-2671')
        queue = self._process(self._enqueue('dedup-intl'), self._payload('dedup-intl', phone='0014155552671'))
        self.assertEqual(queue.match_result, 'matched_phone')
        self.assertEqual(queue.crm_lead_id, lead)

    # 5. Same Meta lead ID -> duplicate event, never a second lead.
    def test_duplicate_meta_lead_id_creates_nothing(self):
        lead = self._make_lead(meta_lead_id='dedup-dup2')
        queue = self._process(self._enqueue('dedup-dup2'), self._payload('dedup-dup2', email='x@example.com'))
        self.assertEqual(queue.match_result, 'duplicate_meta_id')
        self.assertEqual(queue.state, 'duplicate')
        self.assertEqual(queue.crm_lead_id, lead)
        self.assertEqual(self.Lead.search_count([('meta_lead_id', '=', 'dedup-dup2')]), 1)

    # 6. Exact normalized email match inside the same company.
    def test_email_match_within_company(self):
        lead = self._make_lead(email_from='same@example.com')
        queue = self._process(self._enqueue('dedup-c1'), self._payload('dedup-c1', email='same@example.com'))
        self.assertEqual(queue.match_result, 'matched_email')
        self.assertEqual(queue.crm_lead_id, lead)
        self.assertEqual(queue.state, 'done')

    # 8. No matching across companies; identity rows are company-isolated too.
    def test_no_cross_company_match(self):
        company_b = self.env['res.company'].create({'name': 'Dedup Co B'})
        lead_b = self.Lead.create({
            'name': 'B Lead', 'email_from': 'cross@example.com', 'company_id': company_b.id})
        queue = self._process(self._enqueue('dedup-x1'), self._payload('dedup-x1', email='cross@example.com'))
        self.assertEqual(queue.match_result, 'created')
        self.assertNotEqual(queue.crm_lead_id, lead_b)
        self.assertEqual(queue.crm_lead_id.company_id, self.env.company)

    def test_identity_company_record_rule(self):
        company_b = self.env['res.company'].create({'name': 'Identity Co B'})
        lead_a = self._make_lead(email_from='ida@example.com')
        lead_b = self.Lead.create({'name': 'B', 'email_from': 'idb@example.com', 'company_id': company_b.id})
        id_a = self.Identity.create({
            'company_id': self.env.company.id, 'crm_lead_id': lead_a.id,
            'meta_lead_id': 'ida-1', 'match_type': 'created'})
        id_b = self.Identity.create({
            'company_id': company_b.id, 'crm_lead_id': lead_b.id,
            'meta_lead_id': 'idb-1', 'match_type': 'created'})
        user = self.env['res.users'].create({
            'name': 'identity_user', 'login': 'identity_user_test',
            'company_id': self.env.company.id, 'company_ids': [(6, 0, [self.env.company.id])],
            'group_ids': [(6, 0, [self.env.ref('crm_meta_lead_ads.group_meta_lead_user').id])],
        })
        visible = self.Identity.with_user(user).search([('id', 'in', [id_a.id, id_b.id])])
        self.assertIn(id_a, visible)
        self.assertNotIn(id_b, visible)

    # 9. Email and phone pointing at different leads -> ambiguous, nothing linked.
    def test_conflicting_email_phone_is_ambiguous(self):
        self._make_lead(email_from='amb@example.com')
        self._make_lead(phone='050 999 8877')
        queue = self._process(
            self._enqueue('dedup-amb'),
            self._payload('dedup-amb', email='amb@example.com', phone='0509998877'))
        self.assertEqual(queue.state, 'ambiguous')
        self.assertEqual(queue.match_result, 'ambiguous')
        self.assertFalse(queue.crm_lead_id)
        self.assertIn('different CRM leads', queue.error_message or '')
        # The reason is stored without exposing customer contact data.
        self.assertNotIn('amb@example.com', queue.error_message or '')
        self.assertNotIn('0509998877', queue.error_message or '')
        self.assertNotIn('050 999 8877', queue.error_message or '')
        # The event is preserved, logged, and no identity was auto-created.
        self.assertTrue(queue.exists())
        self.assertFalse(self.Identity.search([('meta_lead_id', '=', 'dedup-amb')]))
        log = self.Log.search([('queue_id', '=', queue.id), ('action', '=', 'ambiguous')])
        self.assertTrue(log)
        self.assertEqual(log.level, 'warning')

    def test_multiple_candidates_same_channel_is_ambiguous(self):
        self._make_lead(email_from='multi@example.com')
        self._make_lead(email_from='Multi@example.com')
        queue = self._process(self._enqueue('dedup-amb2'), self._payload('dedup-amb2', email='multi@example.com'))
        self.assertEqual(queue.state, 'ambiguous')
        self.assertIn('several CRM leads', queue.error_message or '')
        self.assertFalse(queue.crm_lead_id)

    # 10. The contact name alone is never a match key.
    def test_name_alone_never_matches(self):
        lead = self._make_lead(contact_name='Unique Person')
        queue = self._process(self._enqueue('dedup-n1'), self._payload('dedup-n1', name='Unique Person'))
        self.assertEqual(queue.match_result, 'created')
        self.assertNotEqual(queue.crm_lead_id, lead)

    # 11. Matching never overwrites existing data or assignment.
    def test_matched_lead_data_never_overwritten(self):
        user = self.env['res.users'].create({'name': 'Sales Person', 'login': 'dedup_sales_test'})
        stage = self.env['crm.stage'].search([], limit=1)
        lead = self._make_lead(
            contact_name='Original Name', phone='050 111 2222',
            email_from='orig@example.com', user_id=user.id, stage_id=stage.id)
        queue = self._process(
            self._enqueue('dedup-k1'),
            self._payload('dedup-k1', email='orig@example.com', phone='0501112222', name='Other Name'))
        self.assertEqual(queue.match_result, 'matched_email')
        lead.invalidate_recordset()
        self.assertEqual(lead.contact_name, 'Original Name')
        self.assertEqual(lead.email_from, 'orig@example.com')
        self.assertEqual(lead.phone, '050 111 2222')
        self.assertEqual(lead.user_id, user)
        self.assertEqual(lead.stage_id, stage)
        self.assertTrue(lead.active)

    # 12. Only empty fields are filled on a matched lead.
    def test_matched_lead_empty_fields_filled(self):
        lead = self._make_lead(phone='050 333 4444')
        queue = self._process(
            self._enqueue('dedup-f1'),
            self._payload('dedup-f1', email='fill@example.com', phone='0503334444'))
        self.assertEqual(queue.match_result, 'matched_phone')
        lead.invalidate_recordset()
        self.assertEqual(lead.email_from, 'fill@example.com')
        self.assertEqual(lead.meta_campaign_id, 'camp-1')
        self.assertEqual(lead.meta_adset_id, 'adset-1')
        self.assertEqual(lead.meta_adgroup_id, 'adset-1')
        self.assertEqual(lead.meta_campaign_name, 'Campaign One')
        self.assertEqual(lead.meta_form_id, self.form)
        self.assertEqual(lead.meta_page_id, self.page)
        self.assertTrue(lead.source_id)

    # 13. Every Meta lead ID is kept in its own identity row.
    def test_multiple_meta_ids_linked_to_one_lead(self):
        q1 = self._process(self._enqueue('dedup-m1'), self._payload('dedup-m1', email='multi-id@example.com'))
        q2 = self._process(self._enqueue('dedup-m2'), self._payload('dedup-m2', email='multi-id@example.com'))
        self.assertEqual(q1.match_result, 'created')
        self.assertEqual(q2.match_result, 'matched_email')
        lead = q1.crm_lead_id
        self.assertEqual(q2.crm_lead_id, lead)
        self.assertEqual(lead.meta_lead_id, 'dedup-m1')
        identities = self.Identity.search([('crm_lead_id', '=', lead.id)])
        self.assertEqual(set(identities.mapped('meta_lead_id')), {'dedup-m1', 'dedup-m2'})
        self.assertEqual(set(identities.mapped('match_type')), {'created', 'matched_email'})
        # A matched event leaves a safe chatter note on the lead.
        notes = lead.message_ids.filtered(lambda m: 'matched by email' in (m.body or ''))
        self.assertTrue(notes)

    # 14. An archived lead is neither matched nor reactivated.
    def test_archived_lead_not_matched_not_reactivated(self):
        lead = self._make_lead(email_from='arch@example.com')
        lead.active = False
        queue = self._process(self._enqueue('dedup-a1'), self._payload('dedup-a1', email='arch@example.com'))
        self.assertEqual(queue.match_result, 'created')
        self.assertNotEqual(queue.crm_lead_id, lead)
        lead.invalidate_recordset()
        self.assertFalse(lead.active)

    # 15. Migration 19.0.3.2.0: batched normalization + identity backfill, idempotent.
    def _load_migration(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'migrations', '19.0.3.2.0', 'post-migration.py')
        spec = importlib.util.spec_from_file_location(
            'crm_meta_lead_ads_post_migration_19_0_3_2_0', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_migration_normalizes_and_links_idempotently(self):
        lead = self.Lead.create({
            'name': 'Legacy Meta', 'meta_lead_id': 'legacy-1',
            'email_from': 'Legacy@Example.com', 'phone': '050 777 8888',
        })
        # A Meta lead whose contact data is unusable: normalization yields
        # False, so a domain-retry loop would chase it forever. The
        # id-paginated migration must visit it once and terminate.
        bad = self.Lead.create({
            'name': 'Legacy Bad', 'meta_lead_id': 'legacy-bad',
            'email_from': 'not-an-email', 'phone': 'call me maybe',
        })
        # Simulate pre-1B rows: no normalized keys, no identity rows.
        lead.with_context(meta_norm_sync=True).write({'meta_norm_email': False, 'meta_norm_phone': False})
        module = self._load_migration()
        module.migrate(self.env.cr, '19.0.3.2.0')
        lead.invalidate_recordset()
        bad.invalidate_recordset()
        self.assertEqual(lead.meta_norm_email, 'legacy@example.com')
        self.assertEqual(lead.meta_norm_phone, '+971507778888')
        # Invalid contact data stays False and does not block anything.
        self.assertFalse(bad.meta_norm_email)
        self.assertFalse(bad.meta_norm_phone)
        identity = self.Identity.search([('meta_lead_id', '=', 'legacy-1')])
        self.assertEqual(len(identity), 1)
        self.assertEqual(identity.crm_lead_id, lead)
        self.assertEqual(identity.match_type, 'backfill')
        # Second full run: no-op, same values, no duplicate identity.
        module.migrate(self.env.cr, '19.0.3.2.0')
        lead.invalidate_recordset()
        self.assertEqual(lead.meta_norm_email, 'legacy@example.com')
        self.assertEqual(self.Identity.search_count([('meta_lead_id', '=', 'legacy-1')]), 1)
        # And the norm backfill reports zero changes on a stable dataset.
        _visited, changed, _batches = module._backfill_norm_fields(self.env)
        self.assertEqual(changed, 0)

    # 16. No tokens leak into error_message, meta.lead.log, or the logger.
    def test_processing_error_leaks_no_secrets(self):
        self.account.write({
            'user_access_token': 'usertok-DEDUP-LEAK', 'app_secret': 'appsecret-DEDUP-LEAK'})
        boom = UserError('boom pagetok-DEDUP-SECRET usertok-DEDUP-LEAK appsecret-DEDUP-LEAK')
        queue = self._enqueue('dedup-leak')
        with patch.object(type(queue), '_fetch_lead', side_effect=boom), \
                self.assertLogs(QUEUE_LOGGER, level='ERROR') as logs:
            queue.process_one()
        self.account.write({'user_access_token': False, 'app_secret': 'secret'})
        queue.invalidate_recordset()
        self.assertEqual(queue.state, 'retry')
        for secret in SECRETS:
            self.assertNotIn(secret, queue.error_message or '')
            self.assertNotIn(secret, '\n'.join(logs.output))
        for log in self.Log.search([('queue_id', '=', queue.id)]):
            for secret in SECRETS:
                self.assertNotIn(secret, log.message or '')

    # 17. Regression: the create path keeps full Batch 1A attribution.
    def test_created_lead_keeps_full_attribution(self):
        queue = self._process(self._enqueue('dedup-r1'), self._payload('dedup-r1', email='reg@example.com'))
        self.assertEqual(queue.match_result, 'created')
        lead = queue.crm_lead_id
        self.assertEqual(lead.meta_campaign_id, 'camp-1')
        self.assertEqual(lead.meta_campaign_name, 'Campaign One')
        self.assertEqual(lead.meta_adset_id, 'adset-1')
        self.assertEqual(lead.meta_adset_name, 'Adset One')
        self.assertEqual(lead.meta_ad_id, 'ad-1')
        self.assertEqual(lead.meta_ad_name, 'Ad One')
        self.assertEqual(lead.meta_adgroup_id, lead.meta_adset_id)
        identity = self.Identity.search([('meta_lead_id', '=', 'dedup-r1')])
        self.assertEqual(len(identity), 1)
        self.assertEqual(identity.match_type, 'created')

    # 18. Two SEQUENTIAL events for the same person produce one CRM lead.
    #     This is NOT the concurrency proof — it only covers the ordered
    #     cron path. Real cross-transaction concurrency is covered by
    #     TestMetaDedupConcurrency below.
    def test_two_sequential_events_same_person_single_lead(self):
        q1 = self._enqueue('dedup-t1')
        q2 = self._enqueue('dedup-t2')
        self._process(q1, self._payload('dedup-t1', email='race@example.com'))
        self._process(q2, self._payload('dedup-t2', email='race@example.com'))
        leads = self.Lead.search([('meta_norm_email', '=', 'race@example.com')])
        self.assertEqual(len(leads), 1)
        self.assertEqual(q1.match_result, 'created')
        self.assertEqual(q2.match_result, 'matched_email')
        self.assertEqual(self.Identity.search_count([('crm_lead_id', '=', leads.id)]), 2)

    # Race hardening: lock keys are deterministic and always ordered.
    def test_advisory_lock_keys_deterministic_and_ordered(self):
        queue = self._enqueue('dedup-lk1')
        keys = queue._dedup_lock_keys('lock@example.com', '+971501234567')
        self.assertEqual(keys, queue._dedup_lock_keys('lock@example.com', '+971501234567'))
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), 2)
        # Different values/companies produce different keys.
        self.assertNotEqual(keys, queue._dedup_lock_keys('other@example.com', '+971501234567'))
        self.assertNotEqual(keys, queue._dedup_lock_keys('lock@example.com', '+971509999999'))
        self.assertTrue(all(isinstance(k, int) for k in keys))
        # No keys, no locks.
        self.assertEqual(queue._dedup_lock_keys('', ''), [])

    # Audit hardening: meta.lead.log.payload_json carries no PII.
    def test_audit_log_payload_has_no_pii(self):
        queue = self._process(
            self._enqueue('dedup-pii'),
            self._payload('dedup-pii', email='pii@example.com', phone='050 123 4567', name='Private Person'))
        log = self.Log.search([('queue_id', '=', queue.id), ('action', '=', 'created')], limit=1)
        self.assertTrue(log)
        safe = log.payload_json or {}
        self.assertNotIn('field_data', safe)
        blob = json.dumps(safe)
        self.assertNotIn('pii@example.com', blob)
        self.assertNotIn('050 123 4567', blob)
        self.assertNotIn('0501234567', blob)
        self.assertNotIn('Private Person', blob)
        # Technical metadata is kept for traceability.
        self.assertEqual(safe.get('id'), 'dedup-pii')
        self.assertEqual(safe.get('campaign_id'), 'camp-1')
        self.assertEqual(safe.get('adset_id'), 'adset-1')
        self.assertEqual(safe.get('platform'), 'facebook')
        # The full payload still lives in the protected queue field.
        self.assertIn('field_data', queue.fetched_payload)

    # Company isolation: a Meta lead ID owned by another company is
    # never linked; the event is parked as ambiguous for manual review.
    def test_cross_company_meta_lead_id_never_linked(self):
        company_b = self.env['res.company'].create({'name': 'Dedup Co CC'})
        lead_b = self.Lead.create({
            'name': 'B Meta Lead', 'meta_lead_id': 'dedup-cc-x', 'company_id': company_b.id})
        queue = self._process(
            self._enqueue('dedup-cc-x'), self._payload('dedup-cc-x', email='cc-x@example.com'))
        self.assertEqual(queue.state, 'ambiguous')
        self.assertEqual(queue.match_result, 'ambiguous')
        self.assertFalse(queue.crm_lead_id)
        self.assertNotEqual(queue.crm_lead_id, lead_b)
        self.assertIn('another company', queue.error_message or '')
        self.assertFalse(self.Identity.search([('meta_lead_id', '=', 'dedup-cc-x')]))
        # The other company's lead was not touched.
        lead_b.invalidate_recordset()
        self.assertEqual(lead_b.company_id, company_b)

    # Identity hardening: an ID already linked to a DIFFERENT lead is a
    # conflict that must surface — never swallowed, never re-linked.
    def test_identity_conflict_raises_not_swallowed(self):
        lead1 = self._make_lead(email_from='idc-1@example.com')
        lead2 = self._make_lead(email_from='idc-2@example.com')
        self.Identity.create({
            'company_id': self.env.company.id, 'crm_lead_id': lead1.id,
            'meta_lead_id': 'dedup-idc', 'match_type': 'created'})
        queue = self._enqueue('dedup-idc')
        with self.assertRaises(UserError):
            queue._ensure_identity(lead2, 'matched_email')
        # Same lead is accepted (idempotent).
        self.assertTrue(queue._ensure_identity(lead1, 'duplicate_meta_id'))


class TestMetaDedupConcurrency(TransactionCase):
    """REAL two-transaction concurrency proof for the dedup advisory
    lock. The fixture is created and committed through an INDEPENDENT
    cursor (self.env.cr is never committed, so the TransactionCase
    savepoint stays intact), two worker threads process the two events
    in genuinely overlapping transactions, and everything is removed
    through an independent cursor in `finally`.

    Requires a live PostgreSQL — like every Odoo test in this project
    it could NOT be executed in the local static environment; it is
    written for a disposable CI/staging database."""

    EVENT_TIMEOUT = 30       # seconds; a healthy run finishes in < 2s
    BLOCK_PROOF_SECONDS = 3  # B must stay blocked at least this long

    def _payload(self, leadgen_id, email):
        return {
            'id': leadgen_id, 'created_time': '2026-09-24T10:00:00+0000',
            'form_id': 'CC', 'platform': 'facebook', 'is_organic': False,
            'field_data': [
                {'name': 'full_name', 'values': ['Concurrent Person']},
                {'name': 'email', 'values': [email]},
            ],
        }

    def _fixture(self, env):
        company = env['res.company'].create({
            'name': 'Conc Co', 'country_id': env.ref('base.ae').id})
        account = env['meta.account'].create({
            'name': 'Conc Meta', 'company_id': company.id,
            'app_id': 'app-cc', 'app_secret': 'secret',
        })
        page = env['meta.page'].create({
            'name': 'Conc Page', 'company_id': company.id,
            'account_id': account.id, 'meta_page_id': 'CC-P',
            'page_access_token': 'pagetok-CC',
        })
        form = env['meta.form'].create({
            'name': 'Conc Form', 'company_id': company.id,
            'page_id': page.id, 'meta_form_id': 'CC',
        })
        q_a = env['meta.lead.queue'].enqueue_event(company, page, 'cc-a', 'CC', {})
        q_b = env['meta.lead.queue'].enqueue_event(company, page, 'cc-b', 'CC', {})
        return {
            'company': company.id, 'account': account.id, 'page': page.id,
            'form': form.id, 'q_a': q_a.id, 'q_b': q_b.id,
        }

    def _worker(self, registry, queue_id, key, gates, results):
        """Process one queue event in its own thread + transaction.
        Worker A additionally parks its OPEN transaction (holding the
        advisory lock) until the main thread releases it. Any error is
        captured and re-raised by the test thread — no silent failure."""
        try:
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                queue = env['meta.lead.queue'].browse(queue_id)
                queue.process_one()
                results[key] = queue.match_result
                gates['%s_processed' % key].set()
                if key == 'a':
                    # The timeout guarantees CI can never hang here even
                    # if the main thread died before releasing us.
                    gates['commit_a'].wait(timeout=self.EVENT_TIMEOUT)
                cr.commit()
                gates['%s_committed' % key].set()
        except Exception as exc:
            results['%s_error' % key] = exc
        finally:
            gates['%s_done' % key].set()

    def test_concurrent_processing_overlapping_transactions(self):
        dbname = self.env.cr.dbname
        registry = Registry(dbname)
        gates = {name: threading.Event() for name in (
            'a_processed', 'a_committed', 'a_done', 'commit_a',
            'b_processed', 'b_committed', 'b_done')}
        results = {}
        thread_a = thread_b = None
        # Fixture via an independent, committed transaction.
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            ids = self._fixture(env)
            cr.commit()
        try:
            payloads = {'cc-a': self._payload('cc-a', 'conc@example.com'),
                        'cc-b': self._payload('cc-b', 'conc@example.com')}

            def fake_request(method, path, **kwargs):
                if path in payloads:
                    return payloads[path]
                raise AssertionError('unexpected Graph path %s' % path)

            with patch.object(type(self.env['meta.account']), '_request',
                              side_effect=fake_request):
                thread_a = threading.Thread(
                    target=self._worker, name='dedup-tx-A',
                    args=(registry, ids['q_a'], 'a', gates, results))
                thread_b = threading.Thread(
                    target=self._worker, name='dedup-tx-B',
                    args=(registry, ids['q_b'], 'b', gates, results))
                thread_a.start()
                self.assertTrue(
                    gates['a_processed'].wait(timeout=self.EVENT_TIMEOUT),
                    'transaction A never processed its event: %r' % results)
                # --- THE OVERLAP ---
                # A has created the lead and still holds the advisory
                # lock inside its OPEN transaction. B starts only now;
                # its pg_advisory_xact_lock call must block.
                thread_b.start()
                self.assertFalse(
                    gates['b_processed'].wait(timeout=self.BLOCK_PROOF_SECONDS),
                    'B completed while A held the advisory lock — the lock '
                    'did not serialize the two transactions')
                # Release A; B must now unblock, re-search, and match.
                gates['commit_a'].set()
                self.assertTrue(
                    gates['a_committed'].wait(timeout=self.EVENT_TIMEOUT),
                    'transaction A never committed')
                self.assertTrue(
                    gates['b_processed'].wait(timeout=self.EVENT_TIMEOUT),
                    'transaction B stayed blocked after A released the lock')
                thread_a.join(timeout=self.EVENT_TIMEOUT)
                thread_b.join(timeout=self.EVENT_TIMEOUT)
            self.assertFalse(thread_a.is_alive() or thread_b.is_alive(),
                             'worker thread(s) hung past their timeouts')
            for key in ('a', 'b'):
                error = results.get('%s_error' % key)
                if error is not None:
                    raise AssertionError('transaction %s failed: %r' % (key, error))
            self.assertEqual(results.get('a'), 'created')
            self.assertIn(results.get('b'), ('matched_email', 'matched_phone'))
            # Final state, verified through a fresh read-only transaction.
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                leads = env['crm.lead'].search([
                    ('meta_norm_email', '=', 'conc@example.com'),
                    ('company_id', '=', ids['company'])])
                self.assertEqual(len(leads), 1)
                identities = env['meta.lead.identity'].search([
                    ('meta_lead_id', 'in', ['cc-a', 'cc-b'])])
                self.assertEqual(len(identities), 2)
                self.assertEqual(
                    set(identities.mapped('crm_lead_id').ids), set(leads.ids))
                queues = env['meta.lead.queue'].search([
                    ('meta_lead_id', 'in', ['cc-a', 'cc-b'])])
                self.assertFalse(queues.filtered(
                    lambda q: q.state in ('ambiguous', 'failed')))
        finally:
            gates['commit_a'].set()  # never leave worker A parked
            for thread in (thread_a, thread_b):
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=self.EVENT_TIMEOUT)
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                leads = env['crm.lead'].search([
                    ('meta_norm_email', '=', 'conc@example.com'),
                    ('company_id', '=', ids['company'])])
                env['meta.lead.identity'].search([
                    ('meta_lead_id', 'in', ['cc-a', 'cc-b'])]).unlink()
                env['meta.lead.log'].search([
                    ('meta_lead_id', 'in', ['cc-a', 'cc-b'])]).unlink()
                env['meta.lead.queue'].search([
                    ('meta_lead_id', 'in', ['cc-a', 'cc-b'])]).unlink()
                leads.unlink()
                env['meta.form'].browse(ids['form']).unlink()
                env['meta.page'].browse(ids['page']).unlink()
                env['meta.account'].browse(ids['account']).unlink()
                env['res.company'].browse(ids['company']).unlink()
                cr.commit()
