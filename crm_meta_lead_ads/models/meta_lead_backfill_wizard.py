from odoo import fields, models, _
from odoo.exceptions import UserError


class MetaLeadBackfillWizard(models.TransientModel):
    _name = 'meta.lead.backfill.wizard'
    _description = 'Meta Leads Historical Backfill'

    account_id = fields.Many2one('meta.account', required=True)
    page_ids = fields.Many2many('meta.page', string='Pages',
                                help='Empty means every page of the account.')
    date_from = fields.Datetime(help='Only leads created after this time (Meta time_created).')
    date_to = fields.Datetime(help='Only leads created before this time.')
    max_pages = fields.Integer(default=25, required=True,
                               help='Safety cap on Graph API pages for this run.')
    max_records = fields.Integer(default=1000, required=True,
                                 help='Safety cap on fetched leads for this run.')

    def action_create_run(self):
        self.ensure_one()
        if self.date_from and self.date_to and self.date_from >= self.date_to:
            raise UserError(_('The start date must be before the end date.'))
        Run = self.env['meta.sync.run']
        existing = Run.search([
            ('account_id', '=', self.account_id.id),
            ('state', 'in', ('draft', 'running')),
        ], limit=1)
        if existing:
            raise UserError(_(
                'Another sync run (%s) is already active for this Meta account. '
                'Wait for it to finish or cancel it first.') % existing.name)
        run = Run.create({
            'account_id': self.account_id.id,
            'company_id': self.account_id.company_id.id,
            'run_type': 'leads',
            'page_ids': [(6, 0, self.page_ids.ids)],
            'date_from': self.date_from,
            'date_to': self.date_to,
            'max_pages': self.max_pages,
            'max_records': self.max_records,
        })
        run.action_start()
        return run._form_action()
