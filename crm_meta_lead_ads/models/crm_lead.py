from odoo import api, fields, models

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
    meta_identity_ids = fields.One2many('meta.lead.identity', 'crm_lead_id', readonly=True)

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
        return super().create(vals_list)

    def write(self, vals):
        result = super().write(vals)
        if self.env.context.get('meta_norm_sync'):
            return result
        if {'email_from', 'phone', 'company_id'} & set(vals):
            for rec in self:
                rec.with_context(meta_norm_sync=True).write(rec._meta_norm_vals())
        return result
