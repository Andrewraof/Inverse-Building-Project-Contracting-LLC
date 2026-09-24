from odoo import fields, models


class MetaLeadIdentity(models.Model):
    """Links every Meta lead ID ever received to the CRM lead it ended
    up on. A CRM lead matched by email/phone can accumulate several Meta
    lead IDs over time; each one gets its own row here instead of being
    crammed into a text field, so no Meta lead ID is ever lost."""
    _name = 'meta.lead.identity'
    _description = 'Meta Lead Identity Link'
    _order = 'id desc'

    company_id = fields.Many2one('res.company', required=True, index=True)
    crm_lead_id = fields.Many2one('crm.lead', required=True, ondelete='cascade', index=True)
    meta_lead_id = fields.Char(required=True, index=True)
    queue_id = fields.Many2one('meta.lead.queue', ondelete='set null', index=True)
    match_type = fields.Selection([
        ('created', 'Created'),
        ('matched_email', 'Matched by Email'),
        ('matched_phone', 'Matched by Phone'),
        ('duplicate_meta_id', 'Same Meta Lead ID'),
        ('backfill', 'Backfill'),
    ], required=True, index=True)

    _unique_identity = models.Constraint(
        'UNIQUE(meta_lead_id, company_id)',
        'This Meta lead ID is already linked for this company.')
