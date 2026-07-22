# Part of the Inverse Building Project Contracting LLC — Dubai HVAC Sales
# Extension (upstream-tracked fork; see UPSTREAM.md).
# See LICENSE file for full copyright and licensing details.

from odoo import _, fields, models
from odoo.exceptions import UserError


class HvacPrintContractWizard(models.TransientModel):
    """Language-selection wizard launched when the user clicks 'Print Contract'.

    Opens as a small dialog that asks for the desired language, then fires
    the corresponding report action (English or Arabic template).
    """

    _name = 'hvac.print.contract.wizard'
    _description = 'Print HVAC Contract — Language Selection'

    order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Sale Order',
        required=True,
        readonly=True,
        ondelete='cascade',
    )
    language = fields.Selection(
        selection=[
            ('en', 'English'),
            ('ar', 'العربية — Arabic'),
        ],
        string='Contract Language',
        default='en',
        required=True,
    )

    def action_print(self):
        """Fire the appropriate PDF report based on the selected language."""
        self.ensure_one()
        order = self.order_id
        if order.state not in ('contract', 'sale'):
            raise UserError(
                _("The contract can only be printed once it is in Contract or Sale state.")
            )
        if self.language == 'ar':
            report_ref = 'hvac_sales_extension.action_report_hvac_contract_ar'
        else:
            report_ref = 'hvac_sales_extension.action_report_hvac_contract_en'
        return self.env.ref(report_ref).report_action(order)
