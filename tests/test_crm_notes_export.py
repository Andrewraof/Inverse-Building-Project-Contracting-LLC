import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hvac_sales_extension" / "models" / "crm_lead.py"


def _load_crm_lead_module(html_converter):
    odoo = types.ModuleType("odoo")
    odoo.api = types.SimpleNamespace(depends=lambda *args: lambda func: func)
    odoo.fields = types.SimpleNamespace(Text=lambda **kwargs: kwargs)
    odoo.models = types.SimpleNamespace(Model=object)

    tools = types.ModuleType("odoo.tools")
    tools.html2plaintext = html_converter

    previous_odoo = sys.modules.get("odoo")
    previous_tools = sys.modules.get("odoo.tools")
    sys.modules["odoo"] = odoo
    sys.modules["odoo.tools"] = tools
    try:
        spec = importlib.util.spec_from_file_location("crm_lead_under_test", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_odoo is None:
            sys.modules.pop("odoo", None)
        else:
            sys.modules["odoo"] = previous_odoo
        if previous_tools is None:
            sys.modules.pop("odoo.tools", None)
        else:
            sys.modules["odoo.tools"] = previous_tools


class CrmNotesExportTests(unittest.TestCase):
    def test_readable_notes_use_odoo_html_converter_and_trim_result(self):
        calls = []

        def converter(value):
            calls.append(value)
            return "  أعمال التشطيبات\nHe sent a WhatsApp message\n\n"

        module = _load_crm_lead_module(converter)
        html = (
            '<div data-last-history-steps=",123,456">أعمال التشطيبات</div>'
            "<div>He sent a WhatsApp message&nbsp;</div>"
        )

        self.assertEqual(
            module.notes_to_plaintext(html),
            "أعمال التشطيبات\nHe sent a WhatsApp message",
        )
        self.assertEqual(
            calls,
            [
                '<div data-last-history-steps=",123,456">أعمال التشطيبات</div><br>'
                "<div>He sent a WhatsApp message&nbsp;</div><br>"
            ],
        )

    def test_readable_notes_return_empty_text_for_missing_notes(self):
        module = _load_crm_lead_module(lambda value: "unexpected")

        self.assertEqual(module.notes_to_plaintext(None), "")
        self.assertEqual(module.notes_to_plaintext(False), "")
        self.assertEqual(module.notes_to_plaintext(""), "")


if __name__ == "__main__":
    unittest.main()
