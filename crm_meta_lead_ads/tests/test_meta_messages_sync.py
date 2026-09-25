from datetime import datetime
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestMetaMessagesSync(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Msg Meta', 'company_id': cls.env.company.id,
            'app_id': 'app-msg', 'app_secret': 'secret-msg',
            'user_access_token': 'usertok-MSG-SECRET', 'state': 'connected',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Msg Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '900',
            'page_access_token': 'pagetok-MSG-SECRET',
        })
        cls.Run = cls.env['meta.sync.run']
        cls.Message = cls.env['meta.message']
        cls.Conversation = cls.env['meta.conversation']

    def _patch_request(self, handler):
        def fake(method, path, **kwargs):
            result = handler(path, dict(kwargs.get('params') or {}))
            if isinstance(result, Exception):
                raise result
            return result
        return patch.object(type(self.account), '_request', side_effect=fake)

    def _perms_handler(self, extra):
        def handler(path, params):
            if path == 'me/permissions':
                return {'data': [{'name': 'pages_messaging', 'status': 'granted'}]}
            if path == 'me':
                return {'id': 'user-1'}
            return extra(path, params)
        return handler

    def _run_messages(self):
        run = self.Run.create({
            'account_id': self.account.id, 'company_id': self.env.company.id,
            'run_type': 'messages',
        })
        run.action_start()
        ticks = 0
        while run.state == 'running' and ticks < 25:
            run._process_tick()
            run.invalidate_recordset()
            ticks += 1
        return run

    def _threads(self, psid='PS-100', thread_id='t-1'):
        return {'data': [{
            'id': thread_id,
            'participants': {'data': [{'id': '900'}, {'id': psid}]},
            'updated_time': '2026-09-20T10:00:00+0000',
        }]}

    def _messages(self, items, after=None):
        resp = {'data': items}
        if after:
            resp['paging'] = {'cursors': {'after': after}}
        return resp

    # 1. Historical sync creates conversation + messages with direction.
    def test_history_sync_creates_conversation_and_messages(self):
        def extra(path, params):
            if path == '900/conversations':
                return self._threads()
            if path == 't-1/messages':
                return self._messages([
                    {'id': 'm1', 'message': 'hello', 'from': {'id': 'PS-100'},
                     'created_time': '2026-09-20T10:00:00+0000'},
                    {'id': 'm2', 'message': 'hi there', 'from': {'id': '900'},
                     'created_time': '2026-09-20T10:05:00+0000'},
                ])
            raise AssertionError('unexpected path %s' % path)
        with self._patch_request(self._perms_handler(extra)):
            run = self._run_messages()
        self.assertEqual(run.state, 'completed')
        conv = self.Conversation.search([('psid', '=', 'PS-100'), ('page_id', '=', self.page.id)])
        self.assertEqual(len(conv), 1)
        self.assertEqual(conv.meta_thread_id, 't-1')
        self.assertTrue(conv.history_synced_at)
        msgs = self.Message.search([('conversation_id', '=', conv.id)], order='sent_at asc')
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].direction, 'inbound')
        self.assertEqual(msgs[1].direction, 'outbound')
        self.assertEqual(run.messages_created, 2)
        self.assertEqual(run.conversations_created + run.conversations_updated, 1)
        self.assertEqual(conv.last_message_at.strftime('%Y-%m-%d %H:%M:%S'),
                         '2026-09-20 10:05:00')
        self.assertEqual(conv.last_message_preview, 'hi there')
        self.assertEqual(conv.last_inbound_at.strftime('%Y-%m-%d %H:%M:%S'),
                         '2026-09-20 10:00:00')
        self.assertEqual(conv.first_response_seconds, 300)
        self.assertEqual(conv.unread_count, 0)

    # 2. Messages already delivered by webhook are not duplicated.
    def test_history_dedup_with_webhook(self):
        conv = self.Conversation._get_or_create(self.page, 'PS-100')
        conv.meta_thread_id = 't-1'
        self.Message.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'conversation_id': conv.id, 'sender_psid': 'PS-100',
            'meta_message_id': 'm1', 'message_text': 'hello', 'direction': 'inbound',
        })

        def extra(path, params):
            if path == '900/conversations':
                return self._threads()
            if path == 't-1/messages':
                return self._messages([
                    {'id': 'm1', 'message': 'hello', 'from': {'id': 'PS-100'},
                     'created_time': '2026-09-20T10:00:00+0000'},
                    {'id': 'm2', 'message': 'new', 'from': {'id': 'PS-100'},
                     'created_time': '2026-09-20T10:01:00+0000'},
                ])
            raise AssertionError('unexpected path %s' % path)
        with self._patch_request(self._perms_handler(extra)):
            run = self._run_messages()
        self.assertEqual(self.Message.search_count([('meta_message_id', '=', 'm1')]), 1)
        self.assertEqual(run.messages_duplicate, 1)
        self.assertEqual(run.messages_created, 1)

    # 3. Attachments are stored as metadata only — no download, no HTML.
    def test_attachments_metadata_only(self):
        def extra(path, params):
            if path == '900/conversations':
                return self._threads()
            if path == 't-1/messages':
                return self._messages([{
                    'id': 'm-att', 'from': {'id': 'PS-100'},
                    'created_time': '2026-09-20T10:00:00+0000',
                    'attachments': {'data': [{
                        'mime_type': 'image/jpeg', 'name': 'photo.jpg',
                        'file_url': 'https://cdn.example.test/x.jpg'}]},
                }])
            raise AssertionError('unexpected path %s' % path)
        with self._patch_request(self._perms_handler(extra)):
            self._run_messages()
        msg = self.Message.search([('meta_message_id', '=', 'm-att')])
        self.assertTrue(msg)
        self.assertEqual(msg.message_text, '[image/jpeg]')
        self.assertEqual(msg.attachments_json[0]['name'], 'photo.jpg')
        self.assertIn('image/jpeg', msg.attachments_summary)

    # 4. Messages pagination uses cursors.after on the same endpoint.
    def test_messages_pagination(self):
        calls = []

        def extra(path, params):
            calls.append((path, params.get('after')))
            if path == '900/conversations':
                return self._threads()
            if path == 't-1/messages':
                if params.get('after') == 'c1':
                    return self._messages([{'id': 'm2', 'from': {'id': 'PS-100'},
                                            'created_time': '2026-09-20T10:01:00+0000'}])
                return self._messages([{'id': 'm1', 'from': {'id': 'PS-100'},
                                        'created_time': '2026-09-20T10:00:00+0000'}], after='c1')
            raise AssertionError('unexpected path %s' % path)
        with self._patch_request(self._perms_handler(extra)):
            run = self._run_messages()
        msg_calls = [c for c in calls if c[0] == 't-1/messages']
        self.assertEqual(len(msg_calls), 2)
        self.assertEqual(msg_calls[1][1], 'c1')
        self.assertEqual(run.messages_created, 2)

    # 5. Without pages_messaging the steps are skipped with warnings.
    def test_missing_messaging_permission(self):
        def handler(path, params):
            if path == 'me/permissions':
                return {'data': []}
            if path == 'me':
                return {'id': 'user-1'}
            raise AssertionError('unexpected path %s' % path)
        with self._patch_request(handler):
            run = self._run_messages()
        self.assertEqual(run.state, 'completed_warnings')
        self.assertIn('pages_messaging', run.missing_permissions or '')
        self.assertEqual(run.messages_created, 0)

    # 6. Multi-company: history synced for company A never lands in company B.
    def test_company_isolation(self):
        company_b = self.env['res.company'].create({'name': 'Msg Co B'})

        def extra(path, params):
            if path == '900/conversations':
                return self._threads()
            if path == 't-1/messages':
                return self._messages([{'id': 'm1', 'message': 'hello',
                                        'from': {'id': 'PS-100'},
                                        'created_time': '2026-09-20T10:00:00+0000'}])
            raise AssertionError('unexpected path %s' % path)
        with self._patch_request(self._perms_handler(extra)):
            self._run_messages()
        msg = self.Message.search([('meta_message_id', '=', 'm1')])
        self.assertEqual(msg.company_id, self.env.company)
        self.assertEqual(msg.conversation_id.company_id, self.env.company)
        self.assertFalse(self.Message.search_count([('company_id', '=', company_b.id)]))

    # 7. First-response metric is computed from history (inbound then reply).
    def test_first_response_from_reply(self):
        conv = self.Conversation._get_or_create(self.page, 'PS-FR', values={'state': 'open'})
        self.Message.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'conversation_id': conv.id, 'sender_psid': 'PS-FR',
            'meta_message_id': 'm-in', 'message_text': 'hi', 'direction': 'inbound',
            'sent_at': '2026-09-20 10:00:00',
        })
        conv.last_inbound_at = '2026-09-20 10:00:00'
        msg = conv._record_outbound_message('reply', send_state='sent')
        conv.invalidate_recordset()
        self.assertGreater(conv.first_response_seconds, 0)
        # A second reply never overwrites the first-response metric.
        first = conv.first_response_seconds
        conv._record_outbound_message('reply 2', send_state='sent')
        conv.invalidate_recordset()
        self.assertEqual(conv.first_response_seconds, first)
        self.assertTrue(msg.send_state)

    def test_first_response_uses_first_inbound_not_latest(self):
        conv = self.Conversation._get_or_create(self.page, 'PS-FIRST')
        for mid, timestamp in [('first', '2026-09-20 10:00:00'),
                               ('second', '2026-09-20 10:03:00')]:
            self.Message.create({
                'company_id': self.env.company.id, 'page_id': self.page.id,
                'conversation_id': conv.id, 'sender_psid': 'PS-FIRST',
                'meta_message_id': mid, 'direction': 'inbound', 'sent_at': timestamp,
            })
        conv.last_inbound_at = '2026-09-20 10:03:00'
        reply = conv._record_outbound_message('reply', send_state='sent')
        self.assertEqual(
            conv.first_response_seconds,
            int((reply.sent_at - datetime(2026, 9, 20, 10, 0)).total_seconds()))

    def test_history_ignores_outbound_before_first_inbound(self):
        conv = self.Conversation._get_or_create(self.page, 'PS-OLD')
        for mid, direction, stamp in [
                ('old-out', 'outbound', '2026-09-20 09:55:00'),
                ('new-in', 'inbound', '2026-09-20 10:00:00'),
                ('new-out', 'outbound', '2026-09-20 10:04:00')]:
            self.Message.create({
                'company_id': self.env.company.id, 'page_id': self.page.id,
                'conversation_id': conv.id, 'sender_psid': 'PS-OLD',
                'meta_message_id': mid, 'direction': direction,
                'send_state': 'sent' if direction == 'outbound' else False,
                'sent_at': stamp,
            })
        conv._refresh_history_metrics()
        self.assertEqual(conv.first_response_seconds, 240)
        self.assertEqual(conv.unread_count, 0)

    def test_history_without_sent_reply_has_no_response(self):
        conv = self.Conversation._get_or_create(self.page, 'PS-NO-REPLY')
        self.Message.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'conversation_id': conv.id, 'sender_psid': 'PS-NO-REPLY',
            'meta_message_id': 'in-only', 'message_text': 'customer',
            'direction': 'inbound', 'sent_at': '2026-09-20 10:00:00',
        })
        self.Message.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'conversation_id': conv.id, 'sender_psid': 'PS-NO-REPLY',
            'meta_message_id': 'failed-only', 'message_text': 'unsent reply',
            'direction': 'outbound', 'send_state': 'failed',
            'sent_at': '2026-09-20 10:01:00',
        })
        conv._refresh_history_metrics()
        self.assertEqual(conv.first_response_seconds, 0)
        self.assertEqual(conv.last_message_preview, 'customer')
        self.assertEqual(conv.unread_count, 0)

    def test_history_refresh_corrects_stale_metrics_idempotently(self):
        conv = self.Conversation._get_or_create(self.page, 'PS-STALE')
        self.Message.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'conversation_id': conv.id, 'sender_psid': 'PS-STALE',
            'meta_message_id': 'stale-in', 'message_text': 'new question',
            'direction': 'inbound', 'sent_at': '2026-09-20 10:00:00',
        })
        conv.write({'last_message_preview': 'old preview',
                    'first_response_seconds': 600})
        conv._refresh_history_metrics()
        values = (conv.last_message_at, conv.last_message_preview,
                  conv.last_inbound_at, conv.first_response_seconds)
        self.assertEqual(values[1], 'new question')
        self.assertEqual(values[3], 0)
        conv._refresh_history_metrics()
        self.assertEqual((conv.last_message_at, conv.last_message_preview,
                          conv.last_inbound_at, conv.first_response_seconds), values)
