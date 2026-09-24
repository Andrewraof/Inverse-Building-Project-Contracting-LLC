import logging
import uuid
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MetaConversation(models.Model):
    _name = 'meta.conversation'
    _description = 'Meta Inbox Conversation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'last_message_at desc, id desc'

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    page_id = fields.Many2one('meta.page', required=True, ondelete='cascade', index=True)
    psid = fields.Char(required=True, index=True)
    channel = fields.Selection([('messenger', 'Messenger')], default='messenger', required=True, index=True)
    sender_name = fields.Char(tracking=True)
    partner_id = fields.Many2one('res.partner', index=True)
    lead_id = fields.Many2one('crm.lead', copy=False, index=True)
    assigned_user_id = fields.Many2one('res.users', tracking=True)
    state = fields.Selection([
        ('new', 'New'), ('open', 'Open'), ('pending', 'Pending'), ('closed', 'Closed'),
    ], default='new', required=True, tracking=True, index=True)
    last_message_at = fields.Datetime(index=True)
    last_message_preview = fields.Char()
    unread_count = fields.Integer(default=0, readonly=True)
    meta_message_ids = fields.One2many('meta.message', 'conversation_id')
    reply_draft = fields.Text(string='Reply', store=False)
    active = fields.Boolean(default=True)

    _unique_conversation = models.Constraint(
        'UNIQUE(page_id, psid)',
        'A conversation for this user already exists on this page.')

    @api.depends('sender_name', 'psid')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.sender_name or rec.psid or _('Meta Conversation')

    @api.model
    def _get_or_create(self, page, psid, values=None):
        """Return the single conversation for (page, psid), creating it
        with ``values`` when missing. An existing conversation is
        returned unchanged."""
        psid = str(psid or '')
        if not psid:
            return self.browse()
        Conv = self.with_context(active_test=False)
        conv = Conv.search([('page_id', '=', page.id), ('psid', '=', psid)], limit=1)
        if conv:
            return conv
        vals = {'company_id': page.company_id.id, 'page_id': page.id, 'psid': psid}
        vals.update(values or {})
        try:
            with self.env.cr.savepoint():
                return self.create(vals)
        except IntegrityError:
            return Conv.search([('page_id', '=', page.id), ('psid', '=', psid)], limit=1)

    @api.model
    def _record_inbound_message(self, page, event):
        """Record one Messenger event and update its conversation.

        Duplicate Meta message ids are returned unchanged, so retries do
        not increment unread counters or schedule duplicate activities.
        """
        message_data = event.get('message') or {}
        sender = event.get('sender') or {}
        mid = str(message_data.get('mid') or '')
        psid = str(sender.get('id') or '')
        if message_data.get('is_echo') or not mid or not psid:
            return self.env['meta.message'].browse()

        Message = self.env['meta.message'].sudo()
        existing = Message.search([
            ('meta_message_id', '=', mid),
            ('company_id', '=', page.company_id.id),
        ], limit=1)
        if existing:
            return existing

        sender_name = page._fetch_sender_name(psid)
        conversation = self.sudo()._get_or_create(page, psid, values={
            'sender_name': sender_name,
            'state': 'open',
        })
        message = Message.record_from_webhook(
            page.company_id, page, event, conversation=conversation,
            sender_name=sender_name)
        if not message:
            return message

        vals = {
            'active': True,
            'state': 'open',
            'last_message_at': message.sent_at or message.received_at,
            'last_message_preview': (message.message_text or '')[:100],
            'unread_count': conversation.unread_count + 1,
        }
        if sender_name:
            vals['sender_name'] = sender_name
        conversation.write(vals)
        conversation._schedule_inbox_activity()
        return message

    def _schedule_inbox_activity(self):
        """Schedule at most one open Inbox activity per conversation."""
        todo = self.env.ref('mail.mail_activity_data_todo')
        model_id = self.env['ir.model']._get_id(self._name)
        configured = self.env['ir.config_parameter'].sudo().get_param(
            'crm_meta_lead_ads.inbox_default_user_id')
        for conversation in self:
            user = conversation.assigned_user_id
            if not user and configured:
                try:
                    user = self.env['res.users'].sudo().browse(int(configured)).exists()
                except (TypeError, ValueError):
                    user = self.env['res.users'].browse()
            if not user:
                continue
            existing = self.env['mail.activity'].sudo().search([
                ('res_model_id', '=', model_id),
                ('res_id', '=', conversation.id),
                ('activity_type_id', '=', todo.id),
            ], limit=1)
            if not existing:
                self.env['mail.activity'].sudo().create({
                    'activity_type_id': todo.id,
                    'res_model_id': model_id,
                    'res_id': conversation.id,
                    'user_id': user.id,
                    'summary': _('New Meta Inbox message'),
                    'date_deadline': fields.Date.today(),
                })

    def _close_inbox_activity(self):
        """Mark open Inbox activities as done (e.g. after replying)."""
        todo = self.env.ref('mail.mail_activity_data_todo')
        activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', self._name), ('res_id', 'in', self.ids),
            ('activity_type_id', '=', todo.id),
        ])
        if activities:
            activities.action_done()

    def action_send_reply(self, text):
        self.ensure_one()
        self._send_reply(text)
        return True

    def action_reply_from_form(self):
        self.ensure_one()
        self._send_reply(self.reply_draft)
        self.reply_draft = False
        return True

    def action_assign_to_me(self):
        self.write({'assigned_user_id': self.env.user.id})
        return True

    def action_mark_read(self):
        self.write({'unread_count': 0})
        self._close_inbox_activity()
        return True

    def action_close(self):
        self.write({'state': 'closed'})
        self._close_inbox_activity()
        return True

    def action_reopen(self):
        self.write({'state': 'open'})
        return True

    def action_create_lead(self):
        """Create one CRM lead from this conversation and link both sides.

        A conversation can only ever create a single lead; once linked,
        the form offers ``action_open_lead`` instead."""
        self.ensure_one()
        if self.lead_id:
            raise UserError(_('This conversation is already linked to a lead.'))
        source = self.env.ref('crm_meta_lead_ads.utm_source_meta_messenger', raise_if_not_found=False)
        lead = self.env['crm.lead'].create({
            'name': _('Meta Messenger: %s') % (self.sender_name or self.psid),
            'type': 'lead',
            'company_id': self.company_id.id,
            'user_id': self.assigned_user_id.id or self.env.user.id,
            'partner_name': self.sender_name or False,
            'source_id': source.id if source else False,
            'meta_conversation_id': self.id,
        })
        self.lead_id = lead.id
        lead.message_post(body=_(
            'Created from Meta Inbox conversation (page: %(page)s, PSID: %(psid)s).',
            page=self.page_id.name, psid=self.psid))
        return lead

    def action_open_lead(self):
        self.ensure_one()
        if not self.lead_id:
            raise UserError(_('No lead is linked to this conversation yet.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': self.lead_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _check_reply_allowed(self):
        """Pre-flight checks before calling the Messenger Send API.

        Meta remains the final authority inside the 24-hour window; we
        only block replies that are guaranteed to be rejected.
        """
        self.ensure_one()
        if self.state == 'closed':
            raise UserError(_('This conversation is closed. Reopen it before replying.'))
        account = self.page_id.account_id
        if account.state != 'connected':
            raise UserError(_('The Meta account is not connected. Reconnect it before replying.'))
        if not self.page_id.page_access_token:
            raise UserError(_('No Page Access Token is stored for this page. Reconnect via Meta OAuth.'))
        last_inbound = self.env['meta.message'].sudo().search([
            ('conversation_id', '=', self.id), ('direction', '=', 'inbound'),
        ], order='sent_at desc, id desc', limit=1)
        if not last_inbound:
            raise UserError(_(
                'No inbound message from the customer yet; Meta only allows replying '
                'after the customer writes first.'))
        reference = last_inbound.sent_at or last_inbound.received_at
        if reference and fields.Datetime.now() - reference > timedelta(hours=24):
            raise UserError(_(
                'More than 24 hours passed since the customer\'s last message, so Meta '
                'no longer accepts a standard reply in this conversation.'))
        return account

    def _send_reply(self, text):
        self.ensure_one()
        text = (text or '').strip()
        if not text:
            raise UserError(_('Cannot send an empty reply.'))
        account = self._check_reply_allowed()
        page = self.page_id
        token = page.page_access_token
        try:
            data = account._request('POST', 'me/messages', token=token, json={
                'recipient': {'id': self.psid},
                'message': {'text': text},
                'messaging_type': 'RESPONSE',
            })
        except Exception as exc:
            reason = account._sanitize_error(
                exc, [token, account.user_access_token, account.app_secret])
            self._record_outbound_message(text, send_state='failed', failure_reason=reason)
            raise UserError(_('Meta rejected the reply: %s') % reason) from exc
        message = self._record_outbound_message(
            text, send_state='sent', meta_message_id=data.get('message_id') or data.get('id'))
        self.write({
            'state': 'open',
            'last_message_at': fields.Datetime.now(),
            'last_message_preview': text[:100],
            'unread_count': 0,
        })
        self._close_inbox_activity()
        return message

    def _record_outbound_message(self, text, send_state='sent', meta_message_id=None, failure_reason=None):
        self.ensure_one()
        return self.env['meta.message'].sudo().create({
            'company_id': self.company_id.id,
            'page_id': self.page_id.id,
            'conversation_id': self.id,
            'sender_psid': self.psid,
            'sender_name': self.page_id.name,
            'message_text': text,
            'meta_message_id': meta_message_id or 'local-%s-%s' % (send_state, uuid.uuid4().hex),
            'direction': 'outbound',
            'send_state': send_state,
            'failure_reason': failure_reason,
            'sent_at': fields.Datetime.now(),
        })

    @api.model
    def _link_legacy_messages(self):
        """Group messages recorded before conversations existed into
        conversations per (company, page, psid). Idempotent: only
        messages without a conversation are processed, an existing
        conversation is reused (never recreated), and no message
        content is deleted or modified beyond the conversation link."""
        Message = self.env['meta.message'].sudo()
        Page = self.env['meta.page'].sudo()
        self.env.cr.execute("""
            SELECT page_id, sender_psid FROM meta_message
            WHERE conversation_id IS NULL
            GROUP BY page_id, sender_psid
        """)
        created = 0
        for page_id, psid in self.env.cr.fetchall():
            msgs = Message.search([
                ('page_id', '=', page_id), ('sender_psid', '=', psid),
                ('conversation_id', '=', False),
            ], order='sent_at desc, id desc')
            if not msgs:
                continue
            latest = msgs[0]
            existed = self.with_context(active_test=False).search_count(
                [('page_id', '=', page_id), ('psid', '=', psid)])
            conv = self._get_or_create(Page.browse(page_id), psid, values={
                'sender_name': latest.sender_name, 'state': 'open',
                'last_message_at': latest.sent_at or latest.received_at,
                'last_message_preview': (latest.message_text or '')[:100],
            })
            if not conv:
                continue
            created += 0 if existed else 1
            msgs.write({'conversation_id': conv.id})
        if created:
            _logger.info('Meta inbox: linked legacy messages into %s new conversation(s).', created)
        return created
