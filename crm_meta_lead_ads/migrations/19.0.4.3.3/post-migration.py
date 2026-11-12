import logging
import re
from collections import Counter

import requests

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)
PREVIOUS_BACKFILL_UTC = '2026-11-12 07:46:56'


def unique_phone(payload):
    numbers = {}
    fields = payload.get('field_data')
    if not isinstance(fields, list):
        return None
    for field in fields:
        if not isinstance(field, dict) or field.get('name') not in ('phone', 'phone_number'):
            continue
        values = field.get('values') or []
        if not isinstance(values, list):
            return None
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            digits = re.sub(r'\D', '', value)
            if not re.fullmatch(r'\+?[0-9 ()-]+', value) or not 7 <= len(digits) <= 15:
                return None
            numbers[digits] = value
    return next(iter(numbers.values())) if len(numbers) == 1 else None


def fetch_meta(lead):
    page = lead.meta_page_id
    if not page or page.company_id != lead.company_id or not page.page_access_token:
        return 'missing_page_access', None
    version = page.account_id._api_version()
    if not re.fullmatch(r'v\d+\.\d+', version) or not lead.meta_lead_id.isdecimal():
        return 'invalid_source', None
    try:
        response = requests.get(
            f'https://graph.facebook.com/{version}/{lead.meta_lead_id}',
            headers={'Authorization': f'Bearer {page.page_access_token}'},
            params={'fields': 'id,form_id,field_data'},
            timeout=(3, 5),
            allow_redirects=False,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        return 'request_failed', None
    if not isinstance(payload, dict):
        return 'invalid_response', None
    if not response.ok or 'error' in payload:
        error = payload.get('error')
        code = error.get('code') if isinstance(error, dict) else None
        return f'meta_error_{code}' if isinstance(code, int) else 'meta_error', None
    return 'fetched', payload


def restore_from_meta(lead, payload):
    """Accept only the same Meta lead and never replace a stored phone."""
    original = lead.meta_raw_payload
    if str(payload.get('id')) != lead.meta_lead_id:
        return 'id_mismatch'
    if original.get('form_id') and str(payload.get('form_id')) != str(original['form_id']):
        return 'form_mismatch'
    phone = unique_phone(payload)
    if not phone:
        return 'no_unique_phone'
    lead.env.cr.execute('SELECT id FROM crm_lead WHERE id = %s FOR UPDATE', [lead.id])
    lead.invalidate_recordset(['phone', 'partner_id'])
    if lead.phone:
        return 'phone_already_present'
    partner = lead.partner_id
    if partner:
        lead.env.cr.execute('SELECT id FROM res_partner WHERE id = %s FOR UPDATE', [partner.id])
        partner.invalidate_recordset(['phone'])
        if partner.phone or lead.env['crm.lead'].search_count(
            [('partner_id', '=', partner.id)], limit=2,
        ) != 1:
            return 'linked_contact_not_safe'
    lead.write({'phone': phone})
    lead.env.flush_all()
    lead.invalidate_recordset(['phone'])
    if re.sub(r'\D', '', lead.phone or '') != re.sub(r'\D', '', phone):
        raise RuntimeError(f'CRM {lead.id}: phone verification failed')
    return 'restored'


def migrate(cr, version):
    if not version or cr.dbname != 'inverse_elite':
        return
    env = api.Environment(cr, SUPERUSER_ID, {
        'active_test': False, 'tracking_disable': True, 'mail_notrack': True,
    })
    leads = env['crm.lead']
    candidates = leads.search([
        ('meta_lead_id', '!=', False), ('phone', '=', False),
        ('write_date', '<', PREVIOUS_BACKFILL_UTC),
    ], order='id').filtered(
        lambda lead: isinstance(lead.meta_raw_payload, dict)
        and str(lead.meta_raw_payload.get('id')) == lead.meta_lead_id
        and not unique_phone(lead.meta_raw_payload)
    )
    if len(candidates) > 8:
        _logger.warning('Meta phone source recheck: scope mismatch count=%s', len(candidates))
        return
    counts = Counter()
    for lead in candidates:
        status = 'duplicate_id'
        if leads.search_count([('meta_lead_id', '=', lead.meta_lead_id)], limit=2) == 1:
            status, payload = fetch_meta(lead)
            if status == 'fetched':
                status = restore_from_meta(lead, payload)
        counts[status] += 1
        _logger.info('Meta phone source recheck: crm_id=%s outcome=%s', lead.id, status)
    _logger.info('Meta phone source recheck: candidates=%s outcomes=%s', len(candidates), dict(counts))
