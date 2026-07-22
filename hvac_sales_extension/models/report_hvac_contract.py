# Part of the Inverse Building Project Contracting LLC — Dubai HVAC Sales
# Extension (upstream-tracked fork; see UPSTREAM.md).
# See LICENSE file for full copyright and licensing details.

from odoo import models


def _fetch_report_values(env, docids):
    """Shared data-fetching logic for both EN and AR report variants."""
    docs = env['sale.order'].browse(docids)
    clauses = env['hvac.contract.clause'].search(
        [('is_parent', '=', True), ('active', '=', True)],
        order='sequence, id',
    )
    return {'docs': docs, 'clauses': clauses}


class ReportHvacContractEn(models.AbstractModel):
    """Report values provider for the English HVAC contract template."""

    _name = 'report.hvac_sales_extension.report_hvac_contract_en'
    _description = 'HVAC Contract Report Values (EN)'

    def _get_report_values(self, docids, data=None):
        return _fetch_report_values(self.env, docids)


class ReportHvacContractAr(models.AbstractModel):
    """Report values provider for the Arabic HVAC contract template."""

    _name = 'report.hvac_sales_extension.report_hvac_contract_ar'
    _description = 'HVAC Contract Report Values (AR)'

    def _get_report_values(self, docids, data=None):
        return _fetch_report_values(self.env, docids)
