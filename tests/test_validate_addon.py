import tempfile
import unittest
from pathlib import Path

from deploy.validate_addon import ValidationError, validate_addon


class ValidateAddonTests(unittest.TestCase):
    def make_addon(self, root: Path) -> Path:
        addon = root / "hvac_sales_extension"
        addon.mkdir()
        (addon / "__manifest__.py").write_text(
            "{'name': 'HVAC', 'version': '19.0.1.0.0', 'depends': ['sale_management']}",
            encoding="utf-8",
        )
        (addon / "__init__.py").write_text("", encoding="utf-8")
        return addon

    def test_accepts_valid_addon(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon = self.make_addon(Path(tmp))
            (addon / "view.xml").write_text("<odoo><data/></odoo>", encoding="utf-8")

            result = validate_addon(addon)

            self.assertEqual(result["name"], "HVAC")
            self.assertEqual(result["version"], "19.0.1.0.0")

    def test_rejects_missing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon = Path(tmp) / "hvac_sales_extension"
            addon.mkdir()

            with self.assertRaisesRegex(ValidationError, "manifest"):
                validate_addon(addon)

    def test_rejects_invalid_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon = self.make_addon(Path(tmp))
            (addon / "broken.py").write_text("def broken(:\n", encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "Python"):
                validate_addon(addon)

    def test_rejects_invalid_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon = self.make_addon(Path(tmp))
            (addon / "broken.xml").write_text("<odoo>", encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "XML"):
                validate_addon(addon)


if __name__ == "__main__":
    unittest.main()
