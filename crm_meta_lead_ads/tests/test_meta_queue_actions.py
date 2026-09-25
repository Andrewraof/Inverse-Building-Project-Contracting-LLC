from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestMetaQueueActions(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Actions Meta', 'company_id': cls.env.company.id,
            'app_id': 'app-act', 'app_secret': 'secret-act',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Actions Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '950',
            'page_access_token': 'pagetok-ACT',
        })
        cls.Queue = cls.env['meta.lead.queue']
        cls.Log = cls.env['meta.lead.log']

    def _queue(self, state='failed', **extra):
        vals = {'company_id': self.env.company.id, 'page_id': self.page.id,
                'meta_lead_id': extra.pop('meta_lead_id', 'QA1'), 'state': state,
                'match_result': extra.pop('match_result', 'failed')}
        vals.update(extra)
        return self.Queue.create(vals)

    # 1. Retry selected requeues failed/ambiguous records and logs the audit.
    def test_retry_selected(self):
        rec = self._queue()
        rec.action_retry_selected()
        self.assertEqual(rec.state, 'pending')
        self.assertFalse(rec.next_retry_at)
        self.assertTrue(self.Log.search([
            ('queue_id', '=', rec.id), ('action', '=', 'manual_retry')]))

    # 2. Done records are never requeued by retry.
    def test_retry_skips_done(self):
        rec = self._queue(state='done', match_result='created')
        rec.action_retry_selected()
        self.assertEqual(rec.state, 'done')

    # 3. Retry-all-failed touches every failed record.
    def test_retry_all_failed(self):
        a = self._queue(meta_lead_id='QA-A')
        b = self._queue(meta_lead_id='QA-B')
        self.Queue.action_retry_all_failed()
        self.assertEqual({a.state, b.state}, {'pending'})

    # 4. Reset to pending is manager-only.
    def test_reset_pending_manager_only(self):
        rec = self._queue(state='ambiguous', match_result='ambiguous')
        user = self.env['res.users'].create({
            'name': 'Plain Meta User', 'login': 'plain-meta-user@test',
            'company_id': self.env.company.id, 'company_ids': [(4, self.env.company.id)],
            'group_ids': [(4, self.env.ref('crm_meta_lead_ads.group_meta_lead_user').id)],
        })
        with self.assertRaises(UserError):
            rec.with_user(user).action_reset_pending()
        self.env.user.group_ids = [(4, self.env.ref('crm_meta_lead_ads.group_meta_lead_manager').id)]
        rec.action_reset_pending()
        self.assertEqual(rec.state, 'pending')
        self.assertTrue(self.Log.search([
            ('queue_id', '=', rec.id), ('action', '=', 'manual_reset')]))

    # 5. Manual resolution links the chosen lead, keeps data, writes identity.
    def test_manual_resolution_links_lead(self):
        lead = self.env['crm.lead'].create({
            'name': 'Existing Lead', 'type': 'lead',
            'company_id': self.env.company.id,
        })
        rec = self._queue(state='ambiguous', match_result='ambiguous',
                          meta_lead_id='QA-AMB', manual_lead_id=lead.id)
        rec.action_link_manual_lead()
        self.assertEqual(rec.state, 'done')
        self.assertEqual(rec.crm_lead_id, lead)
        identity = self.env['meta.lead.identity'].search([
            ('meta_lead_id', '=', 'QA-AMB'), ('company_id', '=', self.env.company.id)])
        self.assertEqual(len(identity), 1)
        self.assertEqual(identity.crm_lead_id, lead)
        self.assertEqual(identity.match_type, 'manual')
        self.assertTrue(self.Log.search([
            ('queue_id', '=', rec.id), ('action', '=', 'manual_resolution')]))

    # 6. Manual resolution never crosses companies and never creates a lead.
    def test_manual_resolution_guards(self):
        company_b = self.env['res.company'].create({'name': 'Act Co B'})
        foreign_lead = self.env['crm.lead'].create({
            'name': 'Foreign', 'type': 'lead', 'company_id': company_b.id,
        })
        rec = self._queue(state='ambiguous', match_result='ambiguous',
                          meta_lead_id='QA-X', manual_lead_id=foreign_lead.id)
        with self.assertRaises(UserError):
            rec.action_link_manual_lead()
        self.assertFalse(rec.crm_lead_id)
        rec2 = self._queue(state='ambiguous', match_result='ambiguous',
                           meta_lead_id='QA-Y')
        with self.assertRaises(UserError):
            rec2.action_link_manual_lead()
        leads_before = self.env['crm.lead'].search_count([])
        with self.assertRaises(UserError):
            rec2.action_link_manual_lead()
        self.assertEqual(self.env['crm.lead'].search_count([]), leads_before)

    # 7. Terminal failure schedules exactly one attention activity.
    def test_terminal_failure_single_activity(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'crm_meta_lead_ads.inbox_default_user_id', str(self.env.user.id))
        rec = self._queue(state='retry', match_result=False)
        rec._schedule_retry('boom', fatal=True)
        rec._schedule_retry('boom again', fatal=True)
        activities = self.env['mail.activity'].search([
            ('res_model', '=', 'meta.lead.queue'), ('res_id', '=', rec.id)])
        self.assertEqual(len(activities), 1)
        self.assertEqual(rec.state, 'failed')

    # 8. Queue notifications can be muted.
    def test_queue_notify_muted(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'crm_meta_lead_ads.queue_notify', 'False')
        rec = self._queue(state='retry', match_result=False)
        rec._schedule_retry('boom', fatal=True)
        activities = self.env['mail.activity'].search([
            ('res_model', '=', 'meta.lead.queue'), ('res_id', '=', rec.id)])
        self.assertFalse(activities)
        self.env['ir.config_parameter'].sudo().set_param(
            'crm_meta_lead_ads.queue_notify', 'True')

    # 9. Processing time is stored for the reports.
    def test_processing_seconds_computed(self):
        rec = self._queue(state='done', match_result='created',
                          received_at='2026-09-20 10:00:00',
                          processed_at='2026-09-20 10:01:30')
        self.assertEqual(rec.processing_seconds, 90.0)

    def test_sql_failure_rolls_back_before_retry_is_recorded(self):
        rec = self._queue(state='pending', match_result=False,
                          meta_lead_id='QA-SQL-ROLLBACK')

        def fail_fetch():
            self.env.cr.execute('SELECT 1 / 0')

        with patch.object(type(rec), '_fetch_lead', side_effect=fail_fetch):
            rec.process_one()

        rec.invalidate_recordset()
        self.assertEqual(rec.state, 'retry')
        self.assertEqual(rec.attempts, 1)
        self.assertIn('SQLSTATE 22012', rec.error_message)
        self.assertNotIn('current transaction is aborted', rec.error_message)
        self.assertTrue(self.Log.search([
            ('queue_id', '=', rec.id), ('action', '=', 'retry_scheduled')]))

    def test_expired_token_still_marks_account_and_queue_failed(self):
        rec = self._queue(state='pending', match_result=False,
                          meta_lead_id='QA-TOKEN-ERROR')

        def fail_fetch():
            self.account.write({'state': 'error', 'error_message': 'Token expired'})
            raise UserError('Token expired')

        with patch.object(type(rec), '_fetch_lead', side_effect=fail_fetch):
            rec.process_one()

        rec.invalidate_recordset()
        self.account.invalidate_recordset()
        self.assertEqual(self.account.state, 'error')
        self.assertEqual(rec.state, 'failed')

    def test_database_error_does_not_persist_customer_value(self):
        rec = self._queue(state='pending', match_result=False,
                          meta_lead_id='QA-PII-ERROR')
        private_value = 'private-person@example.test'

        def fail_fetch():
            self.env.cr.execute('SELECT %s::integer', (private_value,))

        with patch.object(type(rec), '_fetch_lead', side_effect=fail_fetch):
            rec.process_one()

        rec.invalidate_recordset()
        audit = self.Log.search([('queue_id', '=', rec.id),
                                 ('action', '=', 'retry_scheduled')])
        self.assertEqual(rec.state, 'retry')
        self.assertIn('SQLSTATE 22P02', rec.error_message)
        self.assertNotIn(private_value, rec.error_message)
        self.assertNotIn(private_value, '\n'.join(audit.mapped('message')))
