from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from .meta_dedup import normalize_email, normalize_phone


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    meta_lead_id = fields.Char(index=True, copy=False, readonly=True)
    meta_form_id = fields.Many2one('meta.form', copy=False, readonly=True, index=True)
    meta_page_id = fields.Many2one('meta.page', copy=False, readonly=True, index=True)
    meta_platform = fields.Selection([('facebook','Facebook'),('instagram','Instagram'),('unknown','Unknown')], copy=False, readonly=True)
    meta_ad_id = fields.Char(index=True, copy=False, readonly=True)
    # Legacy name: actually stores the adset ID. Kept for backward
    # compatibility; meta_adset_id is the canonical field and both are
    # always written with the same value.
    meta_adgroup_id = fields.Char(copy=False, readonly=True)
    meta_adset_id = fields.Char(index=True, copy=False, readonly=True)
    meta_campaign_id = fields.Char(index=True, copy=False, readonly=True)
    meta_ad_name = fields.Char(copy=False, readonly=True)
    meta_adset_name = fields.Char(copy=False, readonly=True)
    meta_campaign_name = fields.Char(copy=False, readonly=True)
    meta_is_organic = fields.Boolean(copy=False, readonly=True)
    meta_raw_payload = fields.Json(copy=False, readonly=True, groups='base.group_system')
    meta_conversation_id = fields.Many2one('meta.conversation', copy=False, readonly=True, index=True)
    # Normalized dedup match keys, derived from email_from/phone. The
    # original values stay untouched; these columns exist only so the
    # Meta dedup matcher can do exact, indexable comparisons.
    meta_norm_email = fields.Char(index=True, copy=False, readonly=True)
    meta_norm_phone = fields.Char(index=True, copy=False, readonly=True)
    meta_identity_ids = fields.One2many(
        'meta.lead.identity', 'crm_lead_id', readonly=True,
        groups='crm_meta_lead_ads.group_meta_lead_user')
    meta_identity_count = fields.Integer(
        compute='_compute_meta_identity_count',
        groups='crm_meta_lead_ads.group_meta_lead_user')
    meta_routing_rule_id = fields.Many2one('meta.routing.rule', copy=False, readonly=True,
                                           help='Routing rule that assigned this lead, if any.')
    meta_export_notes = fields.Text(
        string='Notes (export)', compute='_compute_meta_export_notes',
        compute_sudo=False, readonly=True, copy=False,
        help='Readable Notes tab plus internal chatter notes visible to the exporting user.')
    meta_export_open_activities = fields.Text(
        string='Open Activities (export)', compute='_compute_meta_export_open_activities',
        compute_sudo=False, readonly=True, copy=False)
    meta_export_done_activities = fields.Text(
        string='Completed Activities (export)', compute='_compute_meta_export_done_activities',
        compute_sudo=False, readonly=True, copy=False,
        help='Completed activity messages, including recorded feedback. No inferred history.')

    def _meta_export_messages_by_lead(self, domain):
        """Batch under caller permissions, never reuse privileged computed values."""
        self.check_access('read')
        ids = [record_id for record_id in self.ids if isinstance(record_id, int)]
        grouped = {record_id: [] for record_id in ids}
        if ids:
            messages = self.env['mail.message'].search([
                ('model', '=', 'crm.lead'), ('res_id', 'in', ids),
            ] + domain, order='date, id')
            for message in messages:
                grouped[message.res_id].append(message)
        return grouped

    @api.depends_context('uid')
    @api.depends('description', 'message_ids.body', 'message_ids.subtype_id')
    def _compute_meta_export_notes(self):
        messages = self._meta_export_messages_by_lead([
            ('message_type', '=', 'comment'),
            ('subtype_id', '=', self.env.ref('mail.mt_note').id),
        ])
        for lead in self:
            parts = [html2plaintext(lead.description or '').strip()]
            for message in messages.get(lead.id, []):
                body = html2plaintext(message.body or '').strip()
                if body:
                    parts.append('%s | %s | %s' % (
                        fields.Datetime.to_string(message.date) or '',
                        message.author_id.name or '', body))
            lead.meta_export_notes = '\n'.join(part for part in parts if part)

    @api.depends_context('uid', 'active_test')
    @api.depends('activity_ids.summary', 'activity_ids.note',
                 'activity_ids.date_deadline', 'activity_ids.user_id',
                 'activity_ids.activity_type_id', 'activity_ids.active')
    def _compute_meta_export_open_activities(self):
        self.check_access('read')
        ids = [record_id for record_id in self.ids if isinstance(record_id, int)]
        grouped = {record_id: [] for record_id in ids}
        if ids:
            activities = self.env['mail.activity'].search([
                ('res_model', '=', 'crm.lead'), ('res_id', 'in', ids),
                ('active', '=', True),
            ], order='date_deadline, id')
            for activity in activities:
                parts = [activity.activity_type_id.name, activity.summary,
                         'due %s' % activity.date_deadline if activity.date_deadline else '',
                         activity.user_id.name, html2plaintext(activity.note or '').strip()]
                grouped[activity.res_id].append(' | '.join(part for part in parts if part))
        for lead in self:
            lead.meta_export_open_activities = '\n'.join(grouped.get(lead.id, []))

    @api.depends_context('uid')
    @api.depends('message_ids.body', 'message_ids.mail_activity_type_id')
    def _compute_meta_export_done_activities(self):
        messages = self._meta_export_messages_by_lead([
            ('mail_activity_type_id', '!=', False),
        ])
        for lead in self:
            lead.meta_export_done_activities = '\n'.join(
                '%s | %s | %s' % (
                    fields.Datetime.to_string(message.date) or '',
                    message.mail_activity_type_id.name or '',
                    html2plaintext(message.body or '').strip())
                for message in messages.get(lead.id, []))

    @api.depends('meta_identity_ids')
    def _compute_meta_identity_count(self):
        for rec in self:
            rec.meta_identity_count = len(rec.meta_identity_ids)

    def action_open_meta_identities(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': 'Meta Lead IDs',
            'res_model': 'meta.lead.identity',
            'domain': [('crm_lead_id', '=', self.id)],
            'view_mode': 'list,form', 'target': 'current',
        }

    def action_open_meta_conversation(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': 'Meta Conversation',
            'res_model': 'meta.conversation', 'res_id': self.meta_conversation_id.id,
            'view_mode': 'form', 'target': 'current',
        }

    _unique_meta_lead = models.Constraint('UNIQUE(meta_lead_id)', 'This Meta lead was already imported.')

    def _meta_uae_context(self):
        self.ensure_one()
        country = self.company_id.country_id
        return bool(country and country.code == 'AE')

    def _meta_norm_vals(self):
        self.ensure_one()
        return {
            'meta_norm_email': normalize_email(self.email_from) or False,
            'meta_norm_phone': normalize_phone(self.phone, uae_context=self._meta_uae_context()) or False,
        }

    @api.model_create_multi
    def create(self, vals_list):
        conversation_ids = [vals.pop('meta_conversation_id', False) for vals in vals_list]
        companies = {}
        for vals in vals_list:
            if 'meta_norm_email' not in vals and vals.get('email_from'):
                vals['meta_norm_email'] = normalize_email(vals['email_from']) or False
            if 'meta_norm_phone' not in vals and vals.get('phone'):
                company_id = vals.get('company_id') or self.env.company.id
                if company_id not in companies:
                    companies[company_id] = self.env['res.company'].browse(company_id)
                country = companies[company_id].country_id
                uae = bool(country and country.code == 'AE')
                vals['meta_norm_phone'] = normalize_phone(vals['phone'], uae_context=uae) or False
        with self.env.cr.savepoint():
            leads = super().create(vals_list)
            for lead, conversation_id in zip(leads, conversation_ids):
                if conversation_id:
                    self.env['meta.conversation'].browse(conversation_id)._link_to_lead(lead)
        return leads

    def write(self, vals):
        if 'meta_conversation_id' in vals:
            if len(self) != 1:
                raise UserError(_('Link one CRM lead at a time.'))
            conversation_id = vals['meta_conversation_id'] or False
            other_vals = {key: value for key, value in vals.items()
                          if key != 'meta_conversation_id'}
            with self.env.cr.savepoint():
                if conversation_id:
                    self.env['meta.conversation'].browse(conversation_id)._link_to_lead(self)
                elif self.meta_conversation_id:
                    raise UserError(_('Clearing a linked Meta conversation is not supported.'))
                return self.write(other_vals) if other_vals else True
        result = super().write(vals)
        if self.env.context.get('meta_norm_sync'):
            return result
        if {'email_from', 'phone', 'company_id'} & set(vals):
            for rec in self:
                rec.with_context(meta_norm_sync=True).write(rec._meta_norm_vals())
        return result

    def _meta_set_conversation_link(self, conversation):
        """Set the inverse only after meta.conversation validated both sides."""
        self.ensure_one()
        return super(CrmLead, self).write({'meta_conversation_id': conversation.id})
