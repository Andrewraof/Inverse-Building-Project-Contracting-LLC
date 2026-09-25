import importlib.util
import os
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestMetaLeadAttribution(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Attribution Meta', 'company_id': cls.env.company.id,
            'app_id': 'app', 'app_secret': 'secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Attribution Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '300',
            'page_access_token': 'pagetok-SECRET-1',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Attribution Form', 'company_id': cls.env.company.id,
            'page_id': cls.page.id, 'meta_form_id': '500',
        })
        cls.env['meta.form.mapping'].create({
            'form_id': cls.form.id, 'meta_field_name': 'email', 'odoo_field_name': 'email_from',
        })
        cls.Queue = cls.env['meta.lead.queue']
        cls.Lead = cls.env['crm.lead']

    def _enqueue(self, leadgen_id):
        return self.Queue.enqueue_event(self.env.company, self.page, leadgen_id, '500', {'test': True})

    def _full_payload(self, leadgen_id):
        return {
            'id': leadgen_id, 'created_time': '2026-09-24T10:00:00+0000',
            'ad_id': 'ad-1', 'adset_id': 'adset-1', 'campaign_id': 'camp-1',
            'ad_name': 'Ad One', 'adset_name': 'Adset One', 'campaign_name': 'Campaign One',
            'form_id': '500', 'platform': 'facebook', 'is_organic': False,
            'field_data': [{'name': 'full_name', 'values': ['Test Customer']}],
        }

    def _process(self, queue, payload):
        """Run process_one with meta.account._request mocked — no real HTTP."""
        with patch.object(type(self.account), '_request', return_value=payload) as mock_request:
            queue.process_one()
        return mock_request

    def test_lead_stores_full_attribution(self):
        queue = self._enqueue('attr-lead-1')
        self._process(queue, self._full_payload('attr-lead-1'))
        lead = queue.crm_lead_id
        self.assertEqual(queue.state, 'done')
        self.assertEqual(lead.meta_campaign_id, 'camp-1')
        self.assertEqual(lead.meta_adset_id, 'adset-1')
        self.assertEqual(lead.meta_ad_id, 'ad-1')
        self.assertEqual(lead.meta_campaign_name, 'Campaign One')
        self.assertEqual(lead.meta_adset_name, 'Adset One')
        self.assertEqual(lead.meta_ad_name, 'Ad One')
        self.assertEqual(lead.meta_form_id, self.form)
        self.assertEqual(lead.meta_platform, 'facebook')
        # Legacy and canonical adset fields must never diverge.
        self.assertEqual(lead.meta_adgroup_id, 'adset-1')
        self.assertEqual(lead.meta_adgroup_id, lead.meta_adset_id)

    def test_fetch_lead_requests_attribution_names(self):
        queue = self._enqueue('attr-lead-2')
        mock_request = self._process(queue, self._full_payload('attr-lead-2'))
        requested = mock_request.call_args.kwargs['params']['fields'].split(',')
        for field_name in ('ad_id', 'ad_name', 'adset_id', 'adset_name', 'campaign_id', 'campaign_name'):
            self.assertIn(field_name, requested)

    def test_organic_lead_without_attribution(self):
        payload = {
            'id': 'attr-lead-3', 'created_time': '2026-09-24T10:00:00+0000',
            'form_id': '500', 'platform': 'instagram', 'is_organic': True,
            'field_data': [],
        }
        queue = self._enqueue('attr-lead-3')
        self._process(queue, payload)
        lead = queue.crm_lead_id
        self.assertEqual(queue.state, 'done')
        self.assertTrue(lead.meta_is_organic)
        self.assertFalse(lead.meta_campaign_id)
        self.assertFalse(lead.meta_adset_id)
        self.assertFalse(lead.meta_ad_id)
        self.assertFalse(lead.meta_campaign_name)
        self.assertEqual(lead.meta_platform, 'instagram')

    def test_missing_names_still_creates_lead(self):
        payload = self._full_payload('attr-lead-4')
        del payload['ad_name'], payload['adset_name'], payload['campaign_name']
        queue = self._enqueue('attr-lead-4')
        self._process(queue, payload)
        lead = queue.crm_lead_id
        self.assertEqual(queue.state, 'done')
        self.assertEqual(lead.meta_campaign_id, 'camp-1')
        self.assertEqual(lead.meta_adset_id, 'adset-1')
        self.assertFalse(lead.meta_campaign_name)
        self.assertFalse(lead.meta_adset_name)
        self.assertFalse(lead.meta_ad_name)

    def test_field_mapping_still_applies(self):
        payload = self._full_payload('attr-lead-map')
        payload['field_data'] = [
            {'name': 'full_name', 'values': ['Mapped Name']},
            {'name': 'email', 'values': ['mapped@example.com']},
        ]
        queue = self._enqueue('attr-lead-map')
        self._process(queue, payload)
        lead = queue.crm_lead_id
        self.assertEqual(lead.contact_name, 'Mapped Name')
        self.assertEqual(lead.email_from, 'mapped@example.com')
        self.assertEqual(lead.meta_campaign_id, 'camp-1')

    def test_unique_meta_lead_constraint_unchanged(self):
        self.Lead.create({'name': 'L1', 'meta_lead_id': 'attr-dup-1'})
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Lead.create({'name': 'L2', 'meta_lead_id': 'attr-dup-1'})

    def test_fetch_error_does_not_leak_page_token(self):
        queue = self._enqueue('attr-lead-leak')

        class FakeResponse:
            status_code = 400
            content = b'x'
            text = 'bad request pagetok-SECRET-1'

            def json(self):
                return {'error': {'code': 100, 'message': 'invalid token pagetok-SECRET-1'}}

        with patch(
                'odoo.addons.crm_meta_lead_ads.models.meta_account.requests.request',
                return_value=FakeResponse()):
            queue.process_one()
        self.assertEqual(queue.state, 'retry')
        self.assertTrue(queue.error_message)
        self.assertNotIn('pagetok-SECRET-1', queue.error_message)
        logs = self.env['meta.lead.log'].search([('queue_id', '=', queue.id)])
        for log in logs:
            self.assertNotIn('pagetok-SECRET-1', log.message or '')

    def test_lead_form_view_shows_attribution_fields(self):
        user = self.env['res.users'].create({
            'name': 'Meta CRM user', 'login': 'meta_crm_view_test',
            'group_ids': [(6, 0, [
                self.env.ref('sales_team.group_sale_salesman').id,
                self.env.ref('crm_meta_lead_ads.group_meta_lead_user').id,
            ])],
        })
        arch = self.Lead.with_user(user).get_view(view_type='form')['arch']
        for field_name in ('meta_campaign_id', 'meta_campaign_name', 'meta_adset_id',
                           'meta_adset_name', 'meta_ad_name', 'meta_adgroup_id'):
            self.assertIn(field_name, arch)

    def test_crm_only_user_can_edit_lead_without_meta_identity_access(self):
        """CRM staff must not need the Meta role to open and edit a lead."""
        user = self.env['res.users'].create({
            'name': 'CRM-only user', 'login': 'crm_only_identity_test',
            'group_ids': [(6, 0, [self.env.ref('sales_team.group_sale_salesman').id])],
        })
        lead = self.Lead.create({'name': 'Customer lead', 'user_id': user.id})
        self.env['meta.lead.identity'].create({
            'company_id': self.env.company.id, 'crm_lead_id': lead.id,
            'meta_lead_id': 'crm-only-access-1', 'match_type': 'created',
        })
        Lead = self.Lead.with_user(user)
        arch = Lead.get_view(view_type='form')['arch']
        self.assertNotIn('meta_identity_count', arch)
        self.assertNotIn('action_open_meta_conversation', arch)
        self.assertNotIn('meta_routing_rule_id', arch)
        self.assertNotIn('meta_identity_count', Lead.fields_get(['meta_identity_count']))
        Lead.browse(lead.id).write({'name': 'Customer lead updated'})
        self.assertEqual(lead.name, 'Customer lead updated')

    # --- Migration 19.0.3.0.0 ---

    def _load_migration(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'migrations', '19.0.3.0.0', 'post-migration.py')
        spec = importlib.util.spec_from_file_location(
            'crm_meta_lead_ads_post_migration_19_0_3_0_0', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_migration_backfills_adset_id_idempotently(self):
        lead = self.Lead.create({
            'name': 'Legacy Lead', 'meta_lead_id': 'attr-legacy-1',
            'meta_adgroup_id': 'legacy-adset-1',
        })
        lead_with_new = self.Lead.create({
            'name': 'New Lead', 'meta_lead_id': 'attr-legacy-2',
            'meta_adgroup_id': 'legacy-adset-2', 'meta_adset_id': 'new-adset-2',
        })
        module = self._load_migration()
        module.migrate(self.env.cr, '19.0.3.0.0')
        lead.invalidate_recordset()
        lead_with_new.invalidate_recordset()
        self.assertEqual(lead.meta_adset_id, 'legacy-adset-1')
        self.assertEqual(lead.meta_adgroup_id, 'legacy-adset-1')
        # An existing canonical value is never overwritten by the legacy one.
        self.assertEqual(lead_with_new.meta_adset_id, 'new-adset-2')
        # Re-running is a no-op and keeps all values stable.
        module.migrate(self.env.cr, '19.0.3.0.0')
        lead.invalidate_recordset()
        lead_with_new.invalidate_recordset()
        self.assertEqual(lead.meta_adset_id, 'legacy-adset-1')
        self.assertEqual(lead_with_new.meta_adset_id, 'new-adset-2')

    # --- meta.message multi-company record rule ---

    def _company_page(self, name):
        company = self.env['res.company'].create({'name': name})
        account = self.env['meta.account'].create({
            'name': name, 'company_id': company.id,
            'app_id': 'app-%s' % name, 'app_secret': 'secret',
        })
        page = self.env['meta.page'].create({
            'name': name, 'company_id': company.id, 'account_id': account.id,
            'meta_page_id': 'page-%s' % name, 'page_access_token': 'token-%s' % name,
        })
        return company, page

    def _message(self, company, page, mid):
        return self.env['meta.message'].create({
            'company_id': company.id, 'page_id': page.id,
            'sender_psid': 'psid-%s' % mid, 'meta_message_id': mid, 'message_text': 'x',
        })

    def _meta_user(self, login, companies, group_xmlid):
        return self.env['res.users'].create({
            'name': login, 'login': login,
            'company_id': companies[0].id, 'company_ids': [(6, 0, companies.ids)],
            'group_ids': [(6, 0, [self.env.ref(group_xmlid).id])],
        })

    def test_message_company_rule_single_company_user(self):
        company_b, page_b = self._company_page('Attrib Co B')
        msg_a = self._message(self.env.company, self.page, 'msg-a-1')
        msg_b = self._message(company_b, page_b, 'msg-b-1')
        user = self._meta_user(
            'attrib_user_test', self.env.company, 'crm_meta_lead_ads.group_meta_lead_user')
        Message = self.env['meta.message'].with_user(user)
        visible = Message.search([('id', 'in', [msg_a.id, msg_b.id])])
        self.assertIn(msg_a, visible)
        self.assertNotIn(msg_b, visible)
        with self.assertRaises(AccessError):
            Message.browse(msg_b.id).read(['message_text'])

    def test_message_company_rule_multi_company_manager(self):
        company_b, page_b = self._company_page('Attrib Co MB')
        company_c, page_c = self._company_page('Attrib Co MC')
        msg_a = self._message(self.env.company, self.page, 'msg-m-a')
        msg_b = self._message(company_b, page_b, 'msg-m-b')
        msg_c = self._message(company_c, page_c, 'msg-m-c')
        companies = self.env.company | company_b
        user = self._meta_user(
            'attrib_manager_test', companies, 'crm_meta_lead_ads.group_meta_lead_manager')
        visible = self.env['meta.message'].with_user(user).search(
            [('id', 'in', [msg_a.id, msg_b.id, msg_c.id])])
        self.assertIn(msg_a, visible)
        self.assertIn(msg_b, visible)
        # A company outside the manager's allowed companies stays invisible.
        self.assertNotIn(msg_c, visible)
