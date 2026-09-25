"""Backfill unambiguous first replies and linked-lead report counts.

This runs on upgrades from 19.0.4.0.0 as well as earlier versions. It is
safe to repeat and logs only aggregate counts, never message or lead data.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)
BATCH_SIZE = 500


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Conv = env['meta.conversation'].sudo().with_context(active_test=False)
    last_id = 0
    visited = 0
    updated = 0
    while True:
        batch = Conv.search([('id', '>', last_id)], order='id', limit=BATCH_SIZE)
        if not batch:
            break
        last_id = batch[-1].id
        for conv in batch:
            visited += 1
            before = conv.first_response_at
            conv._refresh_history_metrics()
            if conv.first_response_at != before:
                updated += 1
        # The stored computed field is recomputed by Odoo on module upgrade;
        # keep legacy rows explicit even if no dependency changed this run.
        cr.execute(
            'UPDATE meta_conversation '
            'SET linked_lead_count = CASE WHEN lead_id IS NULL THEN 0 ELSE 1 END '
            'WHERE id = ANY(%s) AND linked_lead_count IS DISTINCT FROM '
            'CASE WHEN lead_id IS NULL THEN 0 ELSE 1 END',
            (batch.ids,))
    _logger.info(
        'crm_meta_lead_ads 19.0.4.1.0: visited %s conversations, '
        'backfilled %s first reply timestamps', visited, updated)
