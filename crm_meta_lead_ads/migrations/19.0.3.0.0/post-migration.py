import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Backfill crm.lead.meta_adset_id from the legacy meta_adgroup_id column.

    Idempotent: only rows missing the new value are touched, so re-running
    is a no-op. Rows that already have meta_adset_id are never overwritten.
    Logs row counts only — never lead data or payloads.
    """
    cr.execute("""
        UPDATE crm_lead
           SET meta_adset_id = meta_adgroup_id
         WHERE meta_adset_id IS NULL
           AND meta_adgroup_id IS NOT NULL
    """)
    _logger.info(
        'crm_meta_lead_ads %s: backfilled meta_adset_id on %s crm.lead row(s)',
        version, cr.rowcount)
