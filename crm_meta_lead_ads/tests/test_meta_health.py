import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
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


class TestMetaHealthSnapshot(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Health Snapshot', 'company_id': cls.env.company.id,
            'app_id': 'snapshot-app', 'app_secret': 'snapshot-secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Snapshot Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': 'snapshot-page',
            'page_access_token': 'snapshot-token',
        })
        cls.other = cls.env['meta.account'].create({
            'name': 'Other Snapshot', 'company_id': cls.env.company.id,
            'app_id': 'other-snapshot-app', 'app_secret': 'other-snapshot-secret',
        })
        cls.other_page = cls.env['meta.page'].create({
            'name': 'Other Page', 'company_id': cls.env.company.id,
            'account_id': cls.other.id, 'meta_page_id': 'other-snapshot-page',
            'page_access_token': 'other-snapshot-token',
        })

    def _queue(self, page, lead_id, state, at, retry=None):
        return self.env['meta.lead.queue'].create({
            'company_id': page.company_id.id, 'page_id': page.id,
            'meta_lead_id': lead_id, 'state': state,
            'received_at': at, 'next_retry_at': retry,
        })

    def test_disabled_and_quiet_unknown_without_silence_policy(self):
        now = fields.Datetime.now()
        snap = self.account._health_snapshot(now)
        self.assertEqual(snap['level'], 'disabled')
        self.assertEqual(snap['issues'], [])
        self.account.write({'health_monitor_enabled': True})
        snap = self.account._health_snapshot(now)
        self.assertEqual(snap['level'], 'unknown')
        self.assertNotIn('webhook_silence', snap['issues'])

    def test_due_jobs_exclude_future_retries_and_other_account(self):
        now = fields.Datetime.now()
        old = now - timedelta(minutes=31)
        self.account.write({'health_monitor_enabled': True})
        self._queue(self.page, 'health-pending', 'pending', old)
        self._queue(self.page, 'health-retry-future', 'retry', old,
                    now + timedelta(hours=1))
        self._queue(self.page, 'health-processing', 'processing', old)
        self._queue(self.other_page, 'other-pending', 'pending', old)
        snap = self.account._health_snapshot(now)
        self.assertEqual(snap['due_queue_count'], 2)
        self.assertGreaterEqual(snap['oldest_due_minutes'], 30)
        self.assertIn('queue_overdue', snap['issues'])

    def test_failed_ambiguous_and_outbound_are_account_scoped(self):
        now = fields.Datetime.now()
        self.account.write({'health_monitor_enabled': True})
        self._queue(self.page, 'health-failed', 'failed', now)
        self._queue(self.page, 'health-ambiguous', 'ambiguous', now)
        self._queue(self.other_page, 'other-failed', 'failed', now)
        self.env['meta.message'].create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'sender_psid': 'health-psid', 'meta_message_id': 'health-failed-out',
            'direction': 'outbound', 'send_state': 'failed', 'received_at': now,
        })
        self.env['meta.message'].create({
            'company_id': self.env.company.id, 'page_id': self.other_page.id,
            'sender_psid': 'other-psid', 'meta_message_id': 'other-failed-out',
            'direction': 'outbound', 'send_state': 'failed', 'received_at': now,
        })
        snap = self.account._health_snapshot(now)
        self.assertEqual(snap['failed_queue_count'], 1)
        self.assertEqual(snap['ambiguous_queue_count'], 1)
        self.assertEqual(snap['failed_outbound_24h_count'], 1)
        self.assertIn('queue_failed', snap['issues'])
        self.assertIn('queue_ambiguous', snap['issues'])
        self.assertIn('outbound_failed', snap['issues'])

    def test_known_expiry_and_stale_subscription_not_current_failure(self):
        now = fields.Datetime.now()
        self.account.write({'health_monitor_enabled': True,
                            'token_expires_at': now + timedelta(days=2)})
        self.page.write({'subscription_status': 'failed',
                         'subscription_checked_at': now - timedelta(days=2)})
        snap = self.account._health_snapshot(now)
        self.assertIn('token_expiring', snap['issues'])
        self.assertIn('subscription_stale', snap['issues'])
        self.assertNotIn('subscription_failed', snap['issues'])

    def test_optional_silence_starts_at_enablement_not_old_history(self):
        now = fields.Datetime.now()
        self.account.write({'health_monitor_enabled': True,
                            'health_silence_minutes': 60,
                            'health_enabled_at': now - timedelta(minutes=90)})
        self.assertIn('webhook_silence', self.account._health_snapshot(now)['issues'])
        self.page.last_live_webhook_at = now - timedelta(minutes=10)
        self.assertNotIn('webhook_silence', self.account._health_snapshot(now)['issues'])

    def test_archived_page_and_other_company_are_excluded(self):
        now = fields.Datetime.now()
        old = now - timedelta(hours=1)
        foreign = self.env['res.company'].create({'name': 'Health Foreign Company'})
        foreign_account = self.env['meta.account'].create({
            'name': 'Foreign Account', 'company_id': foreign.id,
            'app_id': 'foreign-health-app', 'app_secret': 'foreign-secret',
        })
        foreign_page = self.env['meta.page'].with_company(foreign).create({
            'name': 'Foreign Page', 'company_id': foreign.id,
            'account_id': foreign_account.id, 'meta_page_id': 'foreign-page',
            'page_access_token': 'foreign-token',
        })
        self._queue(foreign_page, 'foreign-old', 'pending', old)
        self._queue(self.page, 'archived-old', 'pending', old)
        self.page.active = False
        self.account.health_monitor_enabled = True
        snap = self.account._health_snapshot(now)
        self.assertEqual(snap['page_count'], 0)
        self.assertEqual(snap['due_queue_count'], 0)

    def test_owner_requires_active_internal_manager_with_company_access(self):
        group = self.env.ref('crm_meta_lead_ads.group_meta_lead_manager')
        user = self.env['res.users'].create({
            'name': 'Health Manager', 'login': 'health_manager_snapshot',
            'company_id': self.env.company.id,
            'company_ids': [(6, 0, [self.env.company.id])],
            'group_ids': [(6, 0, [group.id,
                                   self.env.ref('base.group_user').id])],
        })
        self.account.health_owner_id = user
        user.active = False
        self.assertFalse(self.account._health_owner_eligible())
        with self.assertRaises(ValidationError):
            self.account._check_health_settings()


class TestMetaHealthAlerts(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = cls.env.ref('crm_meta_lead_ads.group_meta_lead_manager')
        cls.manager = cls.env['res.users'].create({
            'name': 'Health Alert Manager', 'login': 'health_alert_manager',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, [cls.env.company.id])],
            'group_ids': [(6, 0, [group.id,
                                   cls.env.ref('base.group_user').id])],
        })
        cls.account = cls.env['meta.account'].create({
            'name': 'Health Alerts', 'company_id': cls.env.company.id,
            'app_id': 'alert-app', 'app_secret': 'alert-secret',
            'health_monitor_enabled': True, 'health_owner_id': cls.manager.id,
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Alert Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': 'alert-page',
            'page_access_token': 'alert-page-token',
        })

    def _alerts(self):
        return self.env['meta.health.alert'].search([
            ('account_id', '=', self.account.id)])

    def _activities(self, alert):
        return self.env['mail.activity'].search([
            ('res_model_id', '=', self.env['ir.model']._get_id('meta.health.alert')),
            ('res_id', '=', alert.id),
            ('activity_type_id', '=', self.env.ref(
                'crm_meta_lead_ads.mail_activity_type_meta_health').id),
        ])

    def test_repeated_assessment_acknowledge_recovery_and_recurrence(self):
        now = fields.Datetime.now()
        self.account.state = 'error'
        self.account._cron_assess_health(limit=5, now=now)
        alert = self._alerts().filtered(lambda a: a.check_code == 'account_disconnected')
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.state, 'open')
        self.assertEqual(len(self._activities(alert)), 1)
        self.account._cron_assess_health(limit=5, now=now + timedelta(minutes=5))
        self.assertEqual(len(self._alerts().filtered(
            lambda a: a.check_code == 'account_disconnected')), 1)
        self.assertEqual(len(self._activities(alert)), 1)
        alert.with_user(self.manager).action_acknowledge()
        self.assertEqual(alert.state, 'acknowledged')
        self.account._cron_assess_health(limit=5, now=now + timedelta(minutes=10))
        self.assertEqual(alert.state, 'acknowledged')
        self.assertEqual(len(self._activities(alert)), 1)
        self.account.state = 'connected'
        self.account._cron_assess_health(limit=5, now=now + timedelta(minutes=15))
        self.assertEqual(alert.state, 'resolved')
        self.assertFalse(self._activities(alert))
        self.account.state = 'error'
        self.account._cron_assess_health(limit=5, now=now + timedelta(minutes=20))
        self.assertEqual(alert.state, 'open')
        self.assertEqual(len(self._activities(alert)), 1)

    def test_revoked_owner_keeps_alert_without_notification(self):
        self.manager.active = False
        self.account.state = 'error'
        self.account._cron_assess_health(limit=5)
        alert = self._alerts().filtered(lambda a: a.check_code == 'account_disconnected')
        self.assertEqual(len(alert), 1)
        self.assertFalse(self._activities(alert))

    def test_non_manager_cannot_acknowledge_alert(self):
        self.account.state = 'error'
        self.account._cron_assess_health(limit=5)
        alert = self._alerts().filtered(lambda a: a.check_code == 'account_disconnected')
        user = self.env['res.users'].create({
            'name': 'Health Ordinary User', 'login': 'health_ordinary_user',
            'company_id': self.env.company.id,
            'company_ids': [(6, 0, [self.env.company.id])],
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                   self.env.ref('crm_meta_lead_ads.group_meta_lead_user').id])],
        })
        with self.assertRaises(UserError):
            alert.with_user(user).action_acknowledge()


class TestMetaHealthViews(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Health Screen', 'company_id': cls.env.company.id,
            'app_id': 'screen-app', 'app_secret': 'screen-secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Health Screen Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': 'screen-page',
            'page_access_token': 'screen-token',
        })

    def test_manager_menu_and_local_view_have_no_sensitive_fields(self):
        menu = self.env.ref('crm_meta_lead_ads.menu_meta_health')
        manager = self.env.ref('crm_meta_lead_ads.group_meta_lead_manager')
        self.assertIn(manager, menu.group_ids)
        view = self.env.ref('crm_meta_lead_ads.view_meta_health_account_form')
        arch = view.arch_db
        for secret in ('app_secret', 'user_access_token', 'page_access_token',
                       'sender_psid', 'message_text', 'fetched_payload'):
            self.assertNotIn(secret, arch)
        with patch.object(type(self.account), '_request',
                          side_effect=AssertionError('health view used Graph')):
            self.account.read(['health_level', 'health_due_queue_count'])

    def test_drilldowns_keep_company_account_and_page_scope(self):
        self.env.user.group_ids = [(4, self.env.ref(
            'crm_meta_lead_ads.group_meta_lead_manager').id)]
        queue = self.account.action_health_queue(page=self.page)
        messages = self.account.action_health_messages(page=self.page)
        for action in (queue, messages):
            self.assertIn(('company_id', '=', self.env.company.id), action['domain'])
            self.assertIn(('page_id', '=', self.page.id), action['domain'])
