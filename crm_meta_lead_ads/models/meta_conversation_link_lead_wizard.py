from odoo import fields, models


class MetaConversationLinkLeadWizard(models.TransientModel):
    _name = 'meta.conversation.link.lead.wizard'
    _description = 'Link a Meta Inbox conversation to an existing CRM lead'

    conversation_id = fields.Many2one('meta.conversation', required=True, readonly=True)
    company_id = fields.Many2one('res.company', related='conversation_id.company_id', readonly=True)
    lead_id = fields.Many2one(
        'crm.lead', required=True, string='CRM Lead',
        domain="[('company_id', 'in', [company_id, False]), ('meta_conversation_id', '=', False)]",
        help='Only leads without a linked Meta conversation are offered.')

    def action_link(self):
        self.ensure_one()
        self.conversation_id._link_to_lead(self.lead_id)
        return {'type': 'ir.actions.act_window_close'}
