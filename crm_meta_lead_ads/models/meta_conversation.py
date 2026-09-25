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
    last_inbound_at = fields.Datetime(readonly=True, index=True,
                                      help='Last customer message; drives the 24h window and overdue filters.')
    first_response_seconds = fields.Integer(readonly=True, copy=False,
                                            help='Seconds from the first inbound message to the first page reply.')
    first_response_at = fields.Datetime(readonly=True, copy=False, index=True,
                                        help='Timestamp of the first successful page reply after the first inbound message.')
    meta_thread_id = fields.Char(readonly=True, copy=False, index=True,
                                 help='Meta conversation thread id, set by historical sync.')
    history_synced_at = fields.Datetime(readonly=True, copy=False)
    unread_count = fields.Integer(default=0, readonly=True)
    meta_message_ids = fields.One2many('meta.message', 'conversation_id')
    reply_draft = fields.Text(string='Reply')
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

        # Serialize concurrent webhook deliveries for the same conversation
        # so the unread increment below is never lost between transactions.
        if conversation.id:
            self.env.cr.execute(
                'SELECT pg_advisory_xact_lock(%s)',
                (int(conversation.id) + 2100000000,))
        conversation.invalidate_recordset(['unread_count'])
        vals = {
            'active': True,
            'state': 'open',
            'last_message_at': message.sent_at or message.received_at,
            'last_inbound_at': message.sent_at or message.received_at,
            'last_message_preview': (message.message_text or '')[:100],
            'unread_count': conversation.unread_count + 1,
        }
        if sender_name:
            vals['sender_name'] = sender_name
        conversation.write(vals)
        if not conversation.assigned_user_id:
            self.env['meta.routing.rule'].sudo().apply_for_conversation(conversation)
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
        result = self._send_reply(text)
        if result['error']:
            return self._reply_notification('danger', _('Meta rejected the reply: %s') % result['error'])
        return self._reply_notification('success', _('Reply sent to the customer.'))

    def action_reply_from_form(self):
        self.ensure_one()
        result = self._send_reply(self.reply_draft)
        if result['error']:
            # Keep reply_draft so the user can fix and retry.
            return self._reply_notification('danger', _('Meta rejected the reply: %s') % result['error'])
        self.reply_draft = False
        return self._reply_notification('success', _('Reply sent to the customer.'))

    def _reply_notification(self, notif_type, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Meta Inbox'),
                'message': message,
                'type': notif_type,
                'sticky': notif_type != 'success',
            },
        }

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
        the form offers ``action_open_lead`` instead. When a partner is
        linked to the conversation its contact details seed the lead;
        the sender display name alone is never used as a match key."""
        self.ensure_one()
        if self.lead_id:
            raise UserError(_('This conversation is already linked to a lead.'))
        source = self.env.ref('crm_meta_lead_ads.utm_source_meta_messenger', raise_if_not_found=False)
        partner = self.partner_id
        lead = self.env['crm.lead'].create({
            'name': _('Meta Messenger: %s') % (self.sender_name or self.psid),
            'type': 'lead',
            'company_id': self.company_id.id,
            'user_id': self.assigned_user_id.id or self.env.user.id,
            'partner_id': partner.id if partner else False,
            'partner_name': (partner.name if partner else self.sender_name) or False,
            'email_from': partner.email if partner and partner.email else False,
            'phone': partner.phone if partner and partner.phone else False,
            'source_id': source.id if source else False,
            'meta_conversation_id': self.id,
        })
        self.lead_id = lead.id
        lead.message_post(body=_(
            'Created from Meta Inbox conversation (page: %(page)s, PSID: %(psid)s).',
            page=self.page_id.name, psid=self.psid))
        return lead

    def action_link_lead(self):
        """Open the wizard that links this conversation to an existing lead."""
        self.ensure_one()
        if self.lead_id:
            raise UserError(_('This conversation is already linked to a lead.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Link Existing Lead'),
            'res_model': 'meta.conversation.link.lead.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_conversation_id': self.id},
        }

    def _link_to_lead(self, lead):
        """Link this conversation to an existing CRM lead, both ways.

        Guards: a conversation links to one lead only, a lead links to
        one conversation only, and cross-company links are refused.
        Nothing about the lead's salesperson, stage or contact data is
        modified."""
        self.ensure_one()
        if not lead:
            raise UserError(_('Select a CRM lead to link first.'))
        # Lock in a stable order (lead, then conversation). A concurrent
        # link must commit before we re-read either side; otherwise two
        # conversations can both believe they own the same lead.
        self.env.cr.execute('SELECT id FROM crm_lead WHERE id = %s FOR UPDATE',
                            (lead.id,))
        if not self.env.cr.fetchone():
            raise UserError(_('The selected CRM lead no longer exists.'))
        self.env.cr.execute('SELECT id FROM meta_conversation WHERE id = %s FOR UPDATE',
                            (self.id,))
        lead.invalidate_recordset(['meta_conversation_id', 'company_id'])
        self.invalidate_recordset(['lead_id'])
        if self.lead_id:
            raise UserError(_('This conversation is already linked to a lead.'))
        if lead.company_id and lead.company_id != self.company_id:
            raise UserError(_('The selected lead belongs to another company.'))
        if lead.meta_conversation_id and lead.meta_conversation_id != self:
            raise UserError(_('The selected lead is already linked to another Meta conversation.'))
        self.lead_id = lead.id
        if lead.meta_conversation_id != self:
            lead.meta_conversation_id = self.id
        lead.message_post(body=_(
            'Linked to Meta Inbox conversation (page: %(page)s, PSID: %(psid)s).',
            page=self.page_id.name, psid=self.psid))
        self.message_post(body=_(
            'Linked to CRM lead %s by %s.') % (lead.id, self.env.user.name))
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
        """Send a Messenger reply. Returns ``{'message': record, 'error': False}``
        on success, or ``{'message': False, 'error': reason}`` after persisting
        a failed outbound message. Delivery failures must NOT raise: raising
        after the failed message is written would roll it back with the
        request, hiding the failure from the database. Pre-send validation
        (closed conversation, expired window, empty text, ...) raises
        UserError as before because nothing has been written yet."""
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
            return {'message': False, 'error': reason}
        message_id = str(data.get('message_id') or '').strip() if isinstance(data, dict) else ''
        if not message_id:
            reason = _('Meta response did not include message_id')
            self._record_outbound_message(text, send_state='failed', failure_reason=reason)
            return {'message': False, 'error': reason}
        message = self._record_outbound_message(
            text, send_state='sent', meta_message_id=message_id)
        # Pending = waiting on the customer; the next inbound flips the
        # conversation back to open.
        self.write({
            'state': 'pending',
            'last_message_at': fields.Datetime.now(),
            'last_message_preview': text[:100],
            'unread_count': 0,
        })
        self._close_inbox_activity()
        return {'message': message, 'error': False}

    def _record_outbound_message(self, text, send_state='sent', meta_message_id=None, failure_reason=None):
        self.ensure_one()
        message = self.env['meta.message'].sudo().create({
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
        if send_state == 'sent':
            self._refresh_history_metrics()
        return message

    def _refresh_history_metrics(self):
        """Derive Inbox timestamps and first response from stored messages.

        Historical imports and live replies use the same definition: the
        first successful page reply after the first customer message.
        Recalculation never marks historical messages unread.
        """
        Message = self.env['meta.message'].sudo()
        for conversation in self:
            messages = Message.search([('conversation_id', '=', conversation.id)])
            ordered = sorted(
                (m for m in messages if m.direction == 'inbound' or m.send_state == 'sent'),
                key=lambda m: (m.sent_at or m.received_at, m.id),
            )
            if not ordered:
                continue
            latest = ordered[-1]
            inbound = [m for m in ordered if m.direction == 'inbound']
            first_inbound = inbound[0] if inbound else False
            first_at = (first_inbound.sent_at or first_inbound.received_at) if first_inbound else False
            first_reply = next((m for m in ordered
                                if first_at and m.direction == 'outbound'
                                and m.send_state == 'sent'
                                and (m.sent_at or m.received_at) >= first_at), False)
            response_seconds = int(((first_reply.sent_at or first_reply.received_at) - first_at).total_seconds()) \
                if first_reply else 0
            vals = {
                'last_message_at': latest.sent_at or latest.received_at,
                'last_message_preview': (latest.message_text or '')[:100],
                'last_inbound_at': (inbound[-1].sent_at or inbound[-1].received_at) if inbound else False,
                'first_response_seconds': response_seconds,
                'first_response_at': (first_reply.sent_at or first_reply.received_at) if first_reply else False,
            }
            changed = {field: value for field, value in vals.items()
                       if conversation[field] != value}
            if changed:
                conversation.write(changed)

    def action_convert_to_opportunity(self):
        """Convert (or create-then-convert) the linked CRM lead to an
        opportunity. Never creates a second lead for the conversation."""
        self.ensure_one()
        lead = self.lead_id or self.action_create_lead()
        if lead.type != 'opportunity':
            lead.write({'type': 'opportunity'})
            lead.message_post(
                body=_('Converted to opportunity from Meta Inbox conversation %s.') % self.id,
                message_type='comment', subtype_xmlid='mail.mt_note')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': lead.id,
            'view_mode': 'form',
            'target': 'current',
        }

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
