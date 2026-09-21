from odoo import fields, models


class MetaLeadLog(models.Model):
    _name = 'meta.lead.log'
    _description = 'Meta Lead Audit Log'
    _order = 'create_date desc'

    company_id = fields.Many2one('res.company', required=True, index=True)
    queue_id = fields.Many2one('meta.lead.queue', ondelete='set null', index=True)
    meta_lead_id = fields.Char(index=True)
    level = fields.Selection([('info','Info'),('warning','Warning'),('error','Error')], default='info', required=True, index=True)
    action = fields.Char(required=True)
    message = fields.Text()
    payload_json = fields.Json(readonly=True)

    def write(self, vals):
        # immutable business audit rows
        if not self.env.context.get('meta_log_internal_write'):
            return super(MetaLeadLog, self).write(vals)
        return super().write(vals)
