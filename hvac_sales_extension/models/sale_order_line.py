from odoo import api, fields, models


class SaleOrderLine(models.Model):
    """Extend sale.order.line with per-line HVAC cooling-load values."""

    _inherit = 'sale.order.line'

    x_line_btu = fields.Float(
        string='BTU',
        digits=(16, 2),
        help='BTU value for this line item. Copied from the product on selection; editable.',
    )
    x_line_cfm = fields.Float(
        string='CFM',
        digits=(16, 2),
        help='CFM value for this line item.',
    )
    x_line_kw = fields.Float(
        string='kW',
        digits=(16, 3),
        help='kW value for this line item.',
    )
    x_line_hp = fields.Float(
        string='HP',
        digits=(16, 2),
        help='HP value for this line item.',
    )

    @api.onchange('product_id')
    def _onchange_product_hvac(self):
        """Propagate HVAC specs from the selected product to the order line.

        Values are copied once on product selection; the user may freely
        override them afterward without writing back to the product master.
        """
        for line in self:
            if line.product_id and line.product_id.product_tmpl_id:
                tmpl = line.product_id.product_tmpl_id
                line.x_line_btu = tmpl.x_hvac_btu
                line.x_line_cfm = tmpl.x_hvac_cfm
                line.x_line_kw = tmpl.x_hvac_kw
                line.x_line_hp = tmpl.x_hvac_hp
