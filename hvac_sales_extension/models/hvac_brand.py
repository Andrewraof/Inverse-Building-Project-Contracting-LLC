from odoo import fields, models


class HvacBrand(models.Model):
    """Master data model for HVAC machine brands (e.g., Carrier, Daikin, Trane)."""

    _name = 'hvac.brand'
    _description = 'HVAC Brand'
    _order = 'name'

    name = fields.Char(
        string='Brand Name',
        required=True,
        translate=True,
    )
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Uncheck to archive this brand.',
    )
