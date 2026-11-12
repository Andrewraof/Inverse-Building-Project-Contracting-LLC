import logging
from collections import Counter
from pathlib import Path
from runpy import run_path

from odoo import SUPERUSER_ID, api
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)
LEAD_IDS = [969, 977, 991, 992, 993, 994, 995, 998]


def migrate(cr, version):
    if not version or cr.dbname != 'inverse_elite':
        return
    previous = Path(__file__).resolve().parents[1] / '19.0.4.3.3' / 'post-migration.py'
    restore = run_path(str(previous))['restore_from_meta']
    env = api.Environment(cr, SUPERUSER_ID, {
        'active_test': False, 'tracking_disable': True, 'mail_notrack': True,
    })
    leads = env['crm.lead']
    counts = Counter()
    for lead in leads.browse(LEAD_IDS).exists():
        page = lead.meta_page_id
        stored = lead.meta_raw_payload
        meta_id = lead.meta_lead_id or ''
        status = 'ineligible'
        if (
            not lead.phone and meta_id.isdecimal()
            and isinstance(stored, dict) and str(stored.get('id')) == meta_id
            and page and page.company_id == lead.company_id and page.page_access_token
            and leads.search_count([('meta_lead_id', '=', meta_id)], limit=2) == 1
        ):
            try:
                with cr.savepoint():
                    payload = page.account_id._request(
                        'GET', meta_id, token=page.page_access_token,
                        params={'fields': 'id,form_id,field_data'},
                    )
            except UserError as exc:
                cause = exc.__cause__
                status = f'api_{type(cause or exc).__name__}'
            else:
                status = restore(lead, payload) if isinstance(payload, dict) else 'invalid_response'
        counts[status] += 1
        _logger.info('Meta phone connector retry: crm_id=%s outcome=%s', lead.id, status)
    _logger.info('Meta phone connector retry: outcomes=%s', dict(counts))
