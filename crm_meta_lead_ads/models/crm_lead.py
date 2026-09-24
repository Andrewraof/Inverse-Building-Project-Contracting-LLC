from odoo import fields, models


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

    _unique_meta_lead = models.Constraint('UNIQUE(meta_lead_id)', 'This Meta lead was already imported.')
