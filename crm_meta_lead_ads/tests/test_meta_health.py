import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestMetaLiveWebhookEvidence(TransactionCase):
    """Removing the signed-event receipt hook must break these tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Health Account', 'company_id': cls.env.company.id,
            'app_id': 'health-app', 'app_secret': 'health-secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Health Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': 'health-page-1',
            'page_access_token': 'health-page-token',
        })
        cls.other_account = cls.env['meta.account'].create({
            'name': 'Other Account', 'company_id': cls.env.company.id,
            'app_id': 'other-app', 'app_secret': 'other-secret',
        })

    def _receive(self, payload, account=None, valid_signature=True):
        from odoo.addons.crm_meta_lead_ads.controllers.main import MetaLeadController

        secret = (account or self.account).app_secret
        raw = json.dumps(payload).encode()
        signature = 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not valid_signature:
            signature = 'sha256=' + '0' * 64
        with patch('odoo.addons.crm_meta_lead_ads.controllers.main.request',
                   new=MagicMock()) as fake_request:
            fake_request.env = self.env
            fake_request.httprequest.get_data.return_value = raw
            fake_request.httprequest.headers.get.return_value = signature
            fake_request.httprequest.get_json.return_value = payload
            fake_request.make_response.side_effect = (
                lambda body, status=200, headers=None: (body, status))
            return MetaLeadController()._receive_webhook(secret, account=account)

    def _lead_payload(self, page_id='health-page-1', lead_id='lead-health-1'):
        return {'object': 'page', 'entry': [{
            'id': page_id,
            'changes': [{'field': 'leadgen', 'value': {
                'page_id': page_id, 'leadgen_id': lead_id}}],
        }]}

    def test_signed_lead_records_receipt_and_enqueue_success(self):
        self.assertEqual(self._receive(self._lead_payload(), self.account)[1], 200)
        self.page.invalidate_recordset()
        self.assertTrue(self.page.last_live_webhook_at)
        self.assertTrue(self.page.last_live_lead_webhook_at)
        self.assertTrue(self.page.last_live_lead_enqueued_at)
        self.assertEqual(self.env['meta.lead.queue'].search_count([
            ('company_id', '=', self.env.company.id),
            ('meta_lead_id', '=', 'lead-health-1')]), 1)

    def test_bad_signature_and_unknown_page_do_not_record_receipt(self):
        self.assertEqual(self._receive(self._lead_payload(), self.account,
                                       valid_signature=False)[1], 403)
        self.assertEqual(self._receive(self._lead_payload('no-such-page'),
                                       self.account)[1], 200)
        self.page.invalidate_recordset()
        self.assertFalse(self.page.last_live_webhook_at)

    def test_account_specific_webhook_does_not_touch_other_account_page(self):
        self.assertEqual(self._receive(self._lead_payload(),
                                       self.other_account)[1], 200)
        self.page.invalidate_recordset()
        self.assertFalse(self.page.last_live_webhook_at)
        self.assertFalse(self.env['meta.lead.queue'].search([
            ('meta_lead_id', '=', 'lead-health-1')]))

    def test_enqueue_failure_keeps_receipt_without_success(self):
        with patch.object(type(self.env['meta.lead.queue']), 'enqueue_event',
                          side_effect=RuntimeError('forced queue failure')):
            self.assertEqual(self._receive(self._lead_payload(),
                                           self.account)[1], 200)
        self.page.invalidate_recordset()
        self.assertTrue(self.page.last_live_lead_webhook_at)
        self.assertFalse(self.page.last_live_lead_enqueued_at)

    def test_signed_message_records_receipt_and_success(self):
        payload = {'object': 'page', 'entry': [{
            'id': self.page.meta_page_id, 'messaging': [{
                'sender': {'id': 'health-test-psid'},
                'recipient': {'id': self.page.meta_page_id},
                'message': {'mid': 'health-message-1', 'text': 'test'},
            }],
        }]}
        with patch.object(type(self.page), '_fetch_sender_name', return_value='Test Sender'):
            self.assertEqual(self._receive(payload, self.account)[1], 200)
        self.page.invalidate_recordset()
        self.assertTrue(self.page.last_live_message_webhook_at)
        self.assertTrue(self.page.last_live_message_recorded_at)

    def test_repeat_signed_lead_keeps_single_queue_record(self):
        payload = self._lead_payload(lead_id='health-repeat-1')
        self.assertEqual(self._receive(payload, self.account)[1], 200)
        self.assertEqual(self._receive(payload, self.account)[1], 200)
        self.assertEqual(self.env['meta.lead.queue'].search_count([
            ('company_id', '=', self.env.company.id),
            ('meta_lead_id', '=', 'health-repeat-1')]), 1)
        self.assertTrue(self.page.last_live_lead_webhook_at)

    def test_historical_message_import_is_not_live_webhook_evidence(self):
        conv = self.env['meta.conversation']._get_or_create(
            self.page, 'history-health-psid')
        run = self.env['meta.sync.run'].create({
            'account_id': self.account.id, 'company_id': self.env.company.id,
            'run_type': 'messages',
        })
        self.assertEqual(run._upsert_history_message(conv, self.page, {
            'id': 'history-health-mid',
            'from': {'id': 'history-health-psid', 'name': 'Historical Contact'},
            'message': 'prior message',
        }), 'created')
        self.page.invalidate_recordset()
        self.assertFalse(self.page.last_live_webhook_at)
        self.assertFalse(self.page.last_live_message_recorded_at)


class TestMetaDiagnostics(TransactionCase):
    """Wrong success banners or stale scope evidence must break these tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Diagnostic Account', 'company_id': cls.env.company.id,
            'app_id': 'diag-app', 'app_secret': 'diag-secret',
            'user_access_token': 'diag-user-token',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Diagnostic Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': 'diag-page',
            'page_access_token': 'diag-page-token',
        })

    def _run(self, connection_error=False, scopes=None, subscription_fields=None,
             permission_error=False):
        if scopes is None:
            scopes = ('leads_retrieval', 'pages_show_list',
                      'pages_read_engagement', 'pages_manage_metadata',
                      'pages_messaging')
        if subscription_fields is None:
            subscription_fields = ('leadgen', 'messages', 'messaging_postbacks')

        def request(method, path, **kwargs):
            if path == 'me':
                if connection_error:
                    raise UserError('connection failed diag-user-token')
                return {'id': 'diag-user'}
            if path == 'me/permissions':
                if permission_error:
                    raise UserError('permission check failed diag-user-token')
                return {'data': [{'name': name, 'status': 'granted'}
                                 for name in scopes]}
            if path == 'diag-page/subscribed_apps':
                return {'data': [{'id': self.account.app_id,
                                  'subscribed_fields': list(subscription_fields)}]}
            raise AssertionError('unexpected Graph path %s' % path)

        with patch.object(type(self.account), '_request', side_effect=request):
            return self.account.action_run_diagnostics()

    def test_failed_connection_never_shows_success(self):
        action = self._run(connection_error=True)
        self.assertEqual(action['params']['type'], 'danger')
        self.account.invalidate_recordset()
        self.assertEqual(self.account.diagnostic_status, 'failure')
        self.assertNotIn('diag-user-token', self.account.error_message or '')

    def test_incomplete_subscription_shows_warning(self):
        action = self._run(subscription_fields=('leadgen',))
        self.assertEqual(action['params']['type'], 'warning')
        self.assertEqual(self.account.diagnostic_status, 'warning')
        self.assertEqual(self.page.subscription_status, 'incomplete')

    def test_unknown_permissions_clear_stale_evidence(self):
        self.account.write({
            'granted_permissions': 'pages_messaging',
            'missing_permissions': 'leads_retrieval',
            'permissions_checked_at': fields.Datetime.now(),
        })
        action = self._run(permission_error=True)
        self.account.invalidate_recordset()
        self.assertFalse(self.account.permissions_checked_at)
        self.assertFalse(self.account.granted_permissions)
        self.assertFalse(self.account.missing_permissions)
        self.assertEqual(self.account.diagnostic_status, 'unknown')
        self.assertEqual(action['params']['type'], 'info')

    def test_empty_permissions_are_unknown_not_denied(self):
        action = self._run(scopes=())
        self.assertEqual(self.account.diagnostic_status, 'unknown')
        self.assertFalse(self.account.missing_permissions)
        self.assertEqual(action['params']['type'], 'info')

    def test_complete_diagnostics_show_success(self):
        action = self._run()
        self.assertEqual(self.account.diagnostic_status, 'success')
        self.assertTrue(self.account.diagnostic_checked_at)
        self.assertEqual(action['params']['type'], 'success')
