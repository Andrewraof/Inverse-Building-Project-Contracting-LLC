import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Link pre-conversation messages into conversations (see
    meta.conversation._link_legacy_messages). Safe to re-run."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['meta.conversation']._link_legacy_messages()
