import re

from odoo import api, fields, models
from odoo.tools import html2plaintext


_BLOCK_END_RE = re.compile(r"</(?:div|p|li|h[1-6])\s*>", re.IGNORECASE)


def notes_to_plaintext(value):
    """Return CRM notes as readable text for spreadsheet exports."""
    if not value:
        return ""
    html_with_line_breaks = _BLOCK_END_RE.sub(r"\g<0><br>", value)
    return html2plaintext(html_with_line_breaks).strip()


class CrmLead(models.Model):
    _inherit = "crm.lead"

    x_notes_readable = fields.Text(
        string="Notes (Readable)",
        compute="_compute_x_notes_readable",
        help="Plain-text version of Notes for readable CSV and Excel exports.",
    )

    @api.depends("description")
    def _compute_x_notes_readable(self):
        for lead in self:
            lead.x_notes_readable = notes_to_plaintext(lead.description)
