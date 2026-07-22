from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    """Extend sale.order with HVAC header fields, project-level aggregates,
    and a 'contract' workflow stage with PDF report generation."""

    _inherit = 'sale.order'

    # ══════════════════════════════════════════════════════════════
    #  CONTRACT WORKFLOW — state extension
    # ══════════════════════════════════════════════════════════════

    state = fields.Selection(
        selection_add=[
            ('contract', 'Contract'),
            ('sale',),  # places 'contract' before 'sale'
        ],
        ondelete={'contract': 'set default'},
    )

    x_contract_ref = fields.Char(
        string='Contract Reference',
        readonly=True,
        copy=False,
        help='Auto-generated reference for the installation contract.',
    )
    x_contract_date = fields.Date(
        string='Contract Date',
        readonly=True,
        copy=False,
        help='Date the contract was created.',
    )

    # ══════════════════════════════════════════════════════════════
    #  HVAC HEADER / CLASSIFICATION FIELDS
    # ══════════════════════════════════════════════════════════════

    x_transaction_type = fields.Many2one(
        comodel_name='hvac.transaction.type',
        string='Transaction Type',
        help='Type of HVAC transaction (e.g., New Installation, Replacement).',
    )
    x_machine_brand_id = fields.Many2one(
        comodel_name='hvac.brand',
        string='Machine Brand',
        help='HVAC equipment brand for this project.',
    )
    x_gas_type = fields.Many2one(
        comodel_name='hvac.gas.type',
        string='Gas Type',
        help='Refrigerant gas type used in this project.',
    )
    x_paci_number = fields.Char(
        string='PACI Number',
        help='Public Authority for Civil Information number.',
    )

    # ══════════════════════════════════════════════════════════════
    #  PRINT OPTIONS — which HVAC unit columns appear on the PDF
    # ══════════════════════════════════════════════════════════════

    x_show_btu = fields.Boolean(
        string='Show BTU in Print',
        default=True,
        help='Include the BTU column and total on the printed quotation/contract.',
    )
    x_show_cfm = fields.Boolean(
        string='Show CFM in Print',
        default=True,
        help='Include the CFM column and total on the printed quotation/contract.',
    )
    x_show_kw = fields.Boolean(
        string='Show kW in Print',
        default=True,
        help='Include the kW column and total on the printed quotation/contract.',
    )
    x_show_hp = fields.Boolean(
        string='Show HP in Print',
        default=True,
        help='Include the HP column and total on the printed quotation/contract.',
    )

    # ══════════════════════════════════════════════════════════════
    #  PROJECT-LEVEL AGGREGATES
    # ══════════════════════════════════════════════════════════════

    x_total_project_btu = fields.Float(
        string='Total BTU',
        compute='_compute_hvac_totals',
        store=True,
        digits=(16, 2),
        help='Sum of BTU across all order lines.',
    )
    x_total_project_cfm = fields.Float(
        string='Total CFM',
        compute='_compute_hvac_totals',
        store=True,
        digits=(16, 2),
        help='Sum of CFM across all order lines.',
    )
    x_total_project_kw = fields.Float(
        string='Total kW',
        compute='_compute_hvac_totals',
        store=True,
        digits=(16, 3),
        help='Sum of kW across all order lines.',
    )
    x_total_project_hp = fields.Float(
        string='Total HP',
        compute='_compute_hvac_totals',
        store=True,
        digits=(16, 2),
        help='Sum of HP across all order lines.',
    )

    @api.depends(
        'order_line.x_line_btu',
        'order_line.x_line_cfm',
        'order_line.x_line_kw',
        'order_line.x_line_hp',
    )
    def _compute_hvac_totals(self):
        """Aggregate HVAC values from non-section / non-note order lines.

        Section and note lines (display_type set) are excluded from the
        summation so that floor-grouping headers do not pollute totals.
        """
        for order in self:
            product_lines = order.order_line.filtered(lambda l: not l.display_type)
            order.x_total_project_btu = sum(product_lines.mapped('x_line_btu'))
            order.x_total_project_cfm = sum(product_lines.mapped('x_line_cfm'))
            order.x_total_project_kw = sum(product_lines.mapped('x_line_kw'))
            order.x_total_project_hp = sum(product_lines.mapped('x_line_hp'))

    # ══════════════════════════════════════════════════════════════
    #  CONTRACT WORKFLOW ACTIONS
    # ══════════════════════════════════════════════════════════════

    def action_create_contract(self):
        """Move the order from draft/sent to the 'contract' stage.

        Generates a unique contract reference and records the contract date.

        :raise UserError: if the order is not in draft or sent state.
        """
        for order in self:
            if order.state not in ('draft', 'sent'):
                raise UserError(
                    _("Only draft or sent quotations can be converted to contracts.")
                )
            vals = {
                'state': 'contract',
                'x_contract_date': fields.Date.context_today(self),
            }
            if not order.x_contract_ref:
                vals['x_contract_ref'] = (
                    self.env['ir.sequence'].next_by_code('hvac.contract') or 'New'
                )
            order.write(vals)
        return True

    def action_print_contract(self):
        """Open the language-selection wizard before printing the contract PDF."""
        self.ensure_one()
        if self.state not in ('contract', 'sale'):
            from odoo.exceptions import UserError
            raise UserError(
                _("The contract can only be printed once it is in "
                  "Contract or Sale state.")
            )
        return {
            'type': 'ir.actions.act_window',
            'name': _('Print Contract'),
            'res_model': 'hvac.print.contract.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_order_id': self.id},
        }

    def action_print_quotation(self):
        """Open the language-selection wizard before printing the quotation PDF."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Print Quotation'),
            'res_model': 'hvac.print.quotation.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_order_id': self.id},
        }

    # ══════════════════════════════════════════════════════════════
    #  OVERRIDES — allow 'contract' in confirmation / cancel / draft
    # ══════════════════════════════════════════════════════════════

    def _confirmation_error_message(self):
        """Extend to allow confirmation from 'contract' state.

        The base Odoo 19 method (sale_order.py ~L1186) rejects any state
        not in {'draft', 'sent'}.  We add 'contract' to the allowed set.
        """
        self.ensure_one()
        if self.state == 'contract':
            # Validate product lines the same way the base does
            if any(
                not line.display_type
                and not line.is_downpayment
                and not line.product_id
                for line in self.order_line
            ):
                return _(
                    "Some order lines are missing a product, "
                    "you need to correct them before going further."
                )
            return False
        return super()._confirmation_error_message()

    def action_cancel(self):
        """Allow cancellation from the contract state."""
        return super().action_cancel()

    def action_draft(self):
        """Allow resetting contract-state orders back to draft."""
        orders = self.filtered(lambda s: s.state in ('cancel', 'sent', 'contract'))
        return orders.write({
            'state': 'draft',
            'signature': False,
            'signed_by': False,
            'signed_on': False,
        })

    @api.depends('state')
    def _compute_type_name(self):
        """Show 'Contract' as the document type when in contract state."""
        super()._compute_type_name()
        for record in self:
            if record.state == 'contract':
                record.type_name = _("Contract")
