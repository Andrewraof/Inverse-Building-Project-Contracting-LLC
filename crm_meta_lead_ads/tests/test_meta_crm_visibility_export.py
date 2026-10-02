from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase
from odoo.tools.safe_eval import safe_eval


class TestMetaCrmVisibilityExport(TransactionCase):
    """Regression coverage for the four customer CRM reporting complaints."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = [cls.env.ref('base.group_user').id,
                  cls.env.ref('sales_team.group_sale_salesman').id]
        cls.seller = cls.env['res.users'].create({
            'name': 'Reporting Seller', 'login': 'reporting-seller@example.invalid',
            'company_id': cls.env.company.id,
            'company_ids': [Command.set(cls.env.company.ids)],
            'group_ids': [Command.set(groups)],
        })
        cls.other_seller = cls.env['res.users'].create({
            'name': 'Other Reporting Seller', 'login': 'other-reporting@example.invalid',
            'company_id': cls.env.company.id,
            'company_ids': [Command.set(cls.env.company.ids)],
            'group_ids': [Command.set(groups)],
        })
        cls.Lead = cls.env['crm.lead']

    def _lead(self, **values):
        return self.Lead.create({
            'name': 'Reporting fixture', 'company_id': self.env.company.id,
            'type': 'lead', 'user_id': self.seller.id,
            **values,
        })

    def _template_paths(self):
        template = self.env.ref('crm_meta_lead_ads.export_meta_leads',
                                raise_if_not_found=False)
        self.assertTrue(template, 'Missing safe CRM export template')
        return template.export_fields.mapped('name')

    def test_converted_opportunity_remains_in_unified_list(self):
        action = self.env.ref('crm_meta_lead_ads.action_meta_crm_records',
                              raise_if_not_found=False)
        self.assertTrue(action, 'Missing unified Meta leads/opportunities action')
        lead = self._lead(meta_lead_id='report-converted', user_id=False)
        domain = safe_eval(action.domain)
        context = safe_eval(action.context)
        visible = self.Lead.with_user(self.seller).with_context(**context)
        self.assertIn(lead, visible.search(domain))
        lead.with_user(self.seller).convert_opportunity(False, False, False)
        self.assertEqual(lead.type, 'opportunity')
        self.assertIn(lead, visible.search(domain))
        self.assertNotIn(lead, visible.search(domain + [('user_id', '=', self.seller.id)]))
        menu = self.env.ref('crm_meta_lead_ads.menu_meta_crm_records')
        self.assertEqual(menu.action, action)

    def test_unified_list_preserves_owner_and_company_rules(self):
        action = self.env.ref('crm_meta_lead_ads.action_meta_crm_records',
                              raise_if_not_found=False)
        self.assertTrue(action, 'Missing unified Meta CRM action')
        own = self._lead(meta_lead_id='report-own')
        other = self._lead(meta_lead_id='report-other', user_id=self.other_seller.id)
        company = self.env['res.company'].create({'name': 'Reporting Other Company'})
        cross_company = self._lead(meta_lead_id='report-company',
                                   company_id=company.id, user_id=False)
        found = self.Lead.with_user(self.seller).search(safe_eval(action.domain))
        self.assertIn(own, found)
        self.assertNotIn(other, found)
        self.assertNotIn(cross_company, found)

    def test_template_exports_assignee_not_creator(self):
        lead = self.Lead.with_user(self.seller).create({
            'name': 'Different creator and assignee', 'type': 'lead',
            'company_id': self.env.company.id, 'user_id': self.other_seller.id,
        })
        paths = self._template_paths()
        row = lead.with_user(self.env.user).export_data(paths)['datas'][0]
        self.assertIn('user_id/name', paths)
        self.assertNotIn('create_uid/name', paths)
        self.assertEqual(row[paths.index('user_id/name')], self.other_seller.name)
        self.assertEqual(lead.create_uid, self.seller)

    def test_source_exports_actual_value_and_keeps_missing_empty(self):
        paths = self._template_paths()
        source = self.env['utm.source'].create({'name': 'Manually selected source'})
        lead = self._lead(source_id=source.id)
        empty = self._lead(source_id=False, user_id=False)
        rows = (lead | empty).with_user(self.seller).export_data(paths)['datas']
        index = paths.index('source_id/name')
        self.assertEqual(rows[0][index], 'Manually selected source')
        self.assertFalse(rows[1][index])
        self.assertEqual(lead.source_id, source)
        self.assertFalse(empty.source_id)
        self.assertFalse(rows[1][paths.index('user_id/name')])

    def test_notes_include_description_and_internal_notes_not_discussion(self):
        self.assertIn('meta_export_notes', self.Lead._fields)
        lead = self._lead(description='<p>Customer requirements</p>')
        lead.message_post(body='<p>First internal note</p>',
                          subtype_xmlid='mail.mt_note', message_type='comment')
        lead.message_post(body='<p>Second internal note</p>',
                          subtype_xmlid='mail.mt_note', message_type='comment')
        lead.message_post(body='External discussion',
                          subtype_xmlid='mail.mt_comment', message_type='comment')
        notes = lead.with_user(self.seller).meta_export_notes
        self.assertIn('Customer requirements', notes)
        self.assertLess(notes.index('First internal note'), notes.index('Second internal note'))
        self.assertNotIn('<p>', notes)
        self.assertNotIn('External discussion', notes)
        self.assertFalse(self._lead().with_user(self.seller).meta_export_notes)

    def test_open_activity_details_export_and_refresh_after_edit(self):
        self.assertIn('meta_export_open_activities', self.Lead._fields)
        lead = self._lead()
        activity = lead.activity_schedule(
            'mail.mail_activity_data_todo', summary='Call back',
            note='<p>Ask about budget</p>', date_deadline='2026-10-15',
            user_id=self.seller.id)
        text = lead.with_user(self.seller).meta_export_open_activities
        for value in ('Call back', 'Ask about budget', '2026-10-15', self.seller.name):
            self.assertIn(value, text)
        activity.write({'summary': 'Updated follow-up'})
        self.assertIn('Updated follow-up', lead.with_user(self.seller).meta_export_open_activities)
        self.assertNotIn('Call back', lead.with_user(self.seller).meta_export_open_activities)

    def test_completed_activity_exports_feedback_not_as_open(self):
        self.assertIn('meta_export_done_activities', self.Lead._fields)
        lead = self._lead()
        activity = lead.activity_schedule(
            'mail.mail_activity_data_todo', summary='Finished call',
            note='<p>Activity context</p>', user_id=self.seller.id)
        activity.with_user(self.seller).action_feedback(feedback='Spoke to client')
        lead = lead.with_user(self.seller)
        self.assertIn('Spoke to client', lead.meta_export_done_activities)
        self.assertIn('Finished call', lead.meta_export_done_activities)
        self.assertNotIn('Finished call', lead.meta_export_open_activities)
        # A caller with active_test=False must not resurrect archived done activities.
        self.assertNotIn('Finished call', lead.with_context(active_test=False).meta_export_open_activities)

    def test_real_export_has_one_row_per_lead_and_no_cross_contamination(self):
        self.assertIn('meta_export_notes', self.Lead._fields)
        first = self._lead(name='First', description='<p>First note</p>')
        second = self._lead(name='Second', description='<p>Second note</p>')
        empty = self._lead(name='Empty')
        rows = (first | second | empty).with_user(self.seller).export_data(
            ['name', 'meta_export_notes'])['datas']
        self.assertEqual(rows, [['First', 'First note'], ['Second', 'Second note'], ['Empty', '']])

    def test_export_does_not_bypass_record_access(self):
        self.assertIn('meta_export_notes', self.Lead._fields)
        lead = self._lead(user_id=self.other_seller.id,
                          description='<p>Other seller private note</p>')
        self.assertIn('Other seller private note', lead.meta_export_notes)
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            lead.with_user(self.seller).export_data(['meta_export_notes'])

    def test_cached_note_is_not_shared_between_users(self):
        self.assertIn('meta_export_notes', self.Lead._fields)
        lead = self._lead(description='<p>Caller-visible description</p>')
        lead.message_post(body='Privileged internal note', subtype_xmlid='mail.mt_note',
                          message_type='comment')
        self.assertIn('Privileged internal note', lead.meta_export_notes)
        # A second caller must recompute under their own mail.message access.
        self.assertIn('Caller-visible description', lead.with_user(self.seller).meta_export_notes)

