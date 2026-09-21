import json
from odoo import fields, models, _


class MetaForm(models.Model):
    _name = 'meta.form'
    _description = 'Meta Lead Form'
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    page_id = fields.Many2one('meta.page', required=True, ondelete='cascade', index=True)
    meta_form_id = fields.Char(required=True, index=True)
    status = fields.Char(readonly=True)
    questions_json = fields.Json(readonly=True)
    mapping_ids = fields.One2many('meta.form.mapping', 'form_id')
    sales_team_id = fields.Many2one('crm.team')
    user_id = fields.Many2one('res.users')
    lead_type = fields.Selection([('lead', 'Lead'), ('opportunity', 'Opportunity')], default='lead', required=True)
    last_sync_date = fields.Datetime()
    polling_enabled = fields.Boolean(default=True)

    _unique_form_company = models.Constraint('UNIQUE(meta_form_id, company_id)', 'This Meta form is already configured for this company.')

    def action_generate_default_mappings(self):
        Mapping = self.env['meta.form.mapping']
        defaults = {
            'full_name': 'contact_name', 'email': 'email_from', 'phone_number': 'phone',
            'city': 'city', 'company_name': 'partner_name',
        }
        for form in self:
            questions = form.questions_json or []
            names = set(defaults)
            for q in questions:
                key = q.get('key') or q.get('name') or q.get('id')
                if key:
                    names.add(str(key))
            for src in names:
                if not Mapping.search_count([('form_id', '=', form.id), ('meta_field_name', '=', src)]):
                    Mapping.create({'form_id': form.id, 'meta_field_name': src, 'odoo_field_name': defaults.get(src, 'description')})
        return True
