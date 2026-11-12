import logging
from pathlib import Path
from runpy import run_path

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)
VERIFIED_PHONES = {
    359: '1142278525019138',
    302: '1107484485364919',
    301: '2089877771618806',
    276: '1763751494913928',
    274: '1612205056981210',
    273: '1817658826340362',
}
VERIFIED_LINKS = {
    339: (836, '1555253836643715'),
    332: (812, '1658403645841968'),
    331: (911, '1914845339498296'),
}


def migrate(cr, version):
    if not version or cr.dbname != 'inverse_elite':
        return
    env = api.Environment(cr, SUPERUSER_ID, {
        'active_test': False, 'tracking_disable': True, 'mail_notrack': True,
    })
    previous = Path(__file__).resolve().parents[1] / '19.0.4.3.3' / 'post-migration.py'
    unique_phone = run_path(str(previous))['unique_phone']
    Partner, Lead = env['res.partner'], env['crm.lead']
    repaired_phones, repaired_links = [], []
    for partner_id, meta_id in VERIFIED_PHONES.items():
        partner = Partner.browse(partner_id).exists()
        if not partner:
            continue
        cr.execute('SELECT id FROM res_partner WHERE id = %s FOR UPDATE', [partner_id])
        partner.invalidate_recordset(['phone', 'meta_review_state', 'meta_review_queue_id'])
        queue = partner.meta_review_queue_id
        payload = queue.fetched_payload
        if (
            partner.phone or partner.meta_review_state != 'pending'
            or not queue or queue.meta_lead_id != meta_id or queue.company_id != partner.company_id
            or queue.review_partner_id != partner or queue.crm_lead_id
            or not isinstance(payload, dict) or str(payload.get('id')) != meta_id
            or Lead.search_count([('partner_id', '=', partner.id)], limit=1)
        ):
            continue
        phone = unique_phone(payload)
        if phone:
            partner.write({'phone': phone})
            partner.flush_recordset(['phone'])
            partner.invalidate_recordset(['phone'])
            if partner.phone != phone:
                raise RuntimeError(f'Meta phone verification failed for contact {partner_id}')
            repaired_phones.append(partner_id)

    for partner_id, (lead_id, meta_id) in VERIFIED_LINKS.items():
        partner = Partner.browse(partner_id).exists()
        lead = Lead.browse(lead_id).exists()
        if not partner or not lead or partner.meta_review_state != 'approved':
            continue
        queue = partner.meta_review_queue_id
        email = (partner.email or '').strip().casefold()
        if (
            partner.meta_review_lead_id or not queue or queue.crm_lead_id
            or queue.review_partner_id != partner or queue.meta_lead_id != meta_id
            or not partner.company_id or queue.company_id != partner.company_id
            or lead.partner_id != partner or lead.company_id != partner.company_id
            or not email or (lead.email_from or '').strip().casefold() != email
            or Lead.search_count([('partner_id', '=', partner.id)], limit=2) != 1
            or Lead.search_count([('meta_lead_id', '=', meta_id)], limit=1)
        ):
            continue
        queue._ensure_identity(lead, 'matched_email')
        queue.write({'crm_lead_id': lead.id})
        partner.write({'meta_review_lead_id': lead.id})
        env.flush_all()
        partner.invalidate_recordset(['meta_review_lead_id'])
        queue.invalidate_recordset(['crm_lead_id'])
        if partner.meta_review_lead_id != lead or queue.crm_lead_id != lead:
            raise RuntimeError(f'Meta CRM link verification failed for contact {partner_id}')
        repaired_links.append((partner_id, lead_id))
    _logger.info('Meta verified repair: phones=%s links=%s', repaired_phones, repaired_links)
