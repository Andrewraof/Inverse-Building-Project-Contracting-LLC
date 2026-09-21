import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    def test_server_script_has_required_safety_gates(self):
        script = (ROOT / "deploy" / "deploy-inverse-odoo").read_text(encoding="utf-8")
        self.assertIn("set -Eeuo pipefail", script)
        self.assertIn("flock", script)
        self.assertIn("validate_addon.py", script)
        self.assertIn("pg_dump", script)
        self.assertIn("systemctl is-active", script)
        self.assertIn("rollback", script)

    def test_workflow_uses_pinned_host_key_and_private_key_secret(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("secrets.SSH_PRIVATE_KEY", workflow)
        self.assertIn("178.104.53.152 ssh-ed25519", workflow)
        self.assertIn("sudo /usr/local/sbin/deploy-inverse-odoo", workflow)
        self.assertIn("concurrency:", workflow)


if __name__ == "__main__":
    unittest.main()
