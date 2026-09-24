import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Force the recovery polling cron to the intended 5-minute interval.

    The cron record was created under noupdate="1", so data-file updates
    never applied on existing databases (production stayed at 1 hour).
    Writing through the ORM bypasses the noupdate mechanism. Idempotent:
    re-running finds the target values already set and changes nothing.
    Logs old/new scheduling values only — no sensitive data.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref('crm_meta_lead_ads.ir_cron_poll_meta_leads', raise_if_not_found=False)
    if not cron:
        _logger.warning(
            'crm_meta_lead_ads %s: recovery polling cron not found; skipped', version)
        return
    if (cron.interval_number, cron.interval_type, cron.active) == (5, 'minutes', True):
        _logger.info(
            'crm_meta_lead_ads %s: recovery polling cron already at 5 minutes, active; nothing to do',
            version)
        return
    _logger.info(
        'crm_meta_lead_ads %s: recovery polling cron changed from every %s %s (active=%s) to every 5 minutes (active=True)',
        version, cron.interval_number, cron.interval_type, cron.active)
    cron.write({'interval_number': 5, 'interval_type': 'minutes', 'active': True})
