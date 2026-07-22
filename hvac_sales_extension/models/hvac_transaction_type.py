from odoo import fields, models


class HvacTransactionType(models.Model):
    """Master data model for HVAC transaction types (e.g., New Installation, Replacement)."""

    _name = 'hvac.transaction.type'
    _description = 'HVAC Transaction Type'
    _order = 'name'

    name = fields.Char(
        string='Transaction Type',
        required=True,
        translate=True,
    )
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Uncheck to archive this transaction type.',
    )
