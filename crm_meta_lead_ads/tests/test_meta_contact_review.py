from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase

from odoo.addons.crm_meta_lead_ads.models.meta_contact_review import CUTOVER_KEY


class TestMetaContactReview(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.account = cls.env['meta.account'].create({
            'name': 'Intake Test', 'company_id': cls.company.id,
            'app_id': 'test-app', 'app_secret': 'test-secret'})
        cls.page = cls.env['meta.page'].create({
            'name': 'Intake Page', 'company_id': cls.company.id,
            'account_id': cls.account.id, 'meta_page_id': 'intake-page',
            'page_access_token': 'test-page-token'})
        cls.env['ir.config_parameter'].set_param(
            CUTOVER_KEY, fields.Datetime.now() - timedelta(minutes=1))
        cls.sales = cls._user('intake-sales', [
            'sales_team.group_sale_salesman', 'crm_meta_lead_ads.group_meta_lead_user'])
        cls.reviewer = cls._user('intake-manager', [
            'sales_team.group_sale_manager', 'crm_meta_lead_ads.group_meta_contact_reviewer'])

    @classmethod
    def _user(cls, login, groups):
        return cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'company_id': cls.company.id,
            'company_ids': [Command.set(cls.company.ids)],
            'group_ids': [Command.set([cls.env.ref('base.group_user').id]
                                     + [cls.env.ref(g).id for g in groups])],
        })

    def _intake(self, key='intake-1', created=None):
        q = self.env['meta.lead.queue'].enqueue_event(self.company, self.page, key)
        payload = {'id': key, 'platform': 'facebook', 'campaign_id': 'test-campaign',
                   'field_data': [{'name': 'full_name', 'values': ['Review Person']},
                                  {'name': 'email', 'values': ['review@example.test']},
                                  {'name': 'phone_number', 'values': ['+971500001234']}]}
        if created:
            payload['created_time'] = created
        with patch.object(type(q), '_fetch_lead', return_value=payload):
            q.process_one()
        self.assertEqual(q.state, 'done', q.error_message)
        return q

    def test_new_enquiry_contact_only_unassigned(self):
        before = self.env['crm.lead'].search_count([])
        q = self._intake()
        self.assertEqual(q.match_result, 'contact_review')
        self.assertFalse(q.crm_lead_id)
        self.assertEqual(self.env['crm.lead'].search_count([]), before)
        p = q.review_partner_id
        self.assertEqual(p.meta_review_state, 'pending')
        self.assertEqual(p.email, 'review@example.test')
        self.assertFalse(p.user_id)
        self.assertFalse(p.message_follower_ids)
        self.assertFalse(p.activity_ids)

    def test_sales_cannot_search_read_approve_or_read_queue(self):
        q = self._intake()
        p = q.review_partner_id
        self.assertFalse(self.env['res.partner'].with_user(self.sales).search([('id', '=', p.id)]))
        with self.assertRaises(AccessError):
            p.with_user(self.sales).read(['email'])
        with self.assertRaises(AccessError):
            p.with_user(self.sales).action_meta_approve()
        with self.assertRaises(AccessError):
            q.with_user(self.sales).read(['fetched_payload'])
        self.assertEqual(p.with_user(self.reviewer).read(['email'])[0]['email'], p.email)

    def test_repeat_event_and_manual_retry_preserve_single_contact(self):
        q = self._intake()
        p = q.review_partner_id
        self.assertEqual(self.env['meta.lead.queue'].enqueue_event(self.company, self.page, q.meta_lead_id), q)
        q.write({'state': 'pending'})
        with patch.object(type(q), '_fetch_lead', return_value=q.fetched_payload):
            q.process_one()
        self.assertEqual(q.review_partner_id, p)
        self.assertEqual(self.env['res.partner'].search_count([('meta_review_queue_id', '=', q.id)]), 1)

    def test_approval_creates_one_lead_and_releases_contact(self):
        q = self._intake()
        p = q.review_partner_id.with_user(self.reviewer)
        p.meta_review_user_id = self.sales
        p.action_meta_approve()
        lead = p.meta_review_lead_id
        self.assertTrue(lead)
        self.assertEqual(lead.user_id, self.sales)
        self.assertEqual(lead.meta_campaign_id, 'test-campaign')
        self.assertEqual(p.meta_review_state, 'approved')
        self.assertEqual(p.with_user(self.sales).read(['email'])[0]['email'], 'review@example.test')
        p.action_meta_approve()
        self.assertEqual(p.meta_review_lead_id, lead)
        self.assertEqual(self.env['crm.lead'].search_count([('meta_lead_id', '=', q.meta_lead_id)]), 1)

    def test_approval_respects_lead_creation_permissions(self):
        q = self._intake()
        p = q.review_partner_id.with_user(self.reviewer)
        p.meta_review_user_id = self.sales
        def denied(model, vals):
            self.assertFalse(model.env.su, 'Approval must not bypass lead creation restrictions')
            raise AccessError('Lead creation restricted')
        with patch.object(type(self.env['crm.lead']), 'create', denied):
            with self.assertRaises(AccessError):
                p.action_meta_approve()
        self.assertEqual(p.meta_review_state, 'pending')
        self.assertFalse(p.meta_review_lead_id)

    def test_approval_without_salesperson_stays_unassigned(self):
        q = self._intake()
        p = q.review_partner_id.with_user(self.reviewer).with_context(
            default_user_id=self.reviewer.id)
        p.write({'name': 'Reviewed without assignment'})
        self.assertEqual(p.meta_review_state, 'pending')
        p.action_meta_approve()
        lead = p.meta_review_lead_id
        self.assertTrue(lead)
        self.assertEqual(p.meta_review_state, 'approved')
        self.assertEqual(lead.type, 'lead')
        self.assertFalse(lead.user_id)
        self.assertFalse(p.user_id)
        self.assertFalse(lead.meta_routing_rule_id)
        self.assertEqual(q.crm_lead_id, lead)
        p.action_meta_approve()
        self.assertEqual(self.env['crm.lead'].search_count([
            ('meta_lead_id', '=', q.meta_lead_id)]), 1)
        lead.write({'user_id': self.sales.id})
        self.assertEqual(lead.user_id, self.sales)

    def test_optional_salesperson_still_validates_selected_user(self):
        p = self._intake().review_partner_id.with_user(self.reviewer)
        inactive = self._user('intake-inactive', ['sales_team.group_sale_salesman'])
        inactive.active = False
        portal = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Intake portal', 'login': 'intake-portal',
            'group_ids': [Command.set([self.env.ref('base.group_portal').id])],
        })
        other = self.env['res.company'].create({'name': 'Sales other company'})
        outsider = self._user('intake-other-sales', ['sales_team.group_sale_salesman'])
        outsider.write({'company_ids': [Command.set(other.ids)], 'company_id': other.id})
        for salesperson in (inactive, portal, outsider):
            with self.subTest(salesperson=salesperson.login):
                p.meta_review_user_id = salesperson
                with self.assertRaises(UserError):
                    p.action_meta_approve()
                self.assertEqual(p.meta_review_state, 'pending')
                self.assertFalse(p.meta_review_lead_id)

    def test_unassigned_approval_keeps_lead_creation_permissions(self):
        p = self._intake().review_partner_id.with_user(self.reviewer)

        def denied(model, vals):
            self.assertFalse(model.env.su)
            raise AccessError('Lead creation restricted')

        with patch.object(type(self.env['crm.lead']), 'create', denied):
            with self.assertRaises(AccessError):
                p.action_meta_approve()
        self.assertEqual(p.meta_review_state, 'pending')
        self.assertFalse(p.meta_review_lead_id)
        self.assertFalse(p.user_id)

    def test_rejection_remains_private(self):
        p = self._intake().review_partner_id
        p.with_user(self.reviewer).action_meta_reject()
        self.assertEqual(p.meta_review_state, 'rejected')
        self.assertFalse(self.env['res.partner'].with_user(self.sales).search([('id', '=', p.id)]))
        with self.assertRaises(UserError):
            p.with_user(self.reviewer).action_meta_approve()

    def test_existing_contacts_and_leads_untouched(self):
        existing = self.env['res.partner'].create({'name': 'Existing customer', 'email': 'review@example.test'})
        lead = self.env['crm.lead'].create({'name': 'Existing deal', 'email_from': existing.email,
                                          'partner_id': existing.id, 'user_id': self.sales.id})
        q = self._intake()
        self.assertNotEqual(q.review_partner_id, existing)
        self.assertEqual(lead.user_id, self.sales)
        self.assertEqual(lead.partner_id, existing)
        self.assertFalse(existing.meta_review_state)
        self.assertTrue(self.env['res.partner'].with_user(self.sales).search([('id', '=', existing.id)]))

    def test_pre_cutover_enquiry_uses_existing_pipeline(self):
        q = self._intake(created='2020-01-01T00:00:00+0000')
        self.assertFalse(q.review_partner_id)
        self.assertTrue(q.crm_lead_id)
        self.assertFalse(q.contact_review_required)

    def test_no_forged_transition_or_cross_company_review(self):
        p = self._intake().review_partner_id
        with self.assertRaises(AccessError):
            p.with_user(self.reviewer).write({'meta_review_state': 'approved'})
        other = self.env['res.company'].create({'name': 'Other intake company'})
        outsider = self._user('intake-outsider', ['crm_meta_lead_ads.group_meta_contact_reviewer'])
        outsider.write({'company_ids': [Command.set(other.ids)], 'company_id': other.id})
        self.assertFalse(self.env['res.partner'].with_user(outsider).search([('id', '=', p.id)]))
        with self.assertRaises(AccessError):
            p.with_user(outsider).action_meta_approve()
