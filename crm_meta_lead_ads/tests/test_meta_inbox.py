from datetime import timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestMetaInbox(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Inbox Meta', 'company_id': cls.env.company.id,
            'app_id': 'app', 'app_secret': 'secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Inbox Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '100', 'page_access_token': 'token',
        })
        cls.page2 = cls.env['meta.page'].create({
            'name': 'Inbox Page 2', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '101', 'page_access_token': 'token2',
        })
        cls.Conversation = cls.env['meta.conversation']
        cls.Message = cls.env['meta.message']

    def _legacy_message(self, psid, mid, text='hello', page=None):
        return self.Message.create({
            'company_id': self.env.company.id, 'page_id': (page or self.page).id,
            'sender_psid': psid, 'meta_message_id': mid, 'message_text': text,
        })

    def _messaging_event(self, mid='inbound-1', psid='customer-1', text='Hello',
                         timestamp=1727172000000, attachments=None):
        message = {'mid': mid}
        if text is not None:
            message['text'] = text
        if attachments is not None:
            message['attachments'] = attachments
        return {
            'sender': {'id': psid}, 'recipient': {'id': self.page.meta_page_id},
            'timestamp': timestamp, 'message': message,
        }

    def test_get_or_create_conversation(self):
        conv = self.Conversation._get_or_create(self.page, 'psid-1')
        self.assertTrue(conv.exists())
        self.assertEqual(conv.channel, 'messenger')
        self.assertEqual(conv.state, 'new')
        again = self.Conversation._get_or_create(self.page, 'psid-1')
        self.assertEqual(conv, again)
        conv.write({'active': False})
        self.assertEqual(self.Conversation._get_or_create(self.page, 'psid-1'), conv)

    def test_conversation_unique_per_page_psid(self):
        self.Conversation._get_or_create(self.page, 'psid-2')
        # The same psid on another page is a different conversation.
        other = self.Conversation._get_or_create(self.page2, 'psid-2')
        self.assertEqual(other.page_id, self.page2)
        # A duplicate on the same page violates the unique constraint.
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Conversation.create({
                    'company_id': self.env.company.id, 'page_id': self.page.id, 'psid': 'psid-2',
                })

    def test_link_legacy_messages(self):
        m1 = self._legacy_message('psid-A', 'mid-A1', 'first')
        m2 = self._legacy_message('psid-A', 'mid-A2', 'latest A')
        m3 = self._legacy_message('psid-B', 'mid-B1', 'latest B', page=self.page2)
        created = self.Conversation._link_legacy_messages()
        self.assertEqual(created, 2)
        for msg in (m1 | m2 | m3):
            msg.invalidate_recordset()
            self.assertTrue(msg.conversation_id)
        conv_a = m2.conversation_id
        self.assertEqual(conv_a.psid, 'psid-A')
        self.assertEqual(conv_a.state, 'open')
        self.assertEqual(conv_a.last_message_preview, 'latest A')
        self.assertTrue(conv_a.last_message_at)
        self.assertNotEqual(m2.conversation_id, m3.conversation_id)
        self.assertEqual(self.Conversation._link_legacy_messages(), 0)

    def test_link_legacy_messages_reuses_existing_conversation(self):
        conv = self.Conversation.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'psid': 'psid-C', 'state': 'open', 'last_message_preview': 'keep me',
        })
        msg = self._legacy_message('psid-C', 'mid-C1', 'orphan')
        created = self.Conversation._link_legacy_messages()
        self.assertEqual(created, 0)
        msg.invalidate_recordset()
        self.assertEqual(msg.conversation_id, conv)
        self.assertEqual(conv.last_message_preview, 'keep me')
        self.assertEqual(self.Conversation.search_count(
            [('page_id', '=', self.page.id), ('psid', '=', 'psid-C')]), 1)

    def test_conversation_keeps_mail_thread_message_ids(self):
        # Regression: the One2many to meta.message must not override
        # mail.thread.message_ids (mail.message), otherwise linking
        # messages crashes with KeyError 'message_type'.
        conv = self.Conversation._get_or_create(self.page, 'psid-D')
        msg = self._legacy_message('psid-D', 'mid-D1', 'orphan')
        self.Conversation._link_legacy_messages()
        msg.invalidate_recordset()
        self.assertEqual(msg.conversation_id, conv)
        self.assertIn(msg, conv.meta_message_ids)
        self.assertEqual(conv.message_ids._name, 'mail.message')
        posted = conv.message_post(body='inbox note')
        self.assertIn(posted, conv.message_ids)

    def test_default_notify_user_setting(self):
        settings = self.env['res.config.settings'].create({
            'meta_inbox_default_user_id': self.env.user.id,
        })
        settings.execute()
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param('crm_meta_lead_ads.inbox_default_user_id'),
            str(self.env.user.id))

    def test_conversation_company_rule(self):
        company2 = self.env['res.company'].create({'name': 'Other Co'})
        account2 = self.env['meta.account'].create({
            'name': 'Other Meta', 'company_id': company2.id, 'app_id': 'a2', 'app_secret': 's2'})
        page_b = self.env['meta.page'].create({
            'name': 'Other Page', 'company_id': company2.id, 'account_id': account2.id,
            'meta_page_id': '200', 'page_access_token': 'tokB'})
        conv_a = self.Conversation.create({
            'company_id': self.env.company.id, 'page_id': self.page.id, 'psid': 'psid-A2'})
        conv_b = self.Conversation.create({
            'company_id': company2.id, 'page_id': page_b.id, 'psid': 'psid-B2'})
        group_user = self.env.ref('crm_meta_lead_ads.group_meta_lead_user')
        user = self.env['res.users'].create({
            'name': 'Inbox User', 'login': 'inbox_user_test',
            'company_id': self.env.company.id, 'company_ids': [(6, 0, [self.env.company.id])],
            'group_ids': [(6, 0, [group_user.id])],
        })
        visible = self.Conversation.with_user(user).search([('id', 'in', [conv_a.id, conv_b.id])])
        self.assertIn(conv_a, visible)
        self.assertNotIn(conv_b, visible)

    def test_record_inbound_message_creates_conversation_and_updates_unread(self):
        event = self._messaging_event(attachments=[{
            'type': 'image', 'payload': {'url': 'https://cdn.example/image'},
        }])
        with patch.object(type(self.page), '_fetch_sender_name', return_value='Customer One'):
            message = self.Conversation._record_inbound_message(self.page, event)
        conversation = message.conversation_id
        self.assertTrue(conversation)
        self.assertEqual(conversation.sender_name, 'Customer One')
        self.assertEqual(conversation.last_message_preview, 'Hello')
        self.assertEqual(conversation.unread_count, 1)
        self.assertEqual(conversation.state, 'open')
        self.assertEqual(message.direction, 'inbound')
        self.assertEqual(message.attachments_json, [{
            'type': 'image', 'url': 'https://cdn.example/image', 'name': False,
        }])

    def test_duplicate_inbound_message_does_not_increment_unread(self):
        event = self._messaging_event(mid='duplicate-mid')
        with patch.object(type(self.page), '_fetch_sender_name', return_value='Customer'):
            first = self.Conversation._record_inbound_message(self.page, event)
            duplicate = self.Conversation._record_inbound_message(self.page, event)
        self.assertEqual(first, duplicate)
        self.assertEqual(first.conversation_id.unread_count, 1)
        self.assertEqual(self.Message.search_count([
            ('meta_message_id', '=', 'duplicate-mid'),
            ('company_id', '=', self.env.company.id),
        ]), 1)

    def test_new_inbound_message_reopens_closed_conversation(self):
        conversation = self.Conversation.create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'psid': 'customer-closed', 'state': 'closed', 'active': False,
        })
        with patch.object(type(self.page), '_fetch_sender_name', return_value='Returning Customer'):
            message = self.Conversation._record_inbound_message(
                self.page, self._messaging_event(mid='reopen-mid', psid='customer-closed'))
        self.assertEqual(message.conversation_id, conversation)
        self.assertTrue(conversation.active)
        self.assertEqual(conversation.state, 'open')

    def test_attachment_only_message_uses_safe_preview(self):
        event = self._messaging_event(
            mid='attachment-mid', text=None,
            attachments=[{'type': 'file', 'payload': {
                'url': 'https://cdn.example/file', 'name': 'quote.pdf',
            }}])
        with patch.object(type(self.page), '_fetch_sender_name', return_value=False):
            message = self.Conversation._record_inbound_message(self.page, event)
        self.assertEqual(message.message_text, '[file]')
        self.assertEqual(message.conversation_id.last_message_preview, '[file]')

    def test_inbox_activity_is_not_duplicated(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'crm_meta_lead_ads.inbox_default_user_id', self.env.user.id)
        with patch.object(type(self.page), '_fetch_sender_name', return_value='Customer'):
            first = self.Conversation._record_inbound_message(
                self.page, self._messaging_event(mid='activity-1'))
            self.Conversation._record_inbound_message(
                self.page, self._messaging_event(mid='activity-2'))
        activities = self.env['mail.activity'].search([
            ('res_model', '=', 'meta.conversation'),
            ('res_id', '=', first.conversation_id.id),
        ])
        self.assertEqual(len(activities), 1)

    def _reply_ready_conversation(self, psid='reply-customer', hours_ago=1):
        self.account.state = 'connected'
        with patch.object(type(self.page), '_fetch_sender_name', return_value='Customer'):
            message = self.Conversation._record_inbound_message(
                self.page, self._messaging_event(mid='in-%s' % psid, psid=psid))
        message.write({'sent_at': fields.Datetime.now() - timedelta(hours=hours_ago)})
        return message.conversation_id

    def _patch_send_api(self, payload=None, status=200):
        payload = payload if payload is not None else {'message_id': 'm.out.1', 'recipient_id': 'reply-customer'}

        class FakeResponse:
            status_code = status
            content = b'x'
            text = 'response'

            def json(self):
                return payload

        return patch(
            'odoo.addons.crm_meta_lead_ads.models.meta_account.requests.request',
            return_value=FakeResponse())

    def test_reply_rejects_empty_text(self):
        conversation = self._reply_ready_conversation('reply-empty')
        with self.assertRaises(UserError):
            conversation.action_send_reply('   ')

    def test_reply_rejects_disconnected_account(self):
        conversation = self._reply_ready_conversation('reply-disc')
        self.account.state = 'disconnected'
        with self.assertRaises(UserError):
            conversation.action_send_reply('hello')

    def test_reply_rejects_missing_page_token(self):
        conversation = self._reply_ready_conversation('reply-notoken')
        self.page.write({'sync_enabled': False, 'page_access_token': False})
        with self.assertRaises(UserError):
            conversation.action_send_reply('hello')
        self.page.write({'sync_enabled': True, 'page_access_token': 'token'})

    def test_reply_rejects_closed_conversation(self):
        conversation = self._reply_ready_conversation('reply-closed')
        conversation.state = 'closed'
        with self.assertRaises(UserError):
            conversation.action_send_reply('hello')

    def test_reply_rejects_without_inbound_message(self):
        self.account.state = 'connected'
        conversation = self.Conversation.create({
            'company_id': self.env.company.id, 'page_id': self.page.id, 'psid': 'silent-customer',
        })
        with self.assertRaises(UserError):
            conversation.action_send_reply('hello')

    def test_reply_rejects_after_24_hours(self):
        conversation = self._reply_ready_conversation('reply-late', hours_ago=25)
        with self.assertRaises(UserError):
            conversation.action_send_reply('too late')

    def test_reply_success_creates_outbound_and_resets(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'crm_meta_lead_ads.inbox_default_user_id', self.env.user.id)
        conversation = self._reply_ready_conversation('reply-customer')
        with self._patch_send_api():
            message = conversation.action_send_reply('Reply text')
        self.assertEqual(message.direction, 'outbound')
        self.assertEqual(message.send_state, 'sent')
        self.assertEqual(message.meta_message_id, 'm.out.1')
        self.assertEqual(message.conversation_id, conversation)
        conversation.invalidate_recordset()
        self.assertEqual(conversation.unread_count, 0)
        self.assertEqual(conversation.last_message_preview, 'Reply text')
        open_activities = self.env['mail.activity'].search([
            ('res_model', '=', 'meta.conversation'), ('res_id', '=', conversation.id),
        ])
        self.assertFalse(open_activities)

    def test_reply_failure_is_recorded_and_sanitized(self):
        leak = 'pagetok-LEAK-999'
        self.page.write({'page_access_token': leak})
        conversation = self._reply_ready_conversation('reply-fail')
        error_payload = {'error': {'code': 190, 'message': 'Invalid token %s' % leak}}
        with self._patch_send_api(payload=error_payload, status=400):
            with self.assertRaises(UserError) as ctx:
                conversation.action_send_reply('hello again')
        self.page.write({'page_access_token': 'token'})
        self.account.state = 'connected'
        self.assertNotIn(leak, str(ctx.exception))
        failed = self.Message.search([
            ('conversation_id', '=', conversation.id), ('direction', '=', 'outbound')])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed.send_state, 'failed')
        self.assertNotIn(leak, failed.failure_reason or '')
