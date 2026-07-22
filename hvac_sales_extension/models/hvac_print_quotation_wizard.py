# Part of the Inverse Building Project Contracting LLC — Dubai HVAC Sales
# Extension (upstream-tracked fork; see UPSTREAM.md).
# See LICENSE file for full copyright and licensing details.

from odoo import _, fields, models


class HvacPrintQuotationWizard(models.TransientModel):
    """Language-selection wizard launched when the user clicks 'Print Quotation'.

    Mirrors HvacPrintContractWizard but targets the quotation (sale order)
    report templates which include the dynamic clause section.
    """

    _name = 'hvac.print.quotation.wizard'
    _description = 'Print HVAC Quotation — Language Selection'

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
        string='Language',
        default='en',
        required=True,
    )

    def action_print(self):
        """Fire the appropriate quotation PDF report based on the selected language."""
        self.ensure_one()
        if self.language == 'ar':
            report_ref = 'hvac_sales_extension.action_report_hvac_quotation_ar'
        else:
            report_ref = 'hvac_sales_extension.action_report_hvac_quotation_en'
        return self.env.ref(report_ref).report_action(self.order_id)
