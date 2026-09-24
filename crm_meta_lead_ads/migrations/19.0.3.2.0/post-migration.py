import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def _backfill_norm_fields(env):
    """Normalize email/phone match keys for Meta-linked leads only.

    Uses stable id-pagination: every Meta-linked lead is visited exactly
    once per run, so the migration always terminates — even for leads
    whose email/phone is empty or invalid (normalization yields False
    and the row simply keeps False). Only changed values are written,
    which also makes re-runs cheap no-ops (idempotent).
    Logs counts only — never customer data.
    """
    Lead = env['crm.lead']
    visited = 0
    changed = 0
    batches = 0
    last_id = 0
    while True:
        leads = Lead.search(
            [('meta_lead_id', '!=', False), ('id', '>', last_id)],
            order='id', limit=BATCH_SIZE)
        if not leads:
            break
        last_id = leads[-1].id
        batches += 1
        visited += len(leads)
        for lead in leads:
            vals = lead._meta_norm_vals()
            to_write = {k: v for k, v in vals.items() if lead[k] != v}
            if to_write:
                lead.with_context(meta_norm_sync=True).write(to_write)
                changed += 1
    return visited, changed, batches


def _backfill_identities(env):
    """Create meta.lead.identity rows for pre-existing Meta leads so the
    linkage table is complete. Batched with id-pagination; skips leads
    without a company (conservative) and IDs already linked."""
    Lead = env['crm.lead']
    Identity = env['meta.lead.identity']
    created = 0
    skipped_no_company = 0
    last_id = 0
    while True:
        leads = Lead.search(
            [('meta_lead_id', '!=', False), ('id', '>', last_id)],
            order='id', limit=BATCH_SIZE)
        if not leads:
            break
        last_id = leads[-1].id
        for lead in leads:
            if not lead.company_id:
                skipped_no_company += 1
                continue
            if Identity.search_count([
                    ('meta_lead_id', '=', lead.meta_lead_id),
                    ('company_id', '=', lead.company_id.id)]):
                continue
            Identity.create({
                'company_id': lead.company_id.id, 'crm_lead_id': lead.id,
                'meta_lead_id': lead.meta_lead_id, 'match_type': 'backfill',
            })
            created += 1
    return created, skipped_no_company


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    visited, changed, batches = _backfill_norm_fields(env)
    identities, skipped = _backfill_identities(env)
    _logger.info(
        'crm_meta_lead_ads %s: visited %s Meta-linked leads in %s batches, '
        'normalized %s; created %s identity rows (%s leads skipped without company)',
        version, visited, batches, changed, identities, skipped)
