from odoo import api, fields, models


class MetaAdInsight(models.Model):
    """Daily ad performance rows (spend/impressions/clicks/leads).

    Structure only: rows are populated by a future sync job once Meta
    grants the ads_read permission (Ads Insights API). Until then the
    list stays empty and the CPL report shows 'permission required'.
    """

    _name = 'meta.ad.insight'
    _description = 'Meta Ad Daily Insights'
    _order = 'date desc, id desc'

    date = fields.Date(required=True, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', readonly=True)
    meta_campaign_id = fields.Char(index=True)
    meta_campaign_name = fields.Char()
    meta_adset_id = fields.Char(index=True)
    meta_adset_name = fields.Char()
    meta_ad_id = fields.Char(index=True)
    meta_ad_name = fields.Char()
    spend = fields.Monetary(currency_field='currency_id')
    impressions = fields.Integer()
    clicks = fields.Integer()
    leads_count = fields.Integer(help='Meta leads attributed to this row; filled by the sync job.')
    cpl = fields.Monetary(currency_field='currency_id', compute='_compute_cpl',
                          help='Cost per lead: spend / leads; zero when no leads.')

    _unique_ad_day = models.Constraint(
        'UNIQUE(company_id, date, meta_campaign_id, meta_adset_id, meta_ad_id)',
        'This ad performance row already exists for that day.')

    @api.depends('spend', 'leads_count')
    def _compute_cpl(self):
        for rec in self:
            rec.cpl = rec.spend / rec.leads_count if rec.leads_count else 0.0
