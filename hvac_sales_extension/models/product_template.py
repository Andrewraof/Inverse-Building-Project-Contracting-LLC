from odoo import api, fields, models

# ── HVAC conversion constants (per 1 Ton of Refrigeration) ──
_BTU_PER_TON = 12000.0
_CFM_PER_TON = 400.0
_KW_PER_TON = 3.517
_HP_PER_TON = 4.7


class ProductTemplate(models.Model):
    """Extend product.template with HVAC cooling-load specifications."""

    _inherit = 'product.template'

    x_hvac_btu = fields.Float(
        string='BTU',
        digits=(16, 2),
        help='British Thermal Units rating of this HVAC product.',
    )
    x_hvac_cfm = fields.Float(
        string='CFM',
        digits=(16, 2),
        help='Cubic Feet per Minute airflow capacity.',
    )
    x_hvac_kw = fields.Float(
        string='kW',
        digits=(16, 3),
        help='Electrical power consumption in kilowatts.',
    )
    x_hvac_hp = fields.Float(
        string='HP',
        digits=(16, 2),
        help='Horsepower rating of compressor / motor.',
    )
    x_hvac_auto_calc = fields.Boolean(
        string='Auto-Calculate from BTU',
        default=True,
        help='When enabled, CFM / kW / HP are derived automatically from the BTU value.',
    )

    @api.onchange('x_hvac_btu', 'x_hvac_auto_calc')
    def _onchange_hvac_btu(self):
        """Recompute derived HVAC fields when BTU or the auto-calc flag changes.

        Conversion ratios (per 1 Ton of Refrigeration):
            1 Ton = 12 000 BTU
            1 Ton = 400 CFM
            1 Ton = 3.517 kW
            1 Ton = 4.7 HP
        """
        for record in self:
            if record.x_hvac_auto_calc and record.x_hvac_btu:
                tons = record.x_hvac_btu / _BTU_PER_TON
                record.x_hvac_cfm = round(tons * _CFM_PER_TON, 2)
                record.x_hvac_kw = round(tons * _KW_PER_TON, 3)
                record.x_hvac_hp = round(tons * _HP_PER_TON, 2)

    @api.model_create_multi
    def create(self, vals_list):
        """Ensure derived HVAC values are computed on create when auto-calc is on."""
        for vals in vals_list:
            self._apply_auto_calc(vals)
        return super().create(vals_list)

    def write(self, vals):
        """Ensure derived HVAC values are recomputed on write when auto-calc is on."""
        # If the caller is explicitly changing auto_calc or BTU, recalculate
        if 'x_hvac_btu' in vals or 'x_hvac_auto_calc' in vals:
            for record in self:
                merged = {
                    'x_hvac_btu': vals.get('x_hvac_btu', record.x_hvac_btu),
                    'x_hvac_auto_calc': vals.get('x_hvac_auto_calc', record.x_hvac_auto_calc),
                }
                self._apply_auto_calc(merged)
                vals.update({
                    'x_hvac_cfm': merged.get('x_hvac_cfm', vals.get('x_hvac_cfm')),
                    'x_hvac_kw': merged.get('x_hvac_kw', vals.get('x_hvac_kw')),
                    'x_hvac_hp': merged.get('x_hvac_hp', vals.get('x_hvac_hp')),
                })
        return super().write(vals)

    @api.model
    def _apply_auto_calc(self, vals):
        """Apply HVAC auto-calculation to a vals dict in-place.

        Only mutates *vals* when auto_calc is True and BTU is provided.
        """
        auto_calc = vals.get('x_hvac_auto_calc', True)
        btu = vals.get('x_hvac_btu', 0.0)
        if auto_calc and btu:
            tons = btu / _BTU_PER_TON
            vals['x_hvac_cfm'] = round(tons * _CFM_PER_TON, 2)
            vals['x_hvac_kw'] = round(tons * _KW_PER_TON, 3)
            vals['x_hvac_hp'] = round(tons * _HP_PER_TON, 2)
