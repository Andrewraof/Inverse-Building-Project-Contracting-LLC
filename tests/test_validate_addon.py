import tempfile
import unittest
import os
from pathlib import Path

from deploy.validate_addon import ValidationError, refresh_deployer, validate_addon

ROOT = Path(__file__).resolve().parents[1]


class ValidateAddonTests(unittest.TestCase):
    def test_meta_security_uses_odoo19_sales_category(self):
        security = (ROOT / "crm_meta_lead_ads" / "security" / "meta_security.xml").read_text(
            encoding="utf-8"
        )
        self.assertIn('ref="base.module_category_sales"', security)
        self.assertNotIn('ref="base.module_category_sales_crm"', security)

    def test_refresh_deployer_atomically_installs_new_version_for_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "deploy-inverse-odoo.new"
            target = root / "deploy-inverse-odoo"
            source.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
            target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")

            changed = refresh_deployer(source, target, effective_uid=0)

            self.assertTrue(changed)
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_refresh_deployer_does_nothing_without_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "new"
            target = root / "installed"
            source.write_text("new", encoding="utf-8")
            target.write_text("old", encoding="utf-8")

            self.assertFalse(refresh_deployer(source, target, effective_uid=1000))
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

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
