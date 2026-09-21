from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class MetaFormMapping(models.Model):
    _name = 'meta.form.mapping'
    _description = 'Meta Form Field Mapping'
    _order = 'sequence,id'

    sequence = fields.Integer(default=10)
    form_id = fields.Many2one('meta.form', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='form_id.company_id', store=True, index=True)
    meta_field_name = fields.Char(required=True)
    odoo_field_name = fields.Char(required=True, help='Technical field name on crm.lead')
    join_separator = fields.Char(default=', ')
    active = fields.Boolean(default=True)

    _unique_mapping = models.Constraint('UNIQUE(form_id, meta_field_name)', 'Each Meta field can be mapped only once per form.')

    @api.constrains('odoo_field_name')
    def _check_odoo_field(self):
        lead_fields = self.env['crm.lead']._fields
        for rec in self:
            if rec.odoo_field_name not in lead_fields:
                raise ValidationError(_('Unknown crm.lead field: %s') % rec.odoo_field_name)
