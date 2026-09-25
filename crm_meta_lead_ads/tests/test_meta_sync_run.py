from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

RUN_LOGGER = 'odoo.addons.crm_meta_lead_ads.models.meta_sync_run'


class TestMetaSyncRun(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Run Meta', 'company_id': cls.env.company.id,
            'app_id': 'app-run', 'app_secret': 'secret-run',
            'user_access_token': 'usertok-RUN-SECRET', 'state': 'connected',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Run Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '700',
            'page_access_token': 'pagetok-RUN-SECRET',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Run Form', 'company_id': cls.env.company.id,
            'page_id': cls.page.id, 'meta_form_id': 'FR1',
        })
        cls.Run = cls.env['meta.sync.run']

    def _new_run(self, run_type='all', **extra):
        vals = {'account_id': self.account.id, 'company_id': self.env.company.id,
                'run_type': run_type}
        vals.update(extra)
        return self.Run.create(vals)

    def _patch_request(self, handler):
        state = {'calls': []}

        def fake(method, path, **kwargs):
            params = dict(kwargs.get('params') or {})
            state['calls'].append({'method': method, 'path': path, 'params': params})
            result = handler(path, params)
            if isinstance(result, Exception):
                raise result
            return result

        return patch.object(type(self.account), '_request', side_effect=fake), state

    def _all_perms(self, path, params):
        if path == 'me/permissions':
            return {'data': [
                {'name': p, 'status': 'granted'} for p in (
                    'leads_retrieval', 'pages_show_list', 'pages_read_engagement',
                    'pages_manage_metadata', 'pages_messaging')]}
        if path == 'me':
            return {'id': 'user-1', 'name': 'Test User'}
        if path == 'me/accounts':
            return {'data': []}
        if path == '700/leadgen_forms':
            return {'data': []}
        if path == 'FR1/leads':
            return {'data': []}
        if path == '700/conversations':
            return {'data': []}
        raise AssertionError('unexpected path %s' % path)

    def _drain(self, run, max_ticks=25):
        ticks = 0
        while run.state == 'running' and ticks < max_ticks:
            run._process_tick()
            run.invalidate_recordset()
            ticks += 1
        return ticks

    # 1. Create + start + full completion of an empty account.
    def test_run_completes_empty_account(self):
        run = self._new_run()
        run.action_start()
        self.assertEqual(run.state, 'running')
        patcher, _state = self._patch_request(self._all_perms)
        with patcher:
            self._drain(run)
        run.invalidate_recordset()
        self.assertEqual(run.state, 'completed')
        self.assertTrue(run.ended_at)
        self.assertEqual(set(run.line_ids.mapped('step')),
                         {'connection', 'pages', 'forms', 'leads', 'conversations', 'messages'})

    # 2. Missing permission -> the dependent step is skipped with a warning
    #    and the run completes with warnings, never failing wholesale.
    def test_missing_permission_skips_step_with_warning(self):
        def handler(path, params):
            if path == 'me/permissions':
                return {'data': [{'name': 'pages_show_list', 'status': 'granted'}]}
            if path == 'me':
                return {'id': 'user-1'}
            if path == 'me/accounts':
                return {'data': []}
            if path == '700/leadgen_forms':
                return {'data': []}
            raise AssertionError('unexpected path %s' % path)
        run = self._new_run(run_type='leads')
        run.action_start()
        patcher, _state = self._patch_request(handler)
        with patcher:
            self._drain(run)
        run.invalidate_recordset()
        self.assertEqual(run.state, 'completed_warnings')
        self.assertIn('leads_retrieval', run.missing_permissions or '')
        leads_line = run.line_ids.filtered(lambda l: l.step == 'leads')
        self.assertEqual(leads_line.state, 'warning')

    # 3. Two active runs for the same account are rejected.
    def test_no_concurrent_runs_per_account(self):
        run1 = self._new_run()
        run1.action_start()
        run2 = self._new_run()
        with self.assertRaises(UserError):
            run2.action_start()

    # 4. Failed run resumes from the saved work_state.
    def test_resume_after_failure(self):
        calls = {'leads': 0}

        def handler(path, params):
            if path == 'me/permissions':
                return {'data': [{'name': 'leads_retrieval', 'status': 'granted'}]}
            if path == 'me':
                return {'id': 'user-1'}
            if path == 'FR1/leads':
                calls['leads'] += 1
                if calls['leads'] == 1:
                    return UserError('Meta API error: temporary')
                return {'data': [{'id': 'RL1'}]}
            raise AssertionError('unexpected path %s' % path)
        run = self._new_run(run_type='leads')
        run.action_start()
        patcher, _state = self._patch_request(handler)
        with patcher:
            with self.assertRaises(UserError):
                run._process_tick()
        # Simulate the cron-level failure handling, then resume.
        run.write({'state': 'failed'})
        run.action_start()
        with patcher:
            self._drain(run)
        run.invalidate_recordset()
        self.assertEqual(run.state, 'completed')
        self.assertEqual(run.leads_queued, 1)

    # 5. Queue processing inside the run counts outcomes.
    def test_leads_step_counts_outcomes(self):
        def handler(path, params):
            if path == 'me/permissions':
                return {'data': [{'name': 'leads_retrieval', 'status': 'granted'}]}
            if path == 'me':
                return {'id': 'user-1'}
            if path == 'FR1/leads':
                return {'data': [{'id': 'Q1'}]}
            if path == 'Q1':
                return {'id': 'Q1', 'field_data': [
                    {'name': 'email', 'values': ['run-test@example.com']}]}
            raise AssertionError('unexpected path %s' % path)
        run = self._new_run(run_type='leads')
        run.action_start()
        patcher, _state = self._patch_request(handler)
        with patcher:
            self._drain(run)
        run.invalidate_recordset()
        self.assertEqual(run.leads_fetched, 1)
        self.assertEqual(run.leads_queued, 1)
        self.assertEqual(run.leads_created, 1)
        queue = self.env['meta.lead.queue'].search([('meta_lead_id', '=', 'Q1')])
        self.assertEqual(queue.sync_run_id, run)
        self.assertEqual(queue.state, 'done')

    def test_run_waits_for_more_than_one_queue_batch(self):
        lead_ids = ['BATCH-%s' % i for i in range(21)]

        def handler(path, params):
            if path == 'me/permissions':
                return {'data': [{'name': 'leads_retrieval', 'status': 'granted'}]}
            if path == 'me':
                return {'id': 'user-1'}
            if path == 'FR1/leads':
                return {'data': [{'id': lead_id} for lead_id in lead_ids]}
            if path in lead_ids:
                return {'id': path, 'field_data': [
                    {'name': 'email', 'values': ['%s@example.com' % path.lower()]}]}
            raise AssertionError('unexpected path %s' % path)

        run = self._new_run(run_type='leads')
        run.action_start()
        patcher, _state = self._patch_request(handler)
        with patcher:
            run._process_tick()
            run.invalidate_recordset()
            self.assertEqual(run.state, 'running')
            self.assertEqual(run.leads_created, 20)
            self.assertEqual(run.leads_queued, 21)
            self._drain(run)
        run.invalidate_recordset()
        self.assertEqual(run.state, 'completed')
        self.assertEqual(run.leads_created, 21)
        self.assertEqual(run.leads_queued, 21)
        run._process_own_queue()
        self.assertEqual(run.leads_created, 21)

    def test_run_rejects_pages_outside_account(self):
        other = self.env['meta.account'].create({
            'name': 'Other Meta', 'company_id': self.env.company.id,
            'app_id': 'other-app', 'app_secret': 'other-secret',
        })
        other_page = self.env['meta.page'].create({
            'name': 'Other Page', 'company_id': self.env.company.id,
            'account_id': other.id, 'meta_page_id': '701',
            'page_access_token': 'other-page-token',
        })
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self._new_run(page_ids=[(6, 0, [other_page.id])])

    # 6. Date range is sent as proper filtering JSON.
    def test_date_range_filtering(self):
        run = self._new_run(run_type='leads', date_from='2026-09-01 00:00:00',
                            date_to='2026-09-20 00:00:00')
        import json
        sent = json.loads(run._leads_filtering())
        self.assertEqual([c['field'] for c in sent], ['time_created', 'time_created'])
        self.assertEqual([c['operator'] for c in sent], ['GREATER_THAN', 'LESS_THAN'])

    # 7. Completion notification respects the sync_notify setting.
    def test_notify_muted_for_clean_completion(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'crm_meta_lead_ads.sync_notify', 'issues')
        run = self._new_run()
        run.action_start()
        patcher, _state = self._patch_request(self._all_perms)
        with patcher:
            self._drain(run)
        activities = self.env['mail.activity'].search([
            ('res_model', '=', 'meta.sync.run'), ('res_id', '=', run.id)])
        self.assertFalse(activities)
        self.env['ir.config_parameter'].sudo().set_param(
            'crm_meta_lead_ads.sync_notify', 'always')
        run2 = self._new_run()
        run2.action_start()
        with patcher:
            self._drain(run2)
        activities = self.env['mail.activity'].search([
            ('res_model', '=', 'meta.sync.run'), ('res_id', '=', run2.id)])
        self.assertTrue(activities)

    # 8. Cancel stops further processing.
    def test_cancel(self):
        run = self._new_run()
        run.action_start()
        run.action_cancel()
        self.assertEqual(run.state, 'cancelled')
        with self.assertRaises(UserError):
            run.action_start()
