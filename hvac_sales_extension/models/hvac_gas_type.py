from odoo import fields, models


class HvacGasType(models.Model):
    """Master data model for HVAC refrigerant gas types (e.g., R-410A, R-32, R-22)."""

    _name = 'hvac.gas.type'
    _description = 'HVAC Gas Type'
    _order = 'name'

    name = fields.Char(
        string='Gas Type',
        required=True,
        translate=True,
    )
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Uncheck to archive this gas type.',
    )
