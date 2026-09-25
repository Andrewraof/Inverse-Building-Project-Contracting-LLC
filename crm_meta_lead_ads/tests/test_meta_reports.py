import importlib.util
import os

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tools.safe_eval import safe_eval

from psycopg2 import IntegrityError


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
            'view_meta_first_response_pivot': ('meta.conversation', 'pivot'),
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
            'action_meta_first_response_report': 'meta.conversation',
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
        self.assertIn("('first_response_at', '=', False)", arch)

    # 3b. The conversation pivot measures lead conversion per assignee.
    def test_conversation_pivot_measures_lead_conversion(self):
        view = self.env.ref('crm_meta_lead_ads.view_meta_conversation_pivot')
        arch = view.arch_db
        self.assertIn('name="linked_lead_count" type="measure"', arch)
        self.assertNotIn('name="first_response_seconds" type="measure"', arch)
        response_view = self.env.ref('crm_meta_lead_ads.view_meta_first_response_pivot')
        self.assertIn('name="first_response_seconds" type="measure"', response_view.arch_db)

    def test_first_response_report_averages_only_replied_conversations(self):
        Conv = self.env['meta.conversation']
        now = fields.Datetime.now()
        for psid, seconds, replied in [
            ('reply-fast', 60, True),
            ('reply-slow', 180, True),
            ('reply-pending', 0, False),
        ]:
            Conv.create({
                'company_id': self.env.company.id,
                'page_id': self.page.id,
                'psid': psid,
                'first_response_seconds': seconds,
                'first_response_at': now if replied else False,
            })
        action = self.env.ref('crm_meta_lead_ads.action_meta_first_response_report')
        domain = safe_eval(action.domain) + [('page_id', '=', self.page.id)]
        groups = Conv.read_group(domain, ['first_response_seconds'], [])
        self.assertEqual(groups[0]['first_response_seconds'], 120.0)

    def test_conversation_linked_lead_count_aggregates(self):
        Conv = self.env['meta.conversation']
        linked = Conv.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'psid': 'report-linked',
        })
        Conv.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'psid': 'report-unlinked',
        })
        linked.action_create_lead()
        groups = Conv.read_group(
            [('page_id', '=', self.page.id)], ['linked_lead_count:sum'], [])
        self.assertEqual(groups[0]['linked_lead_count'], 1)

    def test_upgrade_backfills_first_reply_and_linked_count_idempotently(self):
        Conv = self.env['meta.conversation']
        conv = Conv.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'psid': 'report-upgrade',
        })
        conv.action_create_lead()
        Message = self.env['meta.message']
        for mid, direction in [('upgrade-in', 'inbound'),
                               ('upgrade-out', 'outbound')]:
            Message.create({
                'company_id': self.env.company.id, 'page_id': self.page.id,
                'conversation_id': conv.id, 'sender_psid': conv.psid,
                'meta_message_id': mid, 'direction': direction,
                'send_state': 'sent' if direction == 'outbound' else False,
                'sent_at': '2026-09-20 10:00:00',
            })
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE meta_conversation SET first_response_at = NULL, '
            'linked_lead_count = 0 WHERE id = %s', (conv.id,))
        conv.invalidate_recordset(['first_response_at', 'linked_lead_count'])
        path = os.path.join(os.path.dirname(__file__), '..', 'migrations',
                            '19.0.4.1.0', 'post-migration.py')
        spec = importlib.util.spec_from_file_location('meta_upgrade_19_0_4_1_0', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for _ in range(2):
            module.migrate(self.env.cr, '19.0.4.0.0')
            conv.invalidate_recordset(['first_response_at', 'linked_lead_count'])
            self.assertEqual(conv.first_response_at,
                             fields.Datetime.to_datetime('2026-09-20 10:00:00'))
            self.assertEqual(conv.linked_lead_count, 1)

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


class TestMetaAdInsight(TransactionCase):
    # CPL structure: populated only once Meta grants ads_read.

    def _row(self, **extra):
        vals = {'date': '2026-09-01', 'company_id': self.env.company.id,
                'meta_campaign_id': 'camp-1', 'meta_adset_id': 'adset-1',
                'meta_ad_id': 'ad-1', 'spend': 100.0, 'impressions': 1000,
                'clicks': 50, 'leads_count': 4}
        vals.update(extra)
        return self.env['meta.ad.insight'].create(vals)

    # 1. CPL is spend over leads, zero-safe.
    def test_cpl_compute(self):
        row = self._row()
        self.assertEqual(row.cpl, 25.0)
        no_leads = self._row(date='2026-09-02', leads_count=0)
        self.assertEqual(no_leads.cpl, 0.0)

    # 2. One row per (company, day, campaign, adset, ad).
    def test_unique_row_per_ad_day(self):
        self._row()
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self._row()

    # 3. Rows are isolated per company.
    def test_company_record_rule(self):
        other_company = self.env['res.company'].create({'name': 'Insight Co'})
        self._row()
        self._row(date='2026-09-02', company_id=other_company.id)
        user = self.env['res.users'].create({
            'name': 'Insight User', 'login': 'insight-user@test',
            'company_id': self.env.company.id, 'company_ids': [(4, self.env.company.id)],
            'group_ids': [(4, self.env.ref('crm_meta_lead_ads.group_meta_lead_user').id)],
        })
        visible = self.env['meta.ad.insight'].with_user(user).search([])
        self.assertEqual(len(visible), 1)

    # 4. Report views and the menu action exist.
    def test_insight_report_views_exist(self):
        pivot = self.env.ref('crm_meta_lead_ads.view_meta_ad_insight_pivot')
        self.assertEqual(pivot.type, 'pivot')
        action = self.env.ref('crm_meta_lead_ads.action_meta_ad_insight')
        self.assertEqual(action.res_model, 'meta.ad.insight')
