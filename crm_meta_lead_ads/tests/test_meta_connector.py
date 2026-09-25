from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase
from odoo.addons.crm_meta_lead_ads.controllers.compliance import PRIVACY_POLICY_HTML


class TestMetaConnector(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Test Meta', 'company_id': cls.env.company.id,
            'app_id': 'app', 'app_secret': 'secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Test Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '100', 'page_access_token': 'token',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Test Form', 'company_id': cls.env.company.id,
            'page_id': cls.page.id, 'meta_form_id': '200',
        })

    def test_queue_idempotency(self):
        q1 = self.env['meta.lead.queue'].enqueue_event(self.env.company, self.page, 'L1', self.form.meta_form_id, {})
        q2 = self.env['meta.lead.queue'].enqueue_event(self.env.company, self.page, 'L1', self.form.meta_form_id, {})
        self.assertEqual(q1.id, q2.id)

    def test_privacy_policy_contains_required_disclosures(self):
        self.assertIn('Privacy Policy - Inverse Group', PRIVACY_POLICY_HTML)
        self.assertIn('Information we collect', PRIVACY_POLICY_HTML)
        self.assertIn('do not sell personal information', PRIVACY_POLICY_HTML)
        self.assertIn('/contactus', PRIVACY_POLICY_HTML)

    def test_default_mapping_generation(self):
        self.form.action_generate_default_mappings()
        self.assertTrue(self.form.mapping_ids.filtered(lambda m: m.meta_field_name == 'email' and m.odoo_field_name == 'email_from'))

    def _upsert_page(self, data, account=None):
        return self.env['meta.page']._upsert_from_meta(
            self.env.company, account or self.account, data)

    def _db_token(self, page_id):
        """Read the token straight from the cursor, bypassing the ORM cache."""
        self.env['meta.page'].flush_model(['page_access_token'])
        self.env.cr.execute("SELECT page_access_token FROM meta_page WHERE id = %s", (page_id,))
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _plant_legacy_bad_token(self):
        # Simulate legacy data written before token validation existed.
        self.env.cr.execute(
            "UPDATE meta_page SET page_access_token = %s WHERE id = %s",
            (self.page.meta_page_id, self.page.id))
        self.page.invalidate_recordset(['page_access_token'])

    def test_page_upsert_creates_new_page(self):
        page = self._upsert_page({'id': '300', 'name': 'New Page', 'access_token': 'tok300', 'tasks': ['MANAGE']})
        self.assertTrue(page.exists())
        self.assertNotEqual(page.id, self.page.id)
        self.assertEqual(page.meta_page_id, '300')
        self.assertEqual(page.page_access_token, 'tok300')
        self.assertEqual(page.page_permissions, 'MANAGE')

    def test_page_upsert_updates_existing_page(self):
        account2 = self.env['meta.account'].create({
            'name': 'Second Meta', 'company_id': self.env.company.id,
            'app_id': 'app2', 'app_secret': 'secret2',
        })
        page = self._upsert_page(
            {'id': '100', 'name': 'Renamed Page', 'access_token': 'newtoken', 'tasks': ['MANAGE', 'ADVERTISE']},
            account=account2)
        self.assertEqual(page.id, self.page.id)
        self.assertEqual(page.name, 'Renamed Page')
        self.assertEqual(page.account_id, account2)
        self.assertEqual(page.page_access_token, 'newtoken')
        self.assertEqual(page.page_permissions, 'MANAGE,ADVERTISE')

    def test_page_upsert_reactivates_archived_page(self):
        self.page.write({'active': False, 'sync_enabled': False})
        page = self._upsert_page({'id': '100', 'name': 'Test Page', 'access_token': 'tok100'})
        self.assertEqual(page.id, self.page.id)
        self.assertTrue(page.active)
        self.assertTrue(page.sync_enabled)

    def test_page_upsert_keeps_token_when_meta_returns_none(self):
        page = self._upsert_page({'id': '100', 'name': 'Test Page'})
        self.assertEqual(page.id, self.page.id)
        self.assertEqual(page.page_access_token, 'token')

    def test_page_upsert_rejects_page_id_as_token(self):
        with self.assertRaises(UserError):
            self._upsert_page({'id': '400', 'name': 'No Token Page', 'access_token': '400'})
        self.assertFalse(self.env['meta.page'].search([('meta_page_id', '=', '400')]))

    def test_page_upsert_new_page_without_token_fails_clearly(self):
        with self.assertRaises(UserError):
            self._upsert_page({'id': '500', 'name': 'No Token Page'})
        self.assertFalse(self.env['meta.page'].search([('meta_page_id', '=', '500')]))

    def test_constraint_blocks_page_id_as_token_on_create(self):
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.env['meta.page'].create({
                    'name': 'Bad Page', 'company_id': self.env.company.id,
                    'account_id': self.account.id, 'meta_page_id': '700', 'page_access_token': '700',
                })
        self.assertFalse(self.env['meta.page'].search([('meta_page_id', '=', '700')]))

    def test_constraint_blocks_page_id_as_token_on_write(self):
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.page.write({'page_access_token': '100'})
        self.assertEqual(self._db_token(self.page.id), 'token')

    def test_constraint_blocks_empty_token_on_active_page(self):
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.page.write({'page_access_token': False})
        self.assertEqual(self._db_token(self.page.id), 'token')

    def test_constraint_allows_empty_token_on_inactive_page(self):
        self.page.write({'active': False, 'sync_enabled': False, 'page_access_token': False})
        self.assertFalse(self._db_token(self.page.id))

    def test_legacy_bad_token_not_synced_without_new_token(self):
        self._plant_legacy_bad_token()
        with self.assertRaises(UserError):
            self._upsert_page({'id': '100', 'name': 'Test Page'})
        self.assertEqual(self._db_token(self.page.id), '100')

    def test_legacy_bad_token_replaced_by_valid_token(self):
        self._plant_legacy_bad_token()
        page = self._upsert_page({'id': '100', 'name': 'Test Page', 'access_token': 'tok100'})
        self.assertEqual(page.id, self.page.id)
        self.assertEqual(self._db_token(self.page.id), 'tok100')

    def test_callback_sync_continues_after_page_failure(self):
        Page = self.env['meta.page']
        pages_data = [
            {'id': '600', 'name': 'Good Page 1', 'access_token': 'tok600'},
            {'id': '601', 'name': 'Bad Page', 'access_token': '601'},
            {'id': '602', 'name': 'Good Page 2', 'access_token': 'tok602'},
        ]
        synced, failed, results = Page._upsert_pages_bulk(self.env.company, self.account, pages_data)
        self.assertEqual(synced, 2)
        self.assertEqual(failed, 1)
        self.assertEqual(len(Page.search([('meta_page_id', 'in', ['600', '602'])])), 2)
        self.assertFalse(Page.search([('meta_page_id', '=', '601')]))
        self.assertTrue(any(status.startswith('failed') for _, status in results))

    def test_disconnect_clears_tokens_and_reconnect_recreates(self):
        self.account.write({'user_access_token': 'usertok'})
        self.account.action_disconnect()
        self.assertEqual(self.account.state, 'disconnected')
        self.assertFalse(self.account.user_access_token)
        self.assertFalse(self.page.active)
        self.assertFalse(self.page.sync_enabled)
        self.assertFalse(self._db_token(self.page.id))
        page = self._upsert_page({'id': '100', 'name': 'Test Page', 'access_token': 'tok100'})
        self.assertEqual(page.id, self.page.id)
        self.assertTrue(page.active)
        self.assertTrue(page.sync_enabled)
        self.assertEqual(self._db_token(self.page.id), 'tok100')

    def test_error_messages_do_not_expose_token(self):
        secret = 'secretXYZ123'
        page = self._upsert_page({'id': '900', 'name': 'Secret Page', 'access_token': secret})
        with self.assertRaises(ValidationError) as ctx:
            with self.env.cr.savepoint():
                page.write({'page_access_token': '900'})
        page.invalidate_recordset()
        self.assertNotIn(secret, str(ctx.exception))
        msg = self.account._sanitize_error(
            'Connection error for url: /v25.0/me?access_token=%s (timeout)' % secret,
            token=secret)
        self.assertNotIn(secret, msg)

    def test_secrets_never_leak_into_errors_or_results(self):
        secret = 'tok-leak-check-123'
        self.account.write({'user_access_token': 'user-secret-456'})
        request_path = 'odoo.addons.crm_meta_lead_ads.models.meta_account.requests.request'

        class FakeResp:
            status_code = 400
            content = b'{}'
            text = 'error payload'

            def json(self):
                return {'error': {'code': 190, 'error_subcode': 463,
                                  'message': 'Invalid token: %s' % secret}}

        # 1) Graph JSON error: the token must not reach the UserError,
        #    error_message, or the scheduled activity.
        with patch(request_path, return_value=FakeResp()), \
                patch.object(type(self.account), 'activity_schedule') as schedule:
            with self.assertRaises(UserError) as ctx:
                self.account._request('GET', 'me', token=secret)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(secret, self.account.error_message or '')
        schedule.assert_called_once()
        self.assertNotIn(secret, str(schedule.call_args))

        # 2) Connection-style exception carrying the token in its URL.
        with patch(request_path, side_effect=Exception('url: /me?access_token=%s' % secret)):
            with self.assertRaises(UserError) as ctx2:
                self.account._request('GET', 'me', token=secret)
        self.assertNotIn(secret, str(ctx2.exception))

        # 3) Bulk sync results must not leak the token either.
        Page = self.env['meta.page']
        with patch.object(type(Page), '_upsert_from_meta',
                          side_effect=Exception('boom %s' % secret)):
            synced, failed, results = Page._upsert_pages_bulk(
                self.env.company, self.account, [{'id': '600', 'name': 'P600', 'access_token': secret}])
        self.assertEqual((synced, failed), (0, 1))
        self.assertNotIn(secret, str(results))
