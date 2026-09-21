from odoo.tests.common import TransactionCase


class TestCrmNotesExport(TransactionCase):
    def test_export_returns_readable_notes_without_changing_original_html(self):
        original_html = (
            '<div data-oe-version="2.0" data-last-history-steps=",123,456">'
            "أعمال التشطيبات</div>"
            "<div>He sent a WhatsApp message&nbsp;</div>"
        )
        lead = self.env["crm.lead"].create(
            {"name": "Readable notes export", "description": original_html}
        )

        exported = lead.export_data(["description", "x_notes_readable"])["datas"][0]

        self.assertEqual(exported[0], original_html)
        self.assertEqual(
            exported[1],
            "أعمال التشطيبات\nHe sent a WhatsApp message",
        )
        self.assertEqual(lead.description, original_html)

    def test_export_returns_empty_text_when_notes_are_missing(self):
        lead = self.env["crm.lead"].create({"name": "Empty notes export"})

        exported = lead.export_data(["x_notes_readable"])["datas"][0]

        self.assertEqual(exported[0], "")
