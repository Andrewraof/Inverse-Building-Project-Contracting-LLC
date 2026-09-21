import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    def test_server_script_deploys_hvac_and_meta_addons(self):
        script = (ROOT / "deploy" / "deploy-inverse-odoo").read_text(encoding="utf-8")
        self.assertIn(
            'readonly MODULE_NAMES=("hvac_sales_extension" "crm_meta_lead_ads")',
            script,
        )
        self.assertIn('readonly AUTO_INSTALL_MODULES=("crm_meta_lead_ads")', script)
        self.assertIn('for module_name in "${MODULE_NAMES[@]}"; do', script)
        self.assertIn('operation_mode="install"', script)
        self.assertIn('-i "$module_name"', script)

        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("deploy/validate_addon.py crm_meta_lead_ads", workflow)
        self.assertIn("Bootstrap deployer and deploy exact commit", workflow)
        self.assertIn("Deploy exact commit with refreshed deployer", workflow)

    def test_server_script_has_required_safety_gates(self):
        script = (ROOT / "deploy" / "deploy-inverse-odoo").read_text(encoding="utf-8")
        self.assertIn("set -Eeuo pipefail", script)
        self.assertIn("flock", script)
        self.assertIn("validate_addon.py", script)
        self.assertIn("pg_dump", script)
        self.assertNotIn('pg_dump -Fc -f', script)
        self.assertIn('pg_dump -Fc "$database" >', script)
        self.assertIn("systemctl is-active", script)
        self.assertIn("rollback", script)

    def test_server_script_only_updates_the_production_database(self):
        script = (ROOT / "deploy" / "deploy-inverse-odoo").read_text(encoding="utf-8")
        self.assertIn('readonly PRODUCTION_DATABASES=("inverse_elite")', script)
        self.assertIn('for database in "${PRODUCTION_DATABASES[@]}"; do', script)
        self.assertNotIn("SELECT datname FROM pg_database", script)

    def test_workflow_uses_pinned_host_key_and_private_key_secret(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("secrets.SSH_PRIVATE_KEY", workflow)
        self.assertIn("178.104.53.152 ssh-ed25519", workflow)
        self.assertIn("sudo /usr/local/sbin/deploy-inverse-odoo", workflow)
        self.assertIn("concurrency:", workflow)


if __name__ == "__main__":
    unittest.main()
