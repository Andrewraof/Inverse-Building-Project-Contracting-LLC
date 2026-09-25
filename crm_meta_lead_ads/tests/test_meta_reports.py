from odoo import fields
from odoo.tests.common import TransactionCase


class TestMetaReports(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Reports Meta', 'company_id': cls.env.company.id,
            'app_id': 'app-rep', 'app_secret': 'secret-rep',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Reports Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '960',
            'page_access_token': 'pagetok-REP',
        })

    # 1. Every report view exists with the right model and view type.
    def test_report_views_exist(self):
        expected = {
            'view_meta_queue_pivot': ('meta.lead.queue', 'pivot'),
            'view_meta_queue_graph': ('meta.lead.queue', 'graph'),
            'view_crm_lead_meta_pivot': ('crm.lead', 'pivot'),
            'view_crm_lead_meta_graph': ('crm.lead', 'graph'),
            'view_meta_conversation_pivot': ('meta.conversation', 'pivot'),
            'view_meta_conversation_graph': ('meta.conversation', 'graph'),
            'view_crm_lead_meta_funnel_pivot': ('crm.lead', 'pivot'),
        }
        for xml_id, (model, tag) in expected.items():
            view = self.env.ref('crm_meta_lead_ads.%s' % xml_id)
            self.assertEqual(view.model, model)
            self.assertEqual(view.type, tag)

    # 2. Report actions exist and open the expected models.
    def test_report_actions_exist(self):
        expected = {
            'action_meta_queue_report': 'meta.lead.queue',
            'action_meta_leads_report': 'crm.lead',
            'action_meta_conversation_report': 'meta.conversation',
            'action_meta_funnel_report': 'crm.lead',
        }
        for xml_id, model in expected.items():
            action = self.env.ref('crm_meta_lead_ads.%s' % xml_id)
            self.assertEqual(action.res_model, model)
            self.assertIn('pivot', action.view_mode)

    # 3. The conversation search view exposes the overdue and state filters.
    def test_conversation_search_has_overdue_filter(self):
        view = self.env.ref('crm_meta_lead_ads.view_meta_conversation_search')
        arch = view.arch_db
        self.assertIn('filter_overdue', arch)
        self.assertIn('last_inbound_at', arch)
        self.assertIn('filter_state_pending', arch)
        self.assertIn('filter_awaiting_response', arch)

    # 3b. The conversation pivot measures lead conversion per assignee.
    def test_conversation_pivot_measures_lead_conversion(self):
        view = self.env.ref('crm_meta_lead_ads.view_meta_conversation_pivot')
        arch = view.arch_db
        self.assertIn('lead_id', arch)
        self.assertIn('first_response_seconds', arch)

    # 3c. The leads pivot breaks down by sales team and salesperson.
    def test_leads_pivot_has_team_and_user_rows(self):
        view = self.env.ref('crm_meta_lead_ads.view_crm_lead_meta_pivot')
        arch = view.arch_db
        self.assertIn('team_id', arch)
        self.assertIn('user_id', arch)

    # 4. CPL unavailability is documented on the Diagnostics tab.
    def test_cpl_permission_notice_documented(self):
        view = self.env.ref('crm_meta_lead_ads.view_meta_account_form')
        self.assertIn('ads_read', view.arch_db)

    # 5. Unmapped question keys are listed; mapped ones are not.
    def test_unmapped_fields_compute(self):
        form = self.env['meta.form'].create({
            'name': 'Report Form', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': 'RF1',
            'questions_json': [{'key': 'email'}, {'key': 'custom_q'}],
        })
        self.assertTrue(form.has_unmapped_fields)
        self.assertEqual(form.unmapped_field_names, 'email, custom_q')
        self.env['meta.form.mapping'].create({
            'form_id': form.id, 'meta_field_name': 'custom_q',
            'odoo_field_name': 'description',
        })
        form.invalidate_recordset()
        self.assertEqual(form.unmapped_field_names, 'email')
        self.env['meta.form.mapping'].create({
            'form_id': form.id, 'meta_field_name': 'email',
            'odoo_field_name': 'email_from',
        })
        form.invalidate_recordset()
        self.assertFalse(form.has_unmapped_fields)

    # 6. First-response-time data aggregates for the conversations report.
    def test_conversation_first_response_aggregation(self):
        Conv = self.env['meta.conversation']
        Conv.create({'company_id': self.env.company.id, 'page_id': self.page.id,
                     'psid': 'rep-psid-1', 'first_response_seconds': 60})
        Conv.create({'company_id': self.env.company.id, 'page_id': self.page.id,
                     'psid': 'rep-psid-2', 'first_response_seconds': 180})
        groups = Conv.read_group(
            [('page_id', '=', self.page.id)], ['first_response_seconds:avg'], [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['first_response_seconds'], 120.0)

    # 7. Queue processing time aggregates for the queue report.
    def test_queue_processing_seconds_aggregation(self):
        now = fields.Datetime.now()
        self.env['meta.lead.queue'].create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'meta_lead_id': 'REP-Q1', 'state': 'done', 'match_result': 'created',
            'received_at': now, 'processed_at': fields.Datetime.add(now, seconds=30),
        })
        groups = self.env['meta.lead.queue'].read_group(
            [('page_id', '=', self.page.id)],
            ['processing_seconds:sum', 'attempts:sum'], ['match_result'])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['processing_seconds'], 30.0)
