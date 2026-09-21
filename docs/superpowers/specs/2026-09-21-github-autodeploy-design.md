# GitHub auto-deployment design

## Goal

Deploy `hvac_sales_extension` from the repository's `main` branch to the
production Odoo 19 server after validation, with serialized deployments,
database and file backups, and file rollback when deployment fails.

## Production topology

- Host: `178.104.53.152`
- SSH principal: unprivileged `deploy` user using `SSH_PRIVATE_KEY`
- Odoo: `/opt/odoo/odoo19/odoo-bin`, managed by `odoo19.service`
- Service account: `odoo:odoo`
- Target addon: `/opt/odoo/custom-addons/boq-budget/hvac_sales_extension`

## Security boundary

GitHub Actions cannot run arbitrary root commands. The `deploy` user may invoke
only `/usr/local/sbin/deploy-inverse-odoo` through passwordless sudo. The script
accepts only a full 40-character hexadecimal commit SHA and always fetches from
the fixed public repository. The server host key is pinned in the workflow.

## Deployment flow

1. GitHub validates Python, XML, and manifest syntax and runs unit tests.
2. The workflow connects over SSH and passes the exact triggering commit SHA.
3. The root-owned server script locks deployment, fetches that commit, and
   repeats addon validation.
4. It backs up the existing addon and every database where the module is
   installed.
5. It synchronizes only `hvac_sales_extension`, preserving all other addons.
6. It stops Odoo, upgrades the module in each installed database, starts Odoo,
   and confirms the service is active.
7. On failure, it restores addon files, restarts Odoo, and retains database
   dumps for manual recovery.

## Operational notes

Deployments cause a short maintenance window because this host runs one Odoo
service. GitHub concurrency prevents overlapping production runs. Database
restoration is intentionally not automatic because restoring a live production
database is a separate destructive recovery decision.
