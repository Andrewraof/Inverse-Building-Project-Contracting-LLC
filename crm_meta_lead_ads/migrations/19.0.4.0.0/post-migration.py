"""Post-migration for 19.0.4.0.0: backfill conversation metrics.

For every existing conversation, derive the latest delivered message,
``last_inbound_at``, and ``first_response_seconds`` from the first
inbound -> first later successful outbound pair. Idempotent, batched to
avoid long locks, logs counts only — no PII, no tokens.
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
            before = (conv.last_inbound_at, conv.first_response_seconds,
                      conv.last_message_at, conv.last_message_preview)
            conv._refresh_history_metrics()
            after = (conv.last_inbound_at, conv.first_response_seconds,
                     conv.last_message_at, conv.last_message_preview)
            if before != after:
                updated += 1
    _logger.info(
        'crm_meta_lead_ads 19.0.4.0.0: visited %s conversations, backfilled %s',
        visited, updated)
