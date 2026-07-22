# Part of the Inverse Building Project Contracting LLC — Dubai HVAC Sales
# Extension (upstream-tracked fork; see UPSTREAM.md).
# See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class HvacContractClause(models.Model):
    """Dynamic contract clause library.

    Two-level hierarchy:
    - Parent clause (is_parent=True):  represents a full Article heading with
      an optional HTML preamble and ordered sub-clauses.
    - Child clause  (is_parent=False): a single bullet/paragraph that belongs
      to a parent clause via parent_id.

    Article 1 (Equipment Schedule) is always rendered statically in the PDF.
    This model covers Article 2 onward.

    Numbering logic:
    - Parent clauses are numbered sequentially starting from 2 (Article 1
      is hardcoded), ordered by ``sequence``.
    - Child clauses inherit their parent's number plus their own position:
      e.g. 2.1, 2.2 … — computed at render time in QWeb to avoid stale data.
    """

    _name = 'hvac.contract.clause'
    _description = 'HVAC Contract Clause'
    _order = 'sequence, id'

    # ── Identifier ────────────────────────────────────────────────────────────
    clause_ref = fields.Char(
        string='Reference',
        required=True,
        help='Short internal name shown in dropdowns (e.g. "Scope of Work").',
    )

    # ── Article title — HTML so the author can apply bold / italic ────────────
    title = fields.Html(
        string='Title (EN)',
        sanitize=True,
        required=True,
        help='Article / clause heading in English.',
    )
    title_ar = fields.Html(
        string='Title (AR)',
        sanitize=True,
        help='Article / clause heading in Arabic.',
    )

    # ── Preamble — shown between the heading and the sub-clause list ──────────
    #   Visible only on parent clauses (is_parent=True).
    preamble = fields.Html(
        string='Preamble (EN)',
        sanitize=True,
        help='Introductory paragraph shown before sub-clauses (English). '
             'Only applicable to parent clauses.',
    )
    preamble_ar = fields.Html(
        string='Preamble (AR)',
        sanitize=True,
        help='Introductory paragraph in Arabic. Only for parent clauses.',
    )

    # ── Hierarchy ─────────────────────────────────────────────────────────────
    is_parent = fields.Boolean(
        string='Is Parent Clause',
        default=False,
        help='True → main Article heading.  False → sub-clause / bullet.',
    )
    parent_id = fields.Many2one(
        comodel_name='hvac.contract.clause',
        string='Parent Clause',
        domain=[('is_parent', '=', True)],
        ondelete='cascade',
        index=True,
        help='The parent article this sub-clause belongs to.',
    )
    child_ids = fields.One2many(
        comodel_name='hvac.contract.clause',
        inverse_name='parent_id',
        string='Sub-Clauses',
    )

    # ── Ordering ──────────────────────────────────────────────────────────────
    sequence = fields.Integer(
        string='Sequence',
        default=10,
        help='Controls display order. Lower numbers appear first.',
    )

    # ── Layout helper ─────────────────────────────────────────────────────────
    page_break_before = fields.Boolean(
        string='Page Break Before',
        default=False,
        help='Insert a PDF page break before this article.',
    )

    active = fields.Boolean(default=True)

    # ── Constraints ───────────────────────────────────────────────────────────

    @api.constrains('is_parent', 'parent_id')
    def _check_hierarchy(self):
        for rec in self:
            if not rec.is_parent and not rec.parent_id:
                from odoo.exceptions import ValidationError
                raise ValidationError(
                    "A sub-clause must have a parent clause selected."
                )
            if rec.is_parent and rec.parent_id:
                from odoo.exceptions import ValidationError
                raise ValidationError(
                    "A parent clause cannot itself have a parent."
                )

    # ── Display ───────────────────────────────────────────────────────────────

    def name_get(self):
        result = []
        for rec in self:
            result.append((rec.id, rec.clause_ref or f'Clause #{rec.id}'))
        return result
