import importlib.util
import json
import os
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

QUEUE_LOGGER = 'odoo.addons.crm_meta_lead_ads.models.meta_lead_queue'


class TestMetaPolling(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The module's pre-existing forms are unrelated to these isolated
        # polling scenarios. Keep the cron from fetching them as well.
        cls.env['meta.form'].search([('polling_enabled', '=', True)]).write({
            'polling_enabled': False})
        cls.account = cls.env['meta.account'].create({
            'name': 'Polling Meta', 'company_id': cls.env.company.id,
            'app_id': 'app', 'app_secret': 'secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Polling Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '600',
            'page_access_token': 'pagetok-POLL-SECRET',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Polling Form', 'company_id': cls.env.company.id,
            'page_id': cls.page.id, 'meta_form_id': 'F1',
            'last_sync_date': '2026-09-24 10:00:00',
        })
        cls.Queue = cls.env['meta.lead.queue']
        cls.Log = cls.env['meta.lead.log']

    def _patch_request(self, handler):
        """Mock meta.account._request. handler(path, params) returns a
        response dict or an Exception instance to raise. No real HTTP."""
        state = {'calls': []}

        def fake(method, path, **kwargs):
            params = dict(kwargs.get('params') or {})
            state['calls'].append({'method': method, 'path': path, 'params': params})
            result = handler(path, params)
            if isinstance(result, Exception):
                raise result
            return result

        return patch.object(type(self.account), '_request', side_effect=fake), state

    def _page(self, ids, after=None):
        response = {'data': [{'id': i} for i in ids]}
        if after:
            response['paging'] = {'cursors': {'after': after}}
        return response

    def _existing(self, lead_id, company=None):
        return self.Queue.create({
            'company_id': (company or self.env.company).id, 'page_id': self.page.id,
            'meta_lead_id': lead_id,
        })

    def _queued_ids(self, company=None):
        return set(self.Queue.search([
            ('company_id', '=', (company or self.env.company).id),
        ]).mapped('meta_lead_id'))

    # 1. All IDs already queued -> zero creates, zero SQL errors.
    def test_all_existing_creates_nothing_and_logs_no_sql_error(self):
        self._existing('L1')
        self._existing('L2')
        handler = lambda path, params: self._page(['L1', 'L2'])
        patcher, _state = self._patch_request(handler)
        with patcher, self.assertNoLogs('odoo.sql_db', level='ERROR'), \
                self.assertLogs(QUEUE_LOGGER, level='INFO') as logs:
            self.Queue._cron_poll_meta_leads()
        self.assertEqual(self._queued_ids(), {'L1', 'L2'})
        output = '\n'.join(logs.output)
        self.assertIn('existing=2', output)
        self.assertIn('created=0', output)

    # 2. Mixed batch -> creates only the missing IDs.
    def test_mixed_batch_creates_only_new(self):
        self._existing('L1')
        handler = lambda path, params: self._page(['L1', 'L2', 'L3'])
        patcher, _state = self._patch_request(handler)
        with patcher, self.assertNoLogs('odoo.sql_db', level='ERROR'):
            self.Queue._cron_poll_meta_leads()
        self.assertEqual(self._queued_ids(), {'L1', 'L2', 'L3'})
        new = self.Queue.search([('meta_lead_id', '=', 'L2')])
        self.assertEqual(new.form_id, self.form)
        self.assertEqual(new.raw_webhook, {'recovery_poll': True})

    # 3. Duplicate IDs across Graph pages -> created exactly once.
    def test_duplicate_ids_within_pages_create_once(self):
        def handler(path, params):
            if params.get('after') == 'c1':
                return self._page(['L2', 'L3'])
            return self._page(['L1', 'L2'], after='c1')
        patcher, _state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        self.assertEqual(self._queued_ids(), {'L1', 'L2', 'L3'})

    # 4. Same lead ID in two companies stays isolated per company.
    def test_same_id_two_companies_isolated(self):
        company_b = self.env['res.company'].create({'name': 'Polling Co B'})
        account_b = self.env['meta.account'].create({
            'name': 'Polling Meta B', 'company_id': company_b.id,
            'app_id': 'app-b', 'app_secret': 'secret-b',
        })
        page_b = self.env['meta.page'].create({
            'name': 'Polling Page B', 'company_id': company_b.id,
            'account_id': account_b.id, 'meta_page_id': '601',
            'page_access_token': 'token-b',
        })
        self.env['meta.form'].create({
            'name': 'Polling Form B', 'company_id': company_b.id,
            'page_id': page_b.id, 'meta_form_id': 'F1',
        })
        handler = lambda path, params: self._page(['LX'])
        patcher, _state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        rows = self.Queue.search([('meta_lead_id', '=', 'LX')])
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows.mapped('company_id')), {self.env.company, company_b})

    # 5. Stale pre-check race: the competitor row is committed BEFORE the
    #    cycle but hidden from the bulk pre-check, so the create attempt hits
    #    the real unique constraint; the post-rollback verification search
    #    then provably finds the competitor.
    def test_race_after_precheck_falls_back_to_existing(self):
        self._existing('LR')  # competitor row, present before the cycle
        real_search = self.Queue.search

        def stale_precheck_search(args, **kwargs):
            # Hide rows only from the bulk pre-check ('in' leaf), simulating
            # a read taken before the competitor became visible. The
            # post-conflict verification search uses '=' and stays real.
            for leaf in args:
                if (isinstance(leaf, (list, tuple)) and len(leaf) == 3
                        and leaf[0] == 'meta_lead_id' and leaf[1] == 'in'):
                    return self.Queue.browse()
            return real_search(args, **kwargs)

        handler = lambda path, params: self._page(['LR', 'LOK'])
        patcher, _state = self._patch_request(handler)
        with patcher, \
                patch.object(type(self.Queue), 'search', side_effect=stale_precheck_search), \
                self.assertLogs(QUEUE_LOGGER, level='INFO') as logs:
            self.Queue._cron_poll_meta_leads()
        # The conflicting lead provably exists exactly once (the competitor's
        # row; our duplicate insert was rolled back) — not just a counter.
        self.assertEqual(self.Queue.search_count([('meta_lead_id', '=', 'LR')]), 1)
        # The rest of the batch was processed.
        self.assertIn('LOK', self._queued_ids())
        self.assertIn('race=1', '\n'.join(logs.output))
        # The cycle completed normally: no poll_failed and the watermark moved.
        self.assertFalse(self.Log.search([('action', '=', 'poll_failed')]))
        self.form.invalidate_recordset()
        self.assertGreater(self.form.last_sync_date,
                           fields.Datetime.to_datetime('2026-09-24 10:00:00'))

    # 5b. An IntegrityError with NO competing row is re-raised, never swallowed.
    def test_unknown_integrity_error_reraises_not_swallowed(self):
        with patch.object(type(self.Queue), 'create',
                          side_effect=IntegrityError('unexpected constraint failure')):
            with self.assertRaises(IntegrityError):
                self.Queue._enqueue_poll_leads(
                    self.env.company, self.page, self.form, ['L-NOPE'])
        self.assertNotIn('L-NOPE', self._queued_ids())

    # 5c. At cron level an unknown IntegrityError is recorded as poll_failed
    #     (so it is retried next cycle) — never silently dropped.
    def test_unknown_integrity_error_recorded_not_silently_dropped(self):
        handler = lambda path, params: self._page(['L-NOPE'])
        patcher, _state = self._patch_request(handler)
        with patcher, patch.object(type(self.Queue), 'create',
                                   side_effect=IntegrityError('unexpected constraint failure')):
            self.Queue._cron_poll_meta_leads()
        self.assertNotIn('L-NOPE', self._queued_ids())
        log = self.Log.search([('action', '=', 'poll_failed')], limit=1)
        self.assertTrue(log)
        self.assertIn('unexpected constraint failure', log.message or '')

    # 6. Pagination follows paging.cursors.after as a plain parameter.
    def test_pagination_uses_after_cursor(self):
        def handler(path, params):
            after = params.get('after')
            if after == 'c1':
                return self._page(['L2'], after='c2')
            if after == 'c2':
                return self._page(['L3'])
            return self._page(['L1'], after='c1')
        patcher, state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        self.assertEqual(self._queued_ids(), {'L1', 'L2', 'L3'})
        self.assertEqual(len(state['calls']), 3)
        self.assertEqual(state['calls'][1]['params'].get('after'), 'c1')
        self.assertEqual(state['calls'][2]['params'].get('after'), 'c2')
        for call in state['calls']:
            self.assertEqual(call['path'], 'F1/leads')

    # 7. A repeated cursor stops the loop with a single warning.
    def test_repeated_cursor_stops_with_warning(self):
        def handler(path, params):
            if params.get('after') == 'c1':
                return self._page(['L2'], after='c1')
            return self._page(['L1'], after='c1')
        patcher, state = self._patch_request(handler)
        with patcher, self.assertLogs(QUEUE_LOGGER, level='WARNING') as logs:
            self.Queue._cron_poll_meta_leads()
        self.assertEqual(len(state['calls']), 2)
        self.assertIn('repeated pagination cursor', '\n'.join(logs.output))

    # 8. The 10-page limit is enforced with a single warning.
    def test_page_limit_ten(self):
        def handler(path, params):
            after = params.get('after') or 'start'
            return self._page(['L-%s' % after], after='next-of-%s' % after)
        patcher, state = self._patch_request(handler)
        with patcher, self.assertLogs(QUEUE_LOGGER, level='WARNING') as logs:
            self.Queue._cron_poll_meta_leads()
        self.assertEqual(len(state['calls']), 10)
        self.assertIn('page limit', '\n'.join(logs.output))

    # 9. filtering is sent as proper JSON with the 5-minute overlap margin.
    def test_filtering_param_json_with_overlap_margin(self):
        previous_sync = self.form.last_sync_date
        handler = lambda path, params: self._page([])
        patcher, state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        sent = state['calls'][0]['params'].get('filtering')
        self.assertTrue(sent)
        parsed = json.loads(sent)
        expected = int(previous_sync.timestamp()) - 300
        self.assertEqual(parsed, [{
            'field': 'time_created', 'operator': 'GREATER_THAN', 'value': expected,
        }])

    # 10. The stored watermark is the sync start, not the end of the cycle.
    def test_last_sync_date_is_sync_started_at(self):
        self.form.last_sync_date = '2026-09-24 10:00:00'
        request_times = []

        def handler(path, params):
            request_times.append(fields.Datetime.now())
            return self._page(['LT'])
        patcher, _state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        self.form.invalidate_recordset()
        # sync_started_at is captured before the first request, so the stored
        # watermark can never be later than the moment Meta was queried.
        self.assertLessEqual(fields.Datetime.to_datetime(self.form.last_sync_date),
                             fields.Datetime.to_datetime(request_times[0]))
        self.assertGreater(self.form.last_sync_date,
                           fields.Datetime.to_datetime('2026-09-24 10:00:00'))

    # 11. A failing second page does not advance last_sync_date.
    def test_second_page_failure_keeps_last_sync_date(self):
        def handler(path, params):
            if params.get('after') == 'c1':
                return UserError('Meta API error: page exploded')
            return self._page(['L1'], after='c1')
        patcher, _state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        self.form.invalidate_recordset()
        self.assertEqual(fields.Datetime.to_datetime(self.form.last_sync_date),
                         fields.Datetime.to_datetime('2026-09-24 10:00:00'))
        self.assertNotIn('L1', self._queued_ids())

    # 12. One form's failure does not stop the following forms.
    def test_form_failure_does_not_stop_others(self):
        form2 = self.env['meta.form'].create({
            'name': 'Polling Form 2', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': 'F2',
        })

        def handler(path, params):
            if path == 'F1/leads':
                return UserError('Meta API error: broken form')
            if path == 'F2/leads':
                return self._page(['L-OK'])
            raise AssertionError('unexpected path %s' % path)
        patcher, _state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        self.assertIn('L-OK', self._queued_ids())
        form2.invalidate_recordset()
        self.assertGreater(form2.last_sync_date,
                           fields.Datetime.to_datetime('2026-09-24 10:00:00'))
        failed = self.Log.search([('action', '=', 'poll_failed')])
        self.assertTrue(failed)

    # 13. A real Meta failure is still logged as poll_failed.
    def test_meta_failure_still_logged_poll_failed(self):
        handler = lambda path, params: UserError('Meta API error: downtime')
        patcher, _state = self._patch_request(handler)
        with patcher:
            self.Queue._cron_poll_meta_leads()
        log = self.Log.search([('action', '=', 'poll_failed')], limit=1)
        self.assertTrue(log)
        self.assertIn('downtime', log.message or '')
        self.form.invalidate_recordset()
        self.assertEqual(fields.Datetime.to_datetime(self.form.last_sync_date),
                         fields.Datetime.to_datetime('2026-09-24 10:00:00'))

    # 14. Cron migration: 1 hour -> 5 minutes, idempotent.
    def _load_migration(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'migrations', '19.0.3.1.0', 'post-migration.py')
        spec = importlib.util.spec_from_file_location(
            'crm_meta_lead_ads_post_migration_19_0_3_1_0', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cron_migration_sets_5_minutes_idempotent(self):
        cron = self.env.ref('crm_meta_lead_ads.ir_cron_poll_meta_leads')
        cron.write({'interval_number': 1, 'interval_type': 'hours', 'active': False})
        module = self._load_migration()
        module.migrate(self.env.cr, '19.0.3.1.0')
        cron.invalidate_recordset()
        self.assertEqual(cron.interval_number, 5)
        self.assertEqual(cron.interval_type, 'minutes')
        self.assertTrue(cron.active)
        module.migrate(self.env.cr, '19.0.3.1.0')
        cron.invalidate_recordset()
        self.assertEqual((cron.interval_number, cron.interval_type, cron.active),
                         (5, 'minutes', True))

    # 15. No tokens leak into polling logs or errors.
    def test_no_token_leak_in_poll_errors(self):
        class FakeResponse:
            status_code = 400
            content = b'x'
            text = 'bad request pagetok-POLL-SECRET'

            def json(self):
                return {'error': {'code': 100, 'message': 'invalid token pagetok-POLL-SECRET'}}

        with patch(
                'odoo.addons.crm_meta_lead_ads.models.meta_account.requests.request',
                return_value=FakeResponse()):
            self.Queue._cron_poll_meta_leads()
        log = self.Log.search([('action', '=', 'poll_failed')], limit=1)
        self.assertTrue(log)
        self.assertNotIn('pagetok-POLL-SECRET', log.message or '')

    # 15b. Poll errors are sanitized against ALL available secrets even when
    #      the failure does NOT come from _request, and the logger stays clean.
    def test_poll_error_sanitized_for_all_secrets(self):
        self.account.write({
            'user_access_token': 'usertok-LEAK-2', 'app_secret': 'appsecret-LEAK-3'})
        boom = UserError('boom pagetok-POLL-SECRET usertok-LEAK-2 appsecret-LEAK-3')
        with patch.object(type(self.Queue), '_poll_form_leads', side_effect=boom), \
                self.assertNoLogs(QUEUE_LOGGER, level='INFO'):
            self.Queue._cron_poll_meta_leads()
        self.account.write({'user_access_token': False, 'app_secret': 'secret'})
        log = self.Log.search([('action', '=', 'poll_failed')], limit=1)
        self.assertTrue(log)
        for secret in ('pagetok-POLL-SECRET', 'usertok-LEAK-2', 'appsecret-LEAK-3'):
            self.assertNotIn(secret, log.message or '')
