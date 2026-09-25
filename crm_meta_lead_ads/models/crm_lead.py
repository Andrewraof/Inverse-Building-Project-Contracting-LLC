from odoo import api, fields, models, _
from odoo.exceptions import UserError

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
