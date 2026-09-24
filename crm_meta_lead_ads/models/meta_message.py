import logging
from datetime import datetime, timezone

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MetaMessage(models.Model):
    _name = 'meta.message'
    _description = 'Meta Page Incoming Message'
    _order = 'sent_at desc, id desc'

    company_id = fields.Many2one('res.company', required=True, index=True)
    page_id = fields.Many2one('meta.page', required=True, ondelete='cascade', index=True)
    sender_psid = fields.Char(required=True, index=True)
    sender_name = fields.Char(readonly=True)
    message_text = fields.Text(readonly=True)
    meta_message_id = fields.Char(required=True, index=True)
    is_first_message = fields.Boolean(readonly=True)
    sent_at = fields.Datetime(readonly=True, index=True)
    received_at = fields.Datetime(default=fields.Datetime.now, required=True)
    payload_json = fields.Json(readonly=True, groups='base.group_system')
    conversation_id = fields.Many2one('meta.conversation', ondelete='cascade', index=True)
    direction = fields.Selection([('inbound', 'Inbound'), ('outbound', 'Outbound')],
                                 default='inbound', required=True, index=True)
    send_state = fields.Selection([('sent', 'Sent'), ('failed', 'Failed')], readonly=True, copy=False)
    failure_reason = fields.Char(readonly=True)
    attachments_json = fields.Json(readonly=True, groups='base.group_system')

    _unique_message = models.Constraint('UNIQUE(meta_message_id, company_id)', 'This Meta message is already recorded.')

    @api.model
    def record_from_webhook(self, company, page, event, conversation=None, sender_name=None):
        sender = event.get('sender') or {}
        message = event.get('message') or {}
        if message.get('is_echo'):
            return self.browse()
        mid = message.get('mid')
        psid = str(sender.get('id') or '')
        if not mid or not psid:
            return self.browse()
        existing = self.sudo().search([('meta_message_id', '=', mid), ('company_id', '=', company.id)], limit=1)
        if existing:
            return existing
        first = not self.sudo().search_count([('sender_psid', '=', psid), ('page_id', '=', page.id)])
        text = message.get('text') or ''
        if not text and message.get('attachments'):
            text = '[%s]' % (message['attachments'][0].get('type') or 'attachment')
        ts = event.get('timestamp')
        sent_at = fields.Datetime.now()
        if ts:
            try:
                sent_at = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError, OSError):
                pass
        attachments = []
        for attachment in message.get('attachments') or []:
            payload = attachment.get('payload') or {}
            attachments.append({
                'type': attachment.get('type') or 'attachment',
                'url': payload.get('url') or False,
                'name': payload.get('name') or attachment.get('name') or False,
            })
        return self.sudo().create({
            'company_id': company.id, 'page_id': page.id, 'sender_psid': psid,
            'sender_name': sender_name if sender_name is not None else page._fetch_sender_name(psid),
            'message_text': text,
            'meta_message_id': mid, 'is_first_message': first,
            'sent_at': sent_at, 'payload_json': event,
            'conversation_id': conversation.id if conversation else False,
            'direction': 'inbound', 'attachments_json': attachments,
        })
