import hashlib
import hmac
import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from psycopg2 import IntegrityError, OperationalError
from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.registry import Registry
from odoo.tests.common import TransactionCase

from odoo.addons.crm_meta_lead_ads.controllers.main import MetaLeadController
from odoo.addons.crm_meta_lead_ads.models.meta_account import REQUIRED_PERMISSIONS
from odoo.addons.crm_meta_lead_ads.models.meta_page import REQUIRED_SUBSCRIPTION_FIELDS

REQUEST_PATCH = 'odoo.addons.crm_meta_lead_ads.controllers.main.request'


class _FakeHTTPRequest:
    def __init__(self, raw, signature):
        self._raw = raw
        self.headers = {'X-Hub-Signature-256': signature}

    def get_data(self, cache=True):
        return self._raw

    def get_json(self, silent=True):
        return json.loads(self._raw.decode('utf-8'))


class _FakeRequest:
    """Minimal stand-in for odoo.http.request as used by _receive_webhook."""

    def __init__(self, env, raw, signature):
        self.env = env
        self.httprequest = _FakeHTTPRequest(raw, signature)
        self.last_response = None

    def make_response(self, body, headers=None, status=200):
        self.last_response = SimpleNamespace(body=body, status_code=status)
        return self.last_response


class MetaHealthBase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.account = cls.env['meta.account'].create({
            'name': 'Health Meta', 'company_id': cls.company.id,
            'app_id': 'app', 'app_secret': 'secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Health Page', 'company_id': cls.company.id,
            'account_id': cls.account.id, 'meta_page_id': '100',
            'page_access_token': 'token',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Health Form', 'company_id': cls.company.id,
            'page_id': cls.page.id, 'meta_form_id': '200',
        })
        cls.account2 = cls.env['meta.account'].create({
            'name': 'Health Meta 2', 'company_id': cls.company.id,
            'app_id': 'app2', 'app_secret': 'secret2',
        })
        cls.page2 = cls.env['meta.page'].create({
            'name': 'Health Page 2', 'company_id': cls.company.id,
            'account_id': cls.account2.id, 'meta_page_id': '101',
            'page_access_token': 'token2',
        })
        cls.Alert = cls.env['meta.health.alert']
        cls.activity_type = cls.env.ref('crm_meta_lead_ads.mail_activity_data_meta_health')
        cls.todo_type = cls.env.ref('mail.mail_activity_data_todo')

    def _make_user(self, login, company, group_xmlid, companies=None):
        allowed = companies if companies is not None else [company]
        return self.env['res.users'].create({
            'name': login, 'login': login, 'company_id': company.id,
            'company_ids': [(6, 0, [c.id for c in allowed])],
            'group_ids': [(4, self.env.ref(group_xmlid).id)],
        })

    def _make_manager(self, login='health_manager', companies=None, company=None):
        home = company or self.company
        manager = self._make_user(
            login, home, 'crm_meta_lead_ads.group_meta_lead_manager',
            companies=companies)
        # Meta groups carry their own privilege, so they do not imply the
        # internal-user group; without it the owner would be share=True.
        manager.group_ids = [(4, self.env.ref('base.group_user').id)]
        return manager

    def _health_activities(self, alert):
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'meta.health.alert'), ('res_id', '=', alert.id),
            ('activity_type_id', '=', self.activity_type.id)])

    def _alerts_for(self, account, code=None):
        domain = [('account_id', '=', account.id)]
        if code:
            domain.append(('check_code', '=', code))
        return self.Alert.sudo().search(domain)

    def _run_monitor(self):
        self.Alert._cron_health_monitor()
        self.env['meta.account'].invalidate_model()
        self.Alert.invalidate_model()

    def _run_monitor_at(self, when):
        """Run one monitor pass under a deterministic clock. Datetime
        columns are stored at second precision, so consecutive passes
        within the same wall-clock second are indistinguishable from real
        time alone."""
        with patch.object(fields.Datetime, 'now', return_value=when):
            self._run_monitor()


class TestMetaHealthEvidence(MetaHealthBase):
    """Live-receipt evidence: stamped only after signature validation and
    a page/account match, and never by historical imports or processing
    outcomes. Stamps persist only when the webhook request transaction
    commits — a leadgen enqueue failure aborts the request (non-2xx so
    Meta redelivers) and the stamp rolls back with it. The messaging path
    deliberately keeps its catch-and-200 behavior (a failed message
    recording keeps the receipt and answers 200); that asymmetry is
    called out in the PR for a separate reviewer decision."""

    def _call_webhook(self, secret, payload, account=None, valid=True):
        raw = json.dumps(payload).encode('utf-8')
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest() if valid else '0' * 64
        fake = _FakeRequest(self.env, raw, 'sha256=' + digest)
        with patch(REQUEST_PATCH, fake):
            return MetaLeadController()._receive_webhook(secret, account=account)

    def _leadgen_payload(self, lead_id, page_meta_id='100'):
        return {'object': 'page', 'entry': [{'id': page_meta_id, 'changes': [{
            'field': 'leadgen',
            'value': {'leadgen_id': lead_id, 'page_id': page_meta_id, 'form_id': '200'},
        }]}]}

    def test_signed_leadgen_updates_only_own_page_and_account(self):
        response = self._call_webhook('secret', self._leadgen_payload('L-1'), account=self.account)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.page.last_webhook_event_at)
        self.assertTrue(self.page.last_live_lead_enqueued_at)
        self.assertFalse(self.page.last_live_message_at)
        self.assertTrue(self.account.last_webhook_event_at)
        # Other account/page in the same company stay Unknown.
        self.assertFalse(self.page2.last_webhook_event_at)
        self.assertFalse(self.page2.last_live_lead_enqueued_at)
        self.assertFalse(self.account2.last_webhook_event_at)

    def test_repeated_delivery_does_not_restamp_lead_evidence(self):
        self._call_webhook('secret', self._leadgen_payload('L-1'), account=self.account)
        lead_ts = self.page.last_live_lead_enqueued_at
        self.assertTrue(lead_ts)
        self._call_webhook('secret', self._leadgen_payload('L-1'), account=self.account)
        self.assertEqual(self.page.last_live_lead_enqueued_at, lead_ts)
        self.assertTrue(self.page.last_webhook_event_at >= lead_ts)

    def test_bad_signature_updates_nothing(self):
        response = self._call_webhook(
            'secret', self._leadgen_payload('L-2'), account=self.account, valid=False)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.page.last_webhook_event_at)
        self.assertFalse(self.page.last_live_lead_enqueued_at)
        self.assertFalse(self.account.last_webhook_event_at)
        self.assertFalse(self.env['meta.lead.queue'].search([('meta_lead_id', '=', 'L-2')]))

    def test_wrong_account_updates_nothing(self):
        # Valid signature for account2's secret, but the event belongs to
        # account1's page: the per-account page match must not find it.
        response = self._call_webhook(
            'secret2', self._leadgen_payload('L-3'), account=self.account2)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.page.last_webhook_event_at)
        self.assertFalse(self.page2.last_webhook_event_at)
        self.assertFalse(self.account.last_webhook_event_at)
        self.assertFalse(self.account2.last_webhook_event_at)

    def test_historical_import_does_not_advance_live_evidence(self):
        Queue = self.env['meta.lead.queue']
        _unique, _pre, created, _race = Queue._enqueue_poll_leads(
            self.company, self.page, self.form, ['L-4'])
        self.assertEqual(created, 1)
        queued = Queue.search([('meta_lead_id', '=', 'L-4')])
        self.assertTrue(queued.received_at)
        # received_at is set by imports too — the live evidence stays empty.
        self.assertFalse(self.page.last_webhook_event_at)
        self.assertFalse(self.page.last_live_lead_enqueued_at)
        self.assertFalse(self.account.last_webhook_event_at)

    def test_processing_failure_does_not_change_receipt_evidence(self):
        self._call_webhook('secret', self._leadgen_payload('L-5'), account=self.account)
        event_ts = self.page.last_webhook_event_at
        lead_ts = self.page.last_live_lead_enqueued_at
        queued = self.env['meta.lead.queue'].search([('meta_lead_id', '=', 'L-5')])
        with patch.object(type(queued), '_fetch_lead', side_effect=Exception('boom')):
            queued.process_one()
        self.assertIn(queued.state, ('retry', 'failed'))
        # Receipt (evidence of delivery) is untouched by processing success.
        self.assertEqual(self.page.last_webhook_event_at, event_ts)
        self.assertEqual(self.page.last_live_lead_enqueued_at, lead_ts)

    def test_messaging_event_stamps_receipt_and_message(self):
        payload = {'object': 'page', 'entry': [{'id': '100', 'messaging': [{
            'sender': {'id': '9001'}, 'recipient': {'id': '100'},
            'timestamp': 1727500000000,
            'message': {'mid': 'm-1', 'text': 'hello'}}]}]}

        def profile(method, path, token=None, params=None, **kw):
            if path == '9001':
                return {'id': '9001', 'first_name': 'Test', 'last_name': 'Sender'}
            raise AssertionError('unexpected Graph path %s' % path)

        with patch.object(type(self.account), '_request', side_effect=profile):
            response = self._call_webhook('secret', payload, account=self.account)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.page.last_webhook_event_at)
        self.assertTrue(self.page.last_live_message_at)
        self.assertFalse(self.page.last_live_lead_enqueued_at)
        self.assertFalse(self.page2.last_webhook_event_at)

    def test_echo_message_stamps_nothing(self):
        payload = {'object': 'page', 'entry': [{'id': '100', 'messaging': [{
            'sender': {'id': '100'}, 'recipient': {'id': '9001'},
            'timestamp': 1727500000000,
            'message': {'mid': 'm-echo', 'is_echo': True, 'text': 'out'}}]}]}
        response = self._call_webhook('secret', payload, account=self.account)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.page.last_webhook_event_at)
        self.assertFalse(self.page.last_live_message_at)

    def _graph_lead_payload(self, leadgen_id, email):
        return {
            'id': leadgen_id, 'created_time': '2026-09-24T10:00:00+0000',
            'form_id': '200', 'platform': 'facebook', 'is_organic': False,
            'field_data': [
                {'name': 'full_name', 'values': ['Test Person']},
                {'name': 'email', 'values': [email]},
            ],
        }

    def _process_queue_rows(self, rows, payloads):
        def fake_request(method, path, token=None, params=None, **kw):
            return payloads[path]
        with patch.object(type(self.account), '_request', side_effect=fake_request):
            for row in rows:
                row.process_one()
        rows.invalidate_recordset()

    def _invalidate_webhook_state(self):
        self.env['meta.page'].invalidate_model()
        self.env['meta.account'].invalidate_model()
        self.env['meta.lead.queue'].invalidate_model()

    def test_leadgen_enqueue_failure_aborts_and_rolls_back(self):
        """A valid, signed, page-matched leadgen event whose enqueue fails
        must NOT be acknowledged: the controller raises (a real request
        answers non-2xx/500 so Meta redelivers) and the whole request
        transaction rolls back — no queue row AND no receipt stamp. The
        test savepoint emulates the request-transaction rollback."""
        with patch.object(type(self.env['meta.lead.queue']), 'enqueue_event',
                          side_effect=Exception('boom')):
            with self.assertRaises(RuntimeError):
                with self.env.cr.savepoint():
                    self._call_webhook(
                        'secret', self._leadgen_payload('L-9'), account=self.account)
        self._invalidate_webhook_state()
        self.assertFalse(self.env['meta.lead.queue'].search([('meta_lead_id', '=', 'L-9')]))
        self.assertFalse(self.page.last_webhook_event_at)
        self.assertFalse(self.account.last_webhook_event_at)
        self.assertFalse(self.page.last_live_lead_enqueued_at)
        self.assertFalse(self.page2.last_webhook_event_at)

    def test_leadgen_redelivery_after_failure_saves_exactly_once(self):
        """Meta's redelivery of the SAME payload after a failed delivery
        (enqueue now works) saves exactly one queue row per leadgen_id,
        processing creates exactly one CRM lead, and a further redundant
        delivery duplicates nothing."""
        payload = self._leadgen_payload('L-10')
        with patch.object(type(self.env['meta.lead.queue']), 'enqueue_event',
                          side_effect=Exception('boom')):
            with self.assertRaises(RuntimeError):
                with self.env.cr.savepoint():
                    self._call_webhook('secret', payload, account=self.account)
        self._invalidate_webhook_state()
        self.assertFalse(self.env['meta.lead.queue'].search([('meta_lead_id', '=', 'L-10')]))
        # Redelivery with enqueue working again.
        response = self._call_webhook('secret', payload, account=self.account)
        self.assertEqual(response.status_code, 200)
        rows = self.env['meta.lead.queue'].search([('meta_lead_id', '=', 'L-10')])
        self.assertEqual(len(rows), 1)
        # Meta may also redeliver a 200-acked payload: still exactly one row.
        response = self._call_webhook('secret', payload, account=self.account)
        self.assertEqual(response.status_code, 200)
        rows = self.env['meta.lead.queue'].search([('meta_lead_id', '=', 'L-10')])
        self.assertEqual(len(rows), 1)
        # Processing yields exactly one CRM lead for the leadgen id.
        self._process_queue_rows(rows, {'L-10': self._graph_lead_payload('L-10', 'l10@example.com')})
        self.assertEqual(rows.match_result, 'created')
        self.assertEqual(
            self.env['crm.lead'].search_count([('meta_lead_id', '=', 'L-10')]), 1)
        self.assertTrue(self.page.last_webhook_event_at)
        self.assertTrue(self.page.last_live_lead_enqueued_at)

    def test_multi_event_payload_all_or_nothing_and_redelivery(self):
        """Two leadgen events in ONE payload: if one enqueue fails, the
        webhook answers non-2xx and NOTHING is persisted (rollback is
        all-or-nothing — even the event whose enqueue succeeded). The
        redelivery then saves both exactly once, and processing creates
        exactly one lead per leadgen_id."""
        payload = {'object': 'page', 'entry': [{'id': '100', 'changes': [
            {'field': 'leadgen', 'value': {
                'leadgen_id': 'L-11', 'page_id': '100', 'form_id': '200'}},
            {'field': 'leadgen', 'value': {
                'leadgen_id': 'L-12', 'page_id': '100', 'form_id': '200'}},
        ]}]}
        Queue = self.env['meta.lead.queue']
        real_enqueue = Queue.enqueue_event

        def fail_second(*args, **kwargs):
            if args[2] == 'L-12':
                raise Exception('boom')
            return real_enqueue(*args, **kwargs)

        with patch.object(type(Queue), 'enqueue_event', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                with self.env.cr.savepoint():
                    self._call_webhook('secret', payload, account=self.account)
        self._invalidate_webhook_state()
        # All-or-nothing: even L-11's successful enqueue is rolled back.
        self.assertFalse(Queue.search([('meta_lead_id', 'in', ['L-11', 'L-12'])]))
        self.assertFalse(self.page.last_webhook_event_at)
        # Redelivery with enqueue working: both events saved exactly once.
        response = self._call_webhook('secret', payload, account=self.account)
        self.assertEqual(response.status_code, 200)
        rows = Queue.search([('meta_lead_id', 'in', ['L-11', 'L-12'])])
        self.assertEqual(len(rows), 2)
        response = self._call_webhook('secret', payload, account=self.account)
        self.assertEqual(response.status_code, 200)
        rows = Queue.search([('meta_lead_id', 'in', ['L-11', 'L-12'])])
        self.assertEqual(len(rows), 2)
        # No event lost, no lead duplicated: one CRM lead per leadgen_id.
        self._process_queue_rows(rows, {
            'L-11': self._graph_lead_payload('L-11', 'l11@example.com'),
            'L-12': self._graph_lead_payload('L-12', 'l12@example.com'),
        })
        self.assertEqual(set(rows.mapped('match_result')), {'created'})
        self.assertEqual(
            self.env['crm.lead'].search_count([('meta_lead_id', '=', 'L-11')]), 1)
        self.assertEqual(
            self.env['crm.lead'].search_count([('meta_lead_id', '=', 'L-12')]), 1)

    def test_messaging_processing_failure_keeps_event_receipt(self):
        """The messaging path stamps the event receipt before processing;
        a recording failure must not erase it nor produce a message stamp."""
        payload = {'object': 'page', 'entry': [{'id': '100', 'messaging': [{
            'sender': {'id': '9001'}, 'recipient': {'id': '100'},
            'timestamp': 1727500000000,
            'message': {'mid': 'm-fail', 'text': 'hello'}}]}]}
        with patch.object(type(self.env['meta.conversation']), '_record_inbound_message',
                          side_effect=Exception('boom')):
            response = self._call_webhook('secret', payload, account=self.account)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.page.last_webhook_event_at)
        self.assertFalse(self.page.last_live_message_at)
        self.assertFalse(self.env['meta.message'].search([('meta_message_id', '=', 'm-fail')]))


class TestMetaHealthMonitor(MetaHealthBase):
    def test_quiet_traffic_stays_non_error_by_default(self):
        self.account.write({'health_monitoring_enabled': True})
        self.assertTrue(self.account.health_monitoring_since)
        self._run_monitor()
        self.assertFalse(self._alerts_for(self.account))
        self.assertTrue(self.account.health_last_check_at)
        self.assertEqual(self.account.health_level, 'ok')

    def test_disabled_monitoring_produces_no_alerts(self):
        self.account.write({'state': 'error', 'error_message': 'recorded failure'})
        self._run_monitor()
        self.assertFalse(self._alerts_for(self.account))
        self.assertFalse(self.account.health_last_check_at)
        self.assertEqual(self.account.health_level, 'disabled')

    def test_silence_clock_starts_at_enablement(self):
        self.account.write({
            'health_monitoring_enabled': True, 'health_silence_minutes': 60})
        self._run_monitor()
        self.assertFalse(self._alerts_for(self.account, 'silence'))
        # Move the enablement clock beyond the threshold: now the optional,
        # explicitly uncertain silence warning appears per page.
        self.account.health_monitoring_since = fields.Datetime.now() - timedelta(hours=2)
        self._run_monitor()
        alert = self._alerts_for(self.account, 'silence')
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.page_id, self.page)
        self.assertEqual(alert.severity, 'warning')
        self.assertIn('verify', alert.detail.lower())
        self.assertIn('not necessarily disconnected', alert.detail)
        self.assertEqual(self.account.health_level, 'warning')

    def test_due_backlog_scope_and_future_retries(self):
        self.account.write({'health_monitoring_enabled': True})
        self.account2.write({'health_monitoring_enabled': True})
        now = fields.Datetime.now()
        Queue = self.env['meta.lead.queue']
        overdue = Queue.enqueue_event(self.company, self.page, 'L-b1', '200', {})
        overdue.received_at = now - timedelta(hours=1)
        fresh = Queue.enqueue_event(self.company, self.page, 'L-b2', '200', {})
        future_retry = Queue.enqueue_event(self.company, self.page, 'L-b3', '200', {})
        future_retry.write({
            'state': 'retry', 'received_at': now - timedelta(hours=2),
            'next_retry_at': now + timedelta(hours=1)})
        other_account = Queue.enqueue_event(self.company, self.page2, 'L-b4', None, {})
        other_account.received_at = now - timedelta(hours=2)
        self._run_monitor()
        alert = self._alerts_for(self.account, 'queue_backlog')
        self.assertEqual(len(alert), 1)
        # Only the overdue record of THIS account counts: the fresh record,
        # the future-scheduled retry and account2's record are excluded.
        self.assertEqual(alert.summary_count, 1)
        alert2 = self._alerts_for(self.account2, 'queue_backlog')
        self.assertEqual(len(alert2), 1)
        self.assertEqual(alert2.summary_count, 1)
        self.assertTrue(fresh.state == 'pending')

    def test_backlog_isolated_across_companies(self):
        other_company = self.env['res.company'].create({'name': 'Other Health Co'})
        account3 = self.env['meta.account'].create({
            'name': 'Health Meta 3', 'company_id': other_company.id,
            'app_id': 'app3', 'app_secret': 'secret3',
            'health_monitoring_enabled': True,
        })
        page3 = self.env['meta.page'].create({
            'name': 'Health Page 3', 'company_id': other_company.id,
            'account_id': account3.id, 'meta_page_id': '102',
            'page_access_token': 'token3',
        })
        now = fields.Datetime.now()
        own = self.env['meta.lead.queue'].enqueue_event(self.company, self.page, 'L-c1', None, {})
        own.received_at = now - timedelta(hours=1)
        other = self.env['meta.lead.queue'].enqueue_event(other_company, page3, 'L-c2', None, {})
        other.received_at = now - timedelta(hours=1)
        self.account.write({'health_monitoring_enabled': True})
        self._run_monitor()
        self.assertEqual(self._alerts_for(self.account, 'queue_backlog').summary_count, 1)
        self.assertEqual(self._alerts_for(account3, 'queue_backlog').summary_count, 1)
        self.assertNotEqual(
            self._alerts_for(self.account, 'queue_backlog').company_id,
            self._alerts_for(account3, 'queue_backlog').company_id)

    def test_repeated_passes_single_incident_and_activity(self):
        owner = self._make_manager()
        self.account.write({
            'health_monitoring_enabled': True, 'health_owner_id': owner.id,
            'state': 'error', 'error_message': 'recorded failure'})
        self._run_monitor()
        self._run_monitor()
        alerts = self._alerts_for(self.account, 'account_error')
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.state, 'open')
        activities = self._health_activities(alerts)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, owner)
        self.assertEqual(alerts.notification_state, 'notified')
        # An unchanged pass posts no chatter and creates no activity.
        message_count = len(alerts.message_ids)
        self._run_monitor()
        self.assertEqual(len(alerts.message_ids), message_count)
        self.assertEqual(len(self._health_activities(alerts)), 1)

    def test_unique_constraint_blocks_duplicate_keys(self):
        self.account.write({'health_monitoring_enabled': True, 'state': 'error'})
        self._run_monitor()
        alert = self._alerts_for(self.account, 'account_error')
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Alert.sudo().create({
                    'name': 'dup', 'company_id': self.company.id,
                    'account_id': self.account.id, 'check_code': 'account_error',
                    'alert_key': alert.alert_key, 'severity': 'error',
                })

    def test_integrity_error_reuse_path(self):
        """Sequential proof of the IntegrityError-reuse branch: the
        competitor row appears between the pre-check and the insert (the
        pre-check is forced to miss it), the insert collides, and the
        upsert reuses the competitor instead of failing or duplicating.
        This covers a competitor row VISIBLE to the current transaction.
        Under REPEATABLE READ, a truly concurrent competitor commits after
        our snapshot: its insert surfaces as a serialization failure (not
        IntegrityError) and an in-transaction re-read could not see the
        row anyway — that collision is resolved by a FRESH transaction
        (next cron pass), proven at the database level by
        TestMetaHealthUpsertConcurrency."""
        owner = self._make_manager()
        self.account.write({
            'health_monitoring_enabled': True, 'health_owner_id': owner.id})
        existing = self.Alert._upsert_alert(
            self.account, False, 'account_error', 'error', 'first', 0)
        Alert = self.Alert
        real_search = Alert.search
        calls = {'n': 0}

        def racing_search(_records, domain, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return Alert.browse()  # competitor not visible in the race window
            return real_search(domain, *args, **kwargs)

        with patch.object(type(Alert), 'search', side_effect=racing_search, autospec=True), \
                patch.object(type(Alert), 'create', side_effect=IntegrityError('duplicate key')):
            reused = Alert._upsert_alert(self.account, False, 'account_error', 'error', 'second', 3)
        self.assertEqual(reused.id, existing.id)
        self.assertEqual(reused.summary_count, 3)
        self.assertEqual(len(self._alerts_for(self.account, 'account_error')), 1)

    def test_acknowledge_recover_recur_and_unrelated_activities(self):
        owner = self._make_manager()
        self.account.write({
            'health_monitoring_enabled': True, 'health_owner_id': owner.id,
            'state': 'error', 'error_message': 'recorded failure'})
        self._run_monitor()
        alert = self._alerts_for(self.account, 'account_error')
        self.assertEqual(alert.state, 'open')
        self.assertEqual(len(self._health_activities(alert)), 1)
        # An unrelated To-Do on the same record and on the account must
        # never be completed by the health lifecycle.
        unrelated_alert_todo = self.env['mail.activity'].sudo().create({
            'activity_type_id': self.todo_type.id,
            'res_model_id': self.env['ir.model']._get_id('meta.health.alert'),
            'res_id': alert.id, 'user_id': owner.id, 'summary': 'manual follow-up',
            'date_deadline': fields.Date.today()})
        unrelated_account_todo = self.env['mail.activity'].sudo().create({
            'activity_type_id': self.todo_type.id,
            'res_model_id': self.env['ir.model']._get_id('meta.account'),
            'res_id': self.account.id, 'user_id': owner.id, 'summary': 'call admin',
            'date_deadline': fields.Date.today()})
        # Acknowledge suppresses reminders for this episode.
        alert.with_user(owner).action_acknowledge()
        self.assertEqual(alert.state, 'acknowledged')
        self._run_monitor()
        self.assertEqual(len(self._health_activities(alert)), 1)
        self.assertFalse(self._alerts_for(self.account, 'account_error') - alert)
        # Recovery resolves the incident and completes only its own
        # dedicated activity.
        self.account.write({'state': 'connected', 'error_message': False})
        self._run_monitor()
        self.assertEqual(alert.state, 'resolved')
        self.assertTrue(alert.resolved_at)
        self.assertFalse(self._health_activities(alert))
        self.assertTrue(unrelated_alert_todo.exists())
        self.assertTrue(unrelated_account_todo.exists())
        self.assertEqual(self.account.health_level, 'ok')
        # A new episode after recovery reopens the same record and may
        # notify again.
        self.account.write({'state': 'error', 'error_message': 'recorded failure'})
        self._run_monitor()
        self.assertEqual(alert.state, 'open')
        self.assertEqual(alert.first_seen_at, alert.last_seen_at)
        self.assertEqual(len(self._health_activities(alert)), 1)
        self.assertEqual(len(self._alerts_for(self.account, 'account_error')), 1)

    def test_token_expiry_boundaries(self):
        now = fields.Datetime.now()
        self.account.write({'health_monitoring_enabled': True})
        self.account.token_expires_at = now + timedelta(days=30)
        self._run_monitor()
        self.assertFalse(self._alerts_for(self.account, 'token_expired'))
        self.assertFalse(self._alerts_for(self.account, 'token_expiring'))
        self.account.token_expires_at = now + timedelta(days=3)
        self._run_monitor()
        alert = self._alerts_for(self.account, 'token_expiring')
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.severity, 'warning')
        self.account.token_expires_at = now - timedelta(seconds=1)
        self._run_monitor()
        self.assertEqual(self._alerts_for(self.account, 'token_expired').severity, 'error')
        self.assertFalse(self._alerts_for(self.account, 'token_expiring').filtered(
            lambda a: a.state != 'resolved'))
        # Unknown expiry stays unknown: no alerts at all.
        self.account.token_expires_at = False
        self._run_monitor()
        self.assertFalse(self._alerts_for(self.account, 'token_expired').filtered(
            lambda a: a.state != 'resolved'))

    def test_stale_subscription_check_downgrade(self):
        now = fields.Datetime.now()
        self.account.write({'health_monitoring_enabled': True})
        self.page.write({
            'subscription_status': 'failed', 'subscription_error': 'not subscribed',
            'subscription_checked_at': now - timedelta(hours=30)})
        self._run_monitor()
        self.assertFalse(self._alerts_for(self.account, 'subscription_failed'))
        stale = self._alerts_for(self.account, 'subscription_stale')
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale.severity, 'warning')
        # A fresh check asserts the current failure instead.
        self.page.subscription_checked_at = now - timedelta(hours=1)
        self._run_monitor()
        fresh = self._alerts_for(self.account, 'subscription_failed')
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh.severity, 'error')
        self.assertEqual(stale.state, 'resolved')

    def test_archived_pages_excluded_from_page_checks(self):
        self.account.write({'health_monitoring_enabled': True})
        self.page.write({
            'active': False, 'sync_enabled': False, 'page_access_token': False,
            'subscription_status': 'failed', 'subscription_error': 'not subscribed',
            'subscription_checked_at': fields.Datetime.now()})
        self._run_monitor()
        self.assertFalse(self._alerts_for(self.account, 'subscription_failed'))
        self.assertFalse(self._alerts_for(self.account, 'subscription_stale'))

    def test_owner_validation(self):
        manager = self._make_manager()
        self.account.health_owner_id = manager.id
        non_manager = self._make_user(
            'health_plain_user', self.company, 'crm_meta_lead_ads.group_meta_lead_user')
        with self.assertRaises(ValidationError):
            self.account.health_owner_id = non_manager.id
        manager.active = False
        with self.assertRaises(ValidationError):
            self.account.health_owner_id = manager.id
        other_company = self.env['res.company'].create({'name': 'Owner Other Co'})
        outsider = self._make_manager(
            login='health_outsider', companies=[other_company],
            company=other_company)
        with self.assertRaises(ValidationError):
            self.account.health_owner_id = outsider.id

    def test_threshold_validation(self):
        with self.assertRaises(ValidationError):
            self.account.health_backlog_threshold_minutes = 0
        with self.assertRaises(ValidationError):
            self.account.health_silence_minutes = -5
        with self.assertRaises(ValidationError):
            self.account.health_token_expiry_warn_days = -1

    def test_ineligible_owner_keeps_alert_visible_without_notifying(self):
        owner = self._make_manager()
        self.account.write({
            'health_monitoring_enabled': True, 'health_owner_id': owner.id,
            'state': 'error', 'error_message': 'recorded failure'})
        # Access changed after configuration: the owner was deactivated.
        owner.active = False
        self._run_monitor()
        alert = self._alerts_for(self.account, 'account_error')
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.state, 'open')
        self.assertEqual(alert.notification_state, 'no_recipient')
        self.assertFalse(self._health_activities(alert))

    def test_notification_retries_after_owner_becomes_eligible(self):
        """An open alert stuck on 'no_recipient' must notify the owner
        once he is fixed — exactly once per episode, and never again
        after the alert is acknowledged."""
        owner = self._make_manager()
        self.account.write({
            'health_monitoring_enabled': True, 'health_owner_id': owner.id,
            'state': 'error', 'error_message': 'recorded failure'})
        owner.active = False
        self._run_monitor()
        alert = self._alerts_for(self.account, 'account_error')
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.state, 'open')
        self.assertEqual(alert.notification_state, 'no_recipient')
        self.assertFalse(self._health_activities(alert))
        # The owner is fixed while the episode is still open: the next
        # pass retries the notification instead of waiting for a new
        # episode.
        owner.active = True
        self._run_monitor()
        self.assertEqual(alert.notification_state, 'notified')
        activities = self._health_activities(alert)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, owner)
        # Steady state: further passes create nothing new.
        self._run_monitor()
        self._run_monitor()
        self.assertEqual(len(self._health_activities(alert)), 1)
        # Acknowledge: the alert stays visible and is never re-notified
        # for this episode.
        alert.with_user(owner).action_acknowledge()
        self.assertEqual(alert.state, 'acknowledged')
        self._run_monitor()
        self.assertEqual(len(self._health_activities(alert)), 1)

    def test_secret_marker_never_reaches_alert_or_activity(self):
        marker = 'tok-HEALTH-MARKER-1'
        owner = self._make_manager()
        self.account.write({
            'health_monitoring_enabled': True, 'health_owner_id': owner.id,
            'user_access_token': marker, 'state': 'error'})
        # Plant a raw (unsanitized) legacy error string holding the secret,
        # as an old record might contain it.
        self.env.cr.execute(
            'UPDATE meta_account SET error_message = %s WHERE id = %s',
            ('Graph error 190: invalid token %s supplied' % marker, self.account.id))
        self.account.invalidate_recordset(['error_message'])
        self._run_monitor()
        alert = self._alerts_for(self.account, 'account_error')
        self.assertEqual(len(alert), 1)
        self.assertNotIn(marker, alert.detail or '')
        for activity in self._health_activities(alert):
            self.assertNotIn(marker, activity.summary or '')
            self.assertNotIn(marker, activity.note or '')
        for message in alert.message_ids:
            self.assertNotIn(marker, message.body or '')
        # The manual Check Now action returns only a display_notification
        # dict — the sanitization-input secrets must not reach it either.
        # The action is manager-only, so run it as the configured owner.
        result = self.account.with_user(owner).action_health_check_now()
        self.assertNotIn(marker, str(result))


class TestMetaHealthDiagnostics(MetaHealthBase):
    """Diagnostics outcome aggregation: failures can never look like
    success, and empty permission evidence stays unknown."""

    def _diag_handler(self, behavior):
        def handler(method, path, token=None, params=None, **kw):
            if path == 'me':
                if behavior.get('connection_fail'):
                    raise UserError('Connection error for url: /me (timeout)')
                return {'id': '1', 'name': 'Meta User'}
            if path == 'me/permissions':
                if behavior.get('perms_raise'):
                    raise Exception('permissions endpoint unavailable')
                if behavior.get('perms_empty'):
                    return {'data': []}
                return {'data': [{'name': p, 'status': 'granted'}
                                 for p in REQUIRED_PERMISSIONS]}
            if path.endswith('/subscribed_apps'):
                if behavior.get('subscription_missing'):
                    return {'data': [{'id': 'other-app', 'subscribed_fields': []}]}
                return {'data': [{
                    'id': self.account.app_id,
                    'subscribed_fields': list(REQUIRED_SUBSCRIPTION_FIELDS)}]}
            raise AssertionError('unexpected Graph path %s' % path)
        return handler

    def _run_diagnostics(self, behavior, user=None):
        self.account.user_access_token = 'usertok'
        if user is None:
            # The action is manager-only; run it as a Meta manager (NOT
            # as admin — admin lacks group_meta_lead_manager).
            user = self._make_manager(login='diag_manager')
        with patch.object(type(self.account), '_request',
                          side_effect=self._diag_handler(behavior)):
            return self.account.with_user(user).action_run_diagnostics()

    def test_failed_connection_cannot_be_success(self):
        result = self._run_diagnostics({'connection_fail': True})
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(self.account.last_diagnostic_outcome, 'failure')
        self.assertTrue(self.account.last_diagnostic_at)
        self.assertEqual(self.account.state, 'error')

    def test_failed_subscription_cannot_be_success(self):
        result = self._run_diagnostics({'subscription_missing': True})
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(self.account.last_diagnostic_outcome, 'failure')
        self.assertEqual(self.page.subscription_status, 'failed')

    def test_empty_permissions_stay_unknown_not_denial(self):
        result = self._run_diagnostics({'perms_empty': True})
        # Empty evidence is not a denial and never a failure banner.
        self.assertNotEqual(result['params']['type'], 'danger')
        self.assertNotEqual(self.account.last_diagnostic_outcome, 'failure')
        self.assertFalse(self.account.missing_permissions)
        self.assertFalse(self.account.granted_permissions)
        self.assertFalse(self.account.permissions_checked_at)

    def test_full_pass_is_success(self):
        result = self._run_diagnostics({})
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(self.account.last_diagnostic_outcome, 'success')

    def test_permission_check_failure_keeps_stale_evidence(self):
        checked_at = fields.Datetime.now() - timedelta(days=1)
        self.account.write({
            'granted_permissions': 'leads_retrieval',
            'missing_permissions': 'pages_messaging',
            'permissions_checked_at': checked_at,
        })
        result = self._run_diagnostics({'perms_raise': True})
        self.assertNotEqual(result['params']['type'], 'danger')
        # A failed refresh must not display old permissions as freshly
        # verified: stored values and their check time stay untouched.
        self.assertEqual(self.account.granted_permissions, 'leads_retrieval')
        self.assertEqual(self.account.permissions_checked_at, checked_at)
        self.assertNotEqual(self.account.last_diagnostic_outcome, 'failure')

    def test_diagnostics_manager_without_system_group_succeeds(self):
        """The guard passes on the caller's OWN groups; only then does the
        body escalate. A Meta manager WITHOUT base.group_system must run
        diagnostics end-to-end and get the correct outcome fields."""
        manager = self._make_manager(login='diag_nosystem_mgr')
        self.assertTrue(manager.has_group(
            'crm_meta_lead_ads.group_meta_lead_manager'))
        self.assertFalse(manager.has_group('base.group_system'))
        result = self._run_diagnostics({}, user=manager)
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(self.account.last_diagnostic_outcome, 'success')
        self.assertTrue(self.account.last_diagnostic_at)
        self.assertEqual(self.account.state, 'connected')

    def test_diagnostics_plain_user_rejected(self):
        """A plain Meta user (not manager) gets a clean UserError BEFORE
        any sudo escalation; no findings are written."""
        user = self._make_user(
            'diag_plain_user', self.company, 'crm_meta_lead_ads.group_meta_lead_user')
        self.account.user_access_token = 'usertok'
        with patch.object(type(self.account), '_request',
                          side_effect=self._diag_handler({})):
            with self.assertRaises(UserError):
                self.account.with_user(user).action_run_diagnostics()
        self.assertFalse(self.account.last_diagnostic_at)
        self.assertFalse(self.account.last_diagnostic_outcome)
        self.assertEqual(self.account.state, 'draft')

    def test_diagnostics_cross_company_manager_rejected(self):
        """A manager of company B cannot run diagnostics on company A's
        account (e.g. a guessed id over RPC): clean error, no findings."""
        other_company = self.env['res.company'].create({'name': 'Diag Other Co'})
        manager_b = self._make_manager(
            login='diag_cross_mgr', companies=[other_company], company=other_company)
        self.account.user_access_token = 'usertok'
        with patch.object(type(self.account), '_request',
                          side_effect=self._diag_handler({})):
            with self.assertRaises((UserError, AccessError)):
                self.account.with_user(manager_b).action_run_diagnostics()
        self.assertFalse(self.account.last_diagnostic_at)
        self.assertFalse(self.account.last_diagnostic_outcome)
        self.assertEqual(self.account.state, 'draft')

    def test_diagnostics_never_leak_secret_markers(self):
        """Token/secret values inside a raw Graph error must never reach
        stored fields or the returned notification."""
        token_marker = 'tok-DIAG-MARKER-1'
        secret_marker = 'secret-DIAG-MARKER-2'
        self.account.write({
            'user_access_token': token_marker, 'app_secret': secret_marker,
        })
        manager = self._make_manager(login='diag_leak_mgr')

        def boom(method, path, token=None, params=None, **kw):
            raise Exception(
                'Graph error 190: invalid token %s using %s'
                % (token_marker, secret_marker))

        with patch.object(type(self.account), '_request', side_effect=boom):
            result = self.account.with_user(manager).action_run_diagnostics()
        self.assertEqual(self.account.last_diagnostic_outcome, 'failure')
        self.assertEqual(self.account.state, 'error')
        for stored in (self.account.error_message,
                       self.page.subscription_error):
            self.assertNotIn(token_marker, stored or '')
            self.assertNotIn(secret_marker, stored or '')
        self.assertNotIn(token_marker, str(result))
        self.assertNotIn(secret_marker, str(result))


class TestMetaHealthAccess(MetaHealthBase):
    def test_user_cannot_act_on_alerts(self):
        self.account.write({'health_monitoring_enabled': True, 'state': 'error'})
        self._run_monitor()
        alert = self._alerts_for(self.account, 'account_error')
        user = self._make_user(
            'health_reader', self.company, 'crm_meta_lead_ads.group_meta_lead_user')
        # Read-only for plain users (counts carry no PII), never write.
        self.assertTrue(alert.with_user(user).read(['state']))
        with self.assertRaises(AccessError):
            alert.with_user(user).write({'state': 'acknowledged'})
        with self.assertRaises(UserError):
            alert.with_user(user).action_acknowledge()
        with self.assertRaises(AccessError):
            self.env['meta.health.alert'].with_user(user).create({
                'name': 'x', 'company_id': self.company.id,
                'account_id': self.account.id, 'check_code': 'account_error',
                'alert_key': 'x', 'severity': 'error'})

    def test_health_actions_require_manager_group(self):
        user = self._make_user(
            'health_action_user', self.company, 'crm_meta_lead_ads.group_meta_lead_user')
        with self.assertRaises(UserError):
            self.account.with_user(user).action_health_check_now()
        with self.assertRaises(UserError):
            self.account.with_user(user).action_health_open_queue()
        manager = self._make_manager(login='health_action_manager')
        result = self.account.with_user(manager).action_health_check_now()
        self.assertEqual(result['params']['type'], 'success')
        action = self.account.with_user(manager).action_health_open_queue()
        self.assertEqual(action['res_model'], 'meta.lead.queue')
        self.assertIn(('page_id', 'in', self.account._health_pages().ids), action['domain'])

    def test_user_reads_aggregate_counts_without_error(self):
        user = self._make_user(
            'health_count_user', self.company, 'crm_meta_lead_ads.group_meta_lead_user')
        account_as_user = self.account.with_user(user)
        # Aggregate-only metrics must not raise for plain users and expose
        # no record content (numbers/timestamps only).
        self.assertIsInstance(account_as_user.health_due_backlog_count, int)
        self.assertIsInstance(account_as_user.health_failed_ambiguous_count, int)
        self.assertIn(account_as_user.health_level,
                      ('disabled', 'unknown', 'ok', 'warning', 'error'))

    def test_multi_company_alert_isolation(self):
        other_company = self.env['res.company'].create({'name': 'Access Other Co'})
        account3 = self.env['meta.account'].create({
            'name': 'Access Meta 3', 'company_id': other_company.id,
            'app_id': 'app3', 'app_secret': 'secret3',
            'health_monitoring_enabled': True, 'state': 'error',
        })
        self._run_monitor()
        alert3 = self._alerts_for(account3, 'account_error')
        self.assertEqual(len(alert3), 1)
        manager_a = self._make_manager(login='health_mgr_a')
        self.assertEqual(
            self.env['meta.health.alert'].with_user(manager_a).search_count(
                [('company_id', '=', other_company.id)]), 0)
        manager_all = self._make_manager(
            login='health_mgr_all', companies=[self.company, other_company])
        manager_all.company_id = self.company.id
        found = self.env['meta.health.alert'].with_user(manager_all).with_context(
            allowed_company_ids=[self.company.id, other_company.id]).search_count(
            [('company_id', '=', other_company.id)])
        self.assertEqual(found, 1)

    def test_cross_company_manager_blocked_before_sudo(self):
        """A manager of company B calling health actions on a company-A
        account (e.g. by guessing the id over RPC) must get a clean
        UserError BEFORE any sudo-scoped query runs against company A."""
        other_company = self.env['res.company'].create({'name': 'Guard Other Co'})
        manager_b = self._make_manager(
            login='health_guard_mgr', companies=[other_company], company=other_company)
        account_as_b = self.account.with_user(manager_b)
        with self.assertRaises(UserError):
            account_as_b.action_health_check_now()
        with self.assertRaises(UserError):
            account_as_b.action_health_open_queue()
        # Nothing was assessed: no check timestamp, no alerts.
        self.assertFalse(self.account.health_last_check_at)
        self.assertFalse(self._alerts_for(self.account))
        # Same-company managers still pass the guard.
        manager_a = self._make_manager(login='health_guard_mgr_a')
        self.account.with_user(manager_a).action_health_check_now()

    def test_cross_company_aggregate_counts_not_visible(self):
        """The aggregate computes are scoped by company AND account; a
        manager restricted to another company must not reach company A's
        numbers at all (record rule hides the record, reads raise)."""
        queued = self.env['meta.lead.queue'].enqueue_event(
            self.company, self.page, 'L-x1', None, {})
        queued.state = 'failed'
        self.assertEqual(self.account.health_failed_ambiguous_count, 1)
        other_company = self.env['res.company'].create({'name': 'Metrics Other Co'})
        manager_b = self._make_manager(
            login='health_metrics_mgr', companies=[other_company], company=other_company)
        self.assertEqual(
            self.env['meta.account'].with_user(manager_b).search_count(
                [('id', '=', self.account.id)]), 0)
        with self.assertRaises(AccessError):
            self.account.with_user(manager_b).read(['health_failed_ambiguous_count'])


class TestMetaHealthBatch(MetaHealthBase):
    """The bounded monitor cursor must advance through ALL enabled
    accounts instead of re-checking the same first batch forever."""

    def test_batch_cursor_advances_and_rotates(self):
        accounts = self.env['meta.account']
        for index in range(27):
            accounts |= self.env['meta.account'].create({
                'name': 'Batch %02d' % index, 'company_id': self.company.id,
                'app_id': 'batch-app-%02d' % index,
                'app_secret': 'batch-secret-%02d' % index,
                'health_monitoring_enabled': True,
            })
        base = fields.Datetime.now()
        # Pass 1 checks exactly the bounded batch of 25 distinct accounts.
        self._run_monitor_at(base)
        checked = accounts.filtered('health_last_check_at')
        self.assertEqual(len(checked), 25)
        self.assertEqual(len(set(checked.ids)), 25)
        # Pass 2 picks up the 2 leftover accounts first (NULLS FIRST).
        self._run_monitor_at(base + timedelta(hours=1))
        self.assertEqual(len(accounts.filtered('health_last_check_at')), 27)
        late = accounts - checked
        self.assertEqual(len(late), 2)
        late_ts = {acc.id: acc.health_last_check_at for acc in late}
        oldest = checked.sorted('health_last_check_at')[:1]
        oldest_ts = oldest.health_last_check_at
        # Pass 3 re-checks the OLDEST-checked accounts (rotation, not
        # starvation) and leaves the most recently checked batch alone.
        self._run_monitor_at(base + timedelta(hours=2))
        self.assertGreater(oldest.health_last_check_at, oldest_ts)
        for acc_id, ts in late_ts.items():
            self.assertEqual(accounts.browse(acc_id).health_last_check_at, ts)


class TestMetaHealthUpsertConcurrency(TransactionCase):
    """REAL two-transaction race on the health alert unique key.

    Scope of proof: this test proves the DATABASE-level race only — the
    unique ``alert_key`` blocks a concurrent inserter, the loser's
    colliding INSERT fails once the winner commits, and a fresh
    transaction then sees exactly the winner's row. The ORM-level
    collision path of ``_upsert_alert`` under true concurrency is NOT
    proven here: under Odoo's REPEATABLE READ isolation an in-transaction
    re-read cannot see a competitor row committed after this
    transaction's snapshot, so the loser's re-search would find nothing
    and the code deliberately re-raises; convergence happens in a FRESH
    transaction (the next cron pass / a redelivered webhook), never by an
    in-transaction re-read. The sequential ORM branch is covered by
    TestMetaHealthMonitor.test_integrity_error_reuse_path.

    Odoo holds its Registry lock while running at-install tests, so ORM
    worker threads cannot acquire it here — the same constraint that
    keeps TestMetaDedupConcurrency's ORM variant skipped. The race
    therefore uses RAW cursors (the pattern the dedup advisory-lock test
    proves viable under this harness).

    Requires a live PostgreSQL — like every Odoo test in this project it
    could NOT be executed in the local static environment; it is written
    for a disposable CI/staging database."""

    EVENT_TIMEOUT = 30       # seconds; a healthy run finishes in < 2s
    BLOCK_PROOF_SECONDS = 3  # B must stay blocked at least this long

    INSERT_SQL = (
        'INSERT INTO meta_health_alert (name, company_id, account_id, '
        'check_code, alert_key, severity, state, notification_state, '
        'create_uid, create_date, write_uid, write_date) '
        "VALUES (%(name)s, %(company)s, %(account)s, 'account_error', "
        "%(key)s, 'error', 'open', 'pending', 1, "
        "NOW() AT TIME ZONE 'UTC', 1, NOW() AT TIME ZONE 'UTC') "
        'RETURNING id')

    def test_concurrent_upserts_converge_to_single_incident(self):
        registry = Registry(self.env.cr.dbname)

        # Fixture through an independent, committed cursor (self.env.cr is
        # never committed, so the TransactionCase savepoint stays intact):
        # one account the raced alert rows can reference.
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env['meta.account'].create({
                'name': 'Race Account', 'company_id': env.company.id,
                'app_id': 'race-app', 'app_secret': 'race-secret',
                'health_monitoring_enabled': True,
            })
            cr.commit()
            account_id = account.id
            company_id = account.company_id.id
        key = '%s:0:account_error' % account_id

        inserted_a = threading.Event()
        release_a = threading.Event()
        done_b = threading.Event()
        results = {}
        errors = []

        def winner():
            try:
                with registry.cursor() as cr:
                    cr.execute(self.INSERT_SQL, {
                        'name': 'race winner', 'company': company_id,
                        'account': account_id, 'key': key})
                    results['winner_alert'] = cr.fetchone()[0]
                    inserted_a.set()
                    # Hold the transaction open: the loser's INSERT must
                    # block on our uncommitted unique-key tuple.
                    release_a.wait(timeout=self.EVENT_TIMEOUT)
                    cr.commit()
            except Exception as exc:
                errors.append(('winner', exc))

        def loser():
            try:
                inserted_a.wait(timeout=self.EVENT_TIMEOUT)
                cr = registry.cursor()
                try:
                    # Bounded wait: if the winner never commits, the test
                    # fails on a statement timeout instead of hanging CI.
                    cr.execute('SET statement_timeout = 20000')
                    try:
                        cr.execute(self.INSERT_SQL, {
                            'name': 'race loser', 'company': company_id,
                            'account': account_id, 'key': key})
                        results['loser_error'] = None
                    except (IntegrityError, SerializationFailure,
                            OperationalError) as exc:
                        # REPEATABLE READ surfaces the lost race as a
                        # serialization failure (40001); READ COMMITTED as
                        # a plain unique violation (23505). Both abort the
                        # loser's transaction — never a silent second row.
                        results['loser_error'] = type(exc).__name__
                    cr.rollback()
                    # Session-level SET would otherwise leak into the
                    # pooled connection's next borrower.
                    cr.execute('RESET statement_timeout')
                finally:
                    cr.close()
                # The upsert retries in a FRESH transaction: the new
                # snapshot sees the winner's committed row and reuses it.
                with registry.cursor() as cr:
                    cr.execute(
                        'SELECT id FROM meta_health_alert WHERE alert_key = %s',
                        (key,))
                    row = cr.fetchone()
                    results['loser_alert'] = row[0] if row else None
            except Exception as exc:
                errors.append(('loser', exc))
            finally:
                done_b.set()

        first = threading.Thread(target=winner)
        second = threading.Thread(target=loser)
        try:
            first.start()
            self.assertTrue(inserted_a.wait(timeout=self.EVENT_TIMEOUT), errors)
            second.start()
            self.assertFalse(
                done_b.wait(timeout=self.BLOCK_PROOF_SECONDS),
                'Loser completed while the winner held its transaction open')
            release_a.set()
            self.assertTrue(done_b.wait(timeout=self.EVENT_TIMEOUT), errors)
            # Verified through a fresh cursor (the test transaction's
            # snapshot cannot see the workers' committed rows): exactly
            # ONE alert row for the key — the winner's.
            with registry.cursor() as cr:
                cr.execute(
                    'SELECT COUNT(*), MIN(id), MAX(id) FROM meta_health_alert '
                    'WHERE alert_key = %s', (key,))
                count, min_id, max_id = cr.fetchone()
                self.assertEqual(count, 1)
                self.assertEqual(min_id, max_id)
                self.assertEqual(min_id, results['winner_alert'])
                cr.execute(
                    'SELECT name FROM meta_health_alert WHERE id = %s',
                    (min_id,))
                self.assertEqual(cr.fetchone()[0], 'race winner')
        finally:
            release_a.set()
            for worker in (first, second):
                if worker.ident is not None:
                    worker.join(timeout=self.EVENT_TIMEOUT)
            with registry.cursor() as cr:
                cr.execute(
                    'DELETE FROM meta_health_alert WHERE account_id = %s',
                    (account_id,))
                cr.execute('DELETE FROM meta_account WHERE id = %s', (account_id,))
                cr.commit()
        self.assertFalse(errors, errors)
        # The collision must have surfaced as a constraint/serialization
        # failure, never as a silently inserted second row, and the retry
        # must converge on the winner's incident.
        self.assertIn(results.get('loser_error'),
                      ('UniqueViolation', 'SerializationFailure'))
        self.assertEqual(results['winner_alert'], results['loser_alert'])
