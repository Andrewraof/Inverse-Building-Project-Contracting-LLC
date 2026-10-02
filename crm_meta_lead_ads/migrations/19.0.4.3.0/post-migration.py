from odoo import api, fields, SUPERUSER_ID, Command


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    params = env['ir.config_parameter'].sudo()
    key = 'crm_meta_lead_ads.contact_review_from'
    if not params.get_param(key):
        params.set_param(key, fields.Datetime.now())
    # Grant review to the already-designated lead creators, not to all sales.
    creator_group = env.ref('inverse_crm_lead_control.group_lead_creator', raise_if_not_found=False)
    users = env['res.users'].search([
        ('active', '=', True), ('share', '=', False),
        ('group_ids', 'in', creator_group.ids),
    ]) if creator_group else env['res.users'].browse()
    if not users:
        users = env['res.users'].search([
            ('active', '=', True), ('share', '=', False),
            ('login', 'in', ['hafez.kamal', 'hafez.kamal@inversegroup.ae', 'michael@inversegroup.ae']),
        ])
    if users:
        users.write({'group_ids': [Command.link(env.ref('crm_meta_lead_ads.group_meta_contact_reviewer').id)]})
