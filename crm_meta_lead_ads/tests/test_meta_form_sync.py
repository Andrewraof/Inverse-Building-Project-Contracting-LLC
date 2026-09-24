from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

PAGE_LOGGER = 'odoo.addons.crm_meta_lead_ads.models.meta_page'

QUESTIONS = [
    {'key': 'full_name', 'label': 'Full name', 'type': 'FULL_NAME', 'id': 'q1'},
    {'key': 'email', 'label': 'Email', 'type': 'EMAIL', 'id': 'q2'},
]


class TestMetaFormSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Sync Meta', 'company_id': cls.env.company.id,
            'app_id': 'app-sync', 'app_secret': 'appsecret-SYNC-LEAK',
            'user_access_token': 'usertok-SYNC-LEAK',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Sync Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '109091031855099',
            'page_access_token': 'pagetok-SYNC-SECRET',
        })
        cls.Form = cls.env['meta.form'].with_context(active_test=False)

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

    def _payload(self, items, after=None):
        response = {'data': items}
        if after:
            response['paging'] = {'cursors': {'after': after}}
        return response

    def _item(self, form_id='893155173379183', name='Interior and exterior',
              status='ACTIVE', questions=None):
        return {
            'id': form_id, 'name': name, 'status': status,
            'questions': QUESTIONS if questions is None else questions,
        }

    def _forms(self, form_id='893155173379183'):
        return self.Form.search([('meta_form_id', '=', form_id)])

    # 1. A new form is created.
    def test_create_new_form(self):
        patcher, _state = self._patch_request(lambda p, pr: self._payload([self._item()]))
        with patcher, self.assertLogs(PAGE_LOGGER, level='INFO') as logs:
            self.page.action_sync_forms()
        form = self._forms()
        self.assertEqual(len(form), 1)
        self.assertEqual(form.name, 'Interior and exterior')
        self.assertTrue(form.active)
        self.assertEqual(form.status, 'ACTIVE')
        self.assertEqual(form.page_id, self.page)
        output = '\n'.join(logs.output)
        self.assertIn('created=1', output)
        self.assertIn('updated=0', output)

    # 2. An existing form is updated, not duplicated.
    def test_update_existing_form(self):
        form = self.Form.create({
            'name': 'Old Name', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '893155173379183',
            'status': 'ACTIVE',
        })
        patcher, _ = self._patch_request(
            lambda p, pr: self._payload([self._item(name='New Name')]))
        with patcher:
            self.page.action_sync_forms()
        self.assertEqual(len(self._forms()), 1)
        form.invalidate_recordset()
        self.assertEqual(form.name, 'New Name')

    # 3. An archived form (active=False) is found and updated — no second row.
    def test_archived_form_found_no_duplicate(self):
        form = self.Form.create({
            'name': 'Archived Hoplon', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '1764444874553752',
            'status': 'ARCHIVED', 'active': False,
        })
        patcher, _ = self._patch_request(lambda p, pr: self._payload([
            self._item(form_id='1764444874553752', name='Archived Hoplon v2', status='ARCHIVED'),
        ]))
        with patcher, self.assertNoLogs('odoo.sql_db', level='ERROR'):
            self.page.action_sync_forms()
        rows = self._forms('1764444874553752')
        self.assertEqual(len(rows), 1)
        form.invalidate_recordset()
        self.assertEqual(form.name, 'Archived Hoplon v2')

    # 4. A form Meta reports as ARCHIVED stays/becomes archived.
    def test_archived_status_archives_form(self):
        form = self.Form.create({
            'name': 'F', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '893155173379183',
            'status': 'ACTIVE',
        })
        patcher, _ = self._patch_request(
            lambda p, pr: self._payload([self._item(status='ARCHIVED')]))
        with patcher, self.assertLogs(PAGE_LOGGER, level='INFO') as logs:
            self.page.action_sync_forms()
        form.invalidate_recordset()
        self.assertFalse(form.active)
        self.assertEqual(form.status, 'ARCHIVED')
        self.assertIn('archived=1', '\n'.join(logs.output))

    # 5. The same record is reactivated when Meta flips ARCHIVED -> ACTIVE.
    def test_reactivation_of_archived_form(self):
        form = self.Form.create({
            'name': 'F', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '893155173379183',
            'status': 'ARCHIVED', 'active': False,
        })
        patcher, _ = self._patch_request(
            lambda p, pr: self._payload([self._item(status='ACTIVE')]))
        with patcher:
            self.page.action_sync_forms()
        self.assertEqual(len(self._forms()), 1)
        form.invalidate_recordset()
        self.assertTrue(form.active)
        self.assertEqual(form.status, 'ACTIVE')

    # 6. Manual configuration survives a sync.
    def test_manual_settings_preserved(self):
        team = self.env['crm.team'].create({'name': 'Sync Team'})
        form = self.Form.create({
            'name': 'F', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '893155173379183',
            'sales_team_id': team.id, 'user_id': self.env.user.id,
            'lead_type': 'opportunity', 'polling_enabled': False,
        })
        mapping = self.env['meta.form.mapping'].create({
            'form_id': form.id, 'meta_field_name': 'email', 'odoo_field_name': 'email_from',
        })
        patcher, _ = self._patch_request(
            lambda p, pr: self._payload([self._item(name='Renamed')]))
        with patcher:
            self.page.action_sync_forms()
        form.invalidate_recordset()
        self.assertEqual(form.name, 'Renamed')
        self.assertEqual(form.sales_team_id, team)
        self.assertEqual(form.user_id, self.env.user)
        self.assertEqual(form.lead_type, 'opportunity')
        self.assertFalse(form.polling_enabled)
        self.assertTrue(mapping.exists())

    # 7. The polling watermark last_sync_date is never touched by form sync.
    def test_last_sync_date_untouched(self):
        form = self.Form.create({
            'name': 'F', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '893155173379183',
            'last_sync_date': '2026-09-24 10:00:00',
        })
        patcher, _ = self._patch_request(lambda p, pr: self._payload([self._item()]))
        with patcher:
            self.page.action_sync_forms()
        form.invalidate_recordset()
        self.assertEqual(fields.Datetime.to_datetime(form.last_sync_date),
                         fields.Datetime.to_datetime('2026-09-24 10:00:00'))

    # 8. Company isolation: the same Meta form ID may exist in two companies;
    #    only the record of the synced page's company is updated.
    def test_company_isolation(self):
        company_b = self.env['res.company'].create({'name': 'Sync Co B'})
        page_b = self.env['meta.page'].create({
            'name': 'Sync Page B', 'company_id': company_b.id,
            'account_id': self.account.id, 'meta_page_id': '109091031855099',
            'page_access_token': 'token-b',
        })
        form_a = self.Form.create({
            'name': 'Form A', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '893155173379183',
        })
        form_b = self.Form.create({
            'name': 'Form B', 'company_id': company_b.id,
            'page_id': page_b.id, 'meta_form_id': '893155173379183',
        })
        patcher, _ = self._patch_request(
            lambda p, pr: self._payload([self._item(name='Form A updated')]))
        with patcher:
            self.page.action_sync_forms()
        form_a.invalidate_recordset()
        form_b.invalidate_recordset()
        self.assertEqual(form_a.name, 'Form A updated')
        self.assertEqual(form_b.name, 'Form B')

    # 9. Race: a competitor row inserted between the pre-check and the create
    #    is re-fetched after the unique-constraint hit and updated in place.
    def test_race_reuses_competitor_row(self):
        competitor = self.Form.create({
            'name': 'Competitor', 'company_id': self.env.company.id,
            'page_id': self.page.id, 'meta_form_id': '893155173379183',
        })
        real_search = self.Form.search
        seen = {'n': 0}

        def stale_precheck_search(args, **kwargs):
            # Hide the competitor only from the first sync pre-check,
            # simulating a read taken before the competitor became visible.
            # The post-conflict verification search stays real.
            for leaf in args:
                if (isinstance(leaf, (list, tuple)) and len(leaf) == 3
                        and leaf[0] == 'meta_form_id'):
                    seen['n'] += 1
                    if seen['n'] == 1:
                        return self.Form.browse()
            return real_search(args, **kwargs)

        patcher, _ = self._patch_request(
            lambda p, pr: self._payload([self._item(name='Race Winner')]))
        with patcher, \
                patch.object(type(self.Form), 'search', side_effect=stale_precheck_search), \
                self.assertLogs(PAGE_LOGGER, level='INFO') as logs:
            self.page.action_sync_forms()
        # The conflicting form provably exists exactly once (the competitor's
        # row; our duplicate insert was rolled back) — not just a counter.
        self.assertEqual(len(self._forms()), 1)
        competitor.invalidate_recordset()
        self.assertEqual(competitor.name, 'Race Winner')
        output = '\n'.join(logs.output)
        self.assertIn('created=0', output)
        self.assertIn('updated=1', output)

    # 10. An IntegrityError with NO competing row is re-raised, never swallowed.
    def test_unknown_integrity_error_reraises_not_swallowed(self):
        patcher, _ = self._patch_request(lambda p, pr: self._payload([self._item()]))
        with patcher, patch.object(type(self.Form), 'create',
                                   side_effect=IntegrityError('unexpected constraint failure')):
            with self.assertRaises(IntegrityError):
                self.page.action_sync_forms()

    # 11. Pagination follows paging.cursors.after as a plain parameter on the
    #     same endpoint — paging.next is never used as a URL.
    def test_pagination_uses_after_cursor(self):
        def handler(path, params):
            after = params.get('after')
            if after == 'c1':
                return self._payload([self._item(form_id='F2')], after='c2')
            if after == 'c2':
                return self._payload([self._item(form_id='F3')])
            return self._payload([self._item(form_id='F1')], after='c1')
        patcher, state = self._patch_request(handler)
        with patcher:
            self.page.action_sync_forms()
        self.assertEqual(len(state['calls']), 3)
        self.assertEqual(state['calls'][1]['params'].get('after'), 'c1')
        self.assertEqual(state['calls'][2]['params'].get('after'), 'c2')
        for call in state['calls']:
            self.assertEqual(call['path'], '109091031855099/leadgen_forms')
        self.assertEqual(len(self.Form.search([('meta_form_id', 'in', ['F1', 'F2', 'F3'])])), 3)

    # 12. A repeated cursor stops the loop with a single warning.
    def test_repeated_cursor_stops_with_warning(self):
        def handler(path, params):
            if params.get('after') == 'c1':
                return self._payload([self._item(form_id='F2')], after='c1')
            return self._payload([self._item(form_id='F1')], after='c1')
        patcher, state = self._patch_request(handler)
        with patcher, self.assertLogs(PAGE_LOGGER, level='WARNING') as logs:
            self.page.action_sync_forms()
        self.assertEqual(len(state['calls']), 2)
        self.assertIn('repeated pagination cursor', '\n'.join(logs.output))

    # 13. The 10-page limit is enforced with a single warning.
    def test_page_limit_ten(self):
        def handler(path, params):
            after = params.get('after') or 'start'
            return self._payload([self._item(form_id='F-%s' % after)], after='next-of-%s' % after)
        patcher, state = self._patch_request(handler)
        with patcher, self.assertLogs(PAGE_LOGGER, level='WARNING') as logs:
            self.page.action_sync_forms()
        self.assertEqual(len(state['calls']), 10)
        self.assertIn('page limit', '\n'.join(logs.output))

    # 14. Sync errors never leak the Page token, User token, or App Secret.
    def test_no_secret_leak_in_sync_errors(self):
        boom = UserError('boom pagetok-SYNC-SECRET usertok-SYNC-LEAK appsecret-SYNC-LEAK')
        patcher, _ = self._patch_request(lambda p, pr: boom)
        with patcher, self.assertRaises(UserError) as ctx:
            self.page.action_sync_forms()
        message = str(ctx.exception)
        for secret in ('pagetok-SYNC-SECRET', 'usertok-SYNC-LEAK', 'appsecret-SYNC-LEAK'):
            self.assertNotIn(secret, message)

    # 15. Regression: default mapping generation still works after a sync.
    def test_generate_default_mappings_regression(self):
        patcher, _ = self._patch_request(lambda p, pr: self._payload([self._item()]))
        with patcher:
            self.page.action_sync_forms()
        form = self._forms()
        form.action_generate_default_mappings()
        mapped = set(form.mapping_ids.mapped('meta_field_name'))
        self.assertIn('full_name', mapped)
        self.assertIn('email', mapped)

    # 16. Running the sync twice is idempotent: no extra rows, second run
    #     updates in place.
    def test_sync_twice_idempotent(self):
        patcher, _ = self._patch_request(lambda p, pr: self._payload([self._item()]))
        with patcher:
            self.page.action_sync_forms()
        with patcher, self.assertLogs(PAGE_LOGGER, level='INFO') as logs:
            self.page.action_sync_forms()
        self.assertEqual(len(self._forms()), 1)
        output = '\n'.join(logs.output)
        self.assertIn('created=0', output)
        self.assertIn('updated=1', output)
