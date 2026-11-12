import logging
from pathlib import Path
from runpy import run_path

import requests

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)
CRM_ID = 969
META_ID = '2169139376997711'


def migrate(cr, version):
    if not version or cr.dbname != 'inverse_elite':
        return
    env = api.Environment(cr, SUPERUSER_ID, {
        'active_test': False, 'tracking_disable': True, 'mail_notrack': True,
    })
    leads = env['crm.lead']
    lead = leads.browse(CRM_ID).exists()
    if not lead or lead.meta_lead_id != META_ID or leads.search_count(
        [('meta_lead_id', '=', META_ID)], limit=2,
    ) != 1:
        _logger.info('Meta phone 969: identity_not_confirmed')
        return
    stored = lead.meta_raw_payload
    page = lead.meta_page_id
    if not isinstance(stored, dict) or str(stored.get('id')) != META_ID or (
        not page or page.company_id != lead.company_id or not page.page_access_token
    ):
        _logger.info('Meta phone 969: missing_matching_source_access')
        return
    try:
        response = requests.get(
            page.account_id._graph_url(META_ID),
            params={'fields': 'id,form_id,field_data', 'access_token': page.page_access_token},
            timeout=(4, 10),
            allow_redirects=False,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        _logger.info('Meta phone 969: request_error=%s', type(exc).__name__)
        return
    error = payload.get('error') if isinstance(payload, dict) else None
    if not response.ok or error or not isinstance(payload, dict):
        error = error if isinstance(error, dict) else {}
        code = error.get('code')
        subcode = error.get('error_subcode')
        message = str(error.get('message') or '').lower()
        if 'unsupported get request' in message:
            reason = 'object_unavailable_or_permission_denied'
        elif code == 190:
            reason = 'access_token_rejected'
        elif code in (10, 200):
            reason = 'permission_denied'
        elif 'expired' in message:
            reason = 'expired'
        else:
            reason = 'graph_rejected_request'
        _logger.info('Meta phone 969: http=%s code=%s subcode=%s reason=%s',
                     response.status_code, code if isinstance(code, int) else None,
                     subcode if isinstance(subcode, int) else None, reason)
        return
    previous = Path(__file__).resolve().parents[1] / '19.0.4.3.3' / 'post-migration.py'
    restore = run_path(str(previous))['restore_from_meta']
    _logger.info('Meta phone 969: outcome=%s', restore(lead, payload))
