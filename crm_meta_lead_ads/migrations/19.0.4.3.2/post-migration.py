import logging
import re
from collections import Counter

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def restore_phones(env, records=None):
    """Fill unambiguous Meta phone numbers without replacing existing contact data."""
    leads = env['crm.lead'].sudo().with_context(
        active_test=False, tracking_disable=True, mail_notrack=True,
    )
    stats = Counter()
    candidates = []
    domain = [('meta_lead_id', '!=', False), ('phone', '=', False)]
    if records is not None:
        domain.append(('id', 'in', records.ids))
    for lead in leads.search(domain, order='id'):
        stats['blank_meta_leads'] += 1
        payload = lead.meta_raw_payload or {}
        if not isinstance(payload, dict) or str(payload.get('id')) != lead.meta_lead_id:
            stats['unmatched_payload'] += 1
            continue
        if leads.search_count([('meta_lead_id', '=', lead.meta_lead_id)], limit=2) != 1:
            stats['duplicate_meta_id'] += 1
            continue
        fields = payload.get('field_data')
        if not isinstance(fields, list):
            stats['unusable_phone'] += 1
            continue
        numbers = {}
        invalid = False
        for item in fields:
            if not isinstance(item, dict) or item.get('name') not in ('phone', 'phone_number'):
                continue
            values = item.get('values') or []
            if not isinstance(values, list):
                invalid = True
                continue
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    continue
                value = value.strip()
                digits = re.sub(r'\D', '', value)
                if not re.fullmatch(r'\+?[0-9 ()-]+', value) or not 7 <= len(digits) <= 15:
                    invalid = True
                    continue
                numbers[digits] = value
        if invalid or len(numbers) != 1:
            stats['unusable_phone'] += 1
            continue
        partner = lead.partner_id
        phone = next(iter(numbers.values()))
        if partner and (
            partner.phone
            or leads.search_count([('partner_id', '=', partner.id)], limit=2) != 1
        ):
            stats['partner_conflict_or_shared'] += 1
            continue
        candidates.append((lead, phone))
    for lead, phone in candidates:
        env.cr.execute('SELECT id FROM crm_lead WHERE id = %s FOR UPDATE', [lead.id])
        lead.invalidate_recordset(['phone', 'partner_id'])
        if lead.phone:
            raise RuntimeError(f'CRM {lead.id}: phone changed during the run')
        if lead.partner_id:
            env.cr.execute('SELECT id FROM res_partner WHERE id = %s FOR UPDATE', [lead.partner_id.id])
            lead.partner_id.invalidate_recordset(['phone'])
            if lead.partner_id.phone or leads.search_count(
                [('partner_id', '=', lead.partner_id.id)], limit=2
            ) != 1:
                raise RuntimeError(f'CRM {lead.id}: linked contact changed during the run')
        lead.write({'phone': phone})
    env.flush_all()
    for lead, phone in candidates:
        lead.invalidate_recordset(['phone'])
        if re.sub(r'\D', '', lead.phone or '') != re.sub(r'\D', '', phone):
            raise RuntimeError(f'CRM {lead.id}: verification failed')
    return stats, [lead.id for lead, _ in candidates]


def migrate(cr, version):
    if not version or cr.dbname != 'inverse_elite':
        return
    stats, restored = restore_phones(api.Environment(cr, SUPERUSER_ID, {}))
    _logger.info(
        'Meta phone backfill: verified=%s lead_ids=%s skipped_counts=%s',
        len(restored), restored, dict(stats),
    )
