# Part of the Inverse Building Project Contracting LLC — Dubai HVAC Sales
# Extension (upstream-tracked fork; see UPSTREAM.md).
# See LICENSE file for full copyright and licensing details.

from odoo import models


def _fetch_quotation_values(env, docids, is_ar=False):
    """Shared data-fetching logic for both EN and AR quotation report variants."""
    docs = env['sale.order'].browse(docids)
    clauses = env['hvac.contract.clause'].search(
        [('is_parent', '=', True), ('active', '=', True)],
        order='sequence, id',
    )
    return {'docs': docs, 'clauses': clauses, 'is_ar': is_ar}


class ReportHvacQuotationEn(models.AbstractModel):
    _name = 'report.hvac_sales_extension.report_hvac_quotation_en'
    _description = 'HVAC Quotation Report Values (EN)'

    def _get_report_values(self, docids, data=None):
        return _fetch_quotation_values(self.env, docids, is_ar=False)


class ReportHvacQuotationAr(models.AbstractModel):
    _name = 'report.hvac_sales_extension.report_hvac_quotation_ar'
    _description = 'HVAC Quotation Report Values (AR)'

    def _get_report_values(self, docids, data=None):
        return _fetch_quotation_values(self.env, docids, is_ar=True)
