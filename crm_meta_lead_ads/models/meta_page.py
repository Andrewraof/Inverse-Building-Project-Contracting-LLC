import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class MetaPage(models.Model):
    _name = 'meta.page'
    _description = 'Meta Facebook Page'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    account_id = fields.Many2one('meta.account', required=True, ondelete='cascade', index=True)
    meta_page_id = fields.Char(required=True, index=True)
    page_access_token = fields.Text(groups='base.group_system', copy=False)
    page_permissions = fields.Char(readonly=True, copy=False)
    sync_enabled = fields.Boolean(default=True, tracking=True)
    subscribed = fields.Boolean(default=False, readonly=True)
    form_ids = fields.One2many('meta.form', 'page_id')
    last_sync_at = fields.Datetime(readonly=True)

    _unique_page_company = models.Constraint('UNIQUE(meta_page_id, company_id)', 'This Meta Page is already configured for this company.')

    @api.constrains('page_access_token', 'meta_page_id', 'active', 'sync_enabled')
    def _check_page_access_token(self):
        for rec in self:
            token = (rec.page_access_token or '').strip()
            page_ref = str(rec.meta_page_id or '')
            if token and token == page_ref:
                raise ValidationError(_(
                    "Invalid Page Access Token for page '%s' (ID %s): the token must not equal the Page ID."
                ) % (rec.name, page_ref))
            if rec.active and rec.sync_enabled and not token:
                raise ValidationError(_(
                    "Page '%s' (ID %s) is active and sync-enabled but has no Page Access Token. "
                    "Reconnect via Meta OAuth or enter a valid token."
                ) % (rec.name, page_ref))

    def _upsert_from_meta(self, company, account, data):
        meta_page_id = str(data.get('id') or '')
        if not meta_page_id:
            return self.browse()
        tasks = data.get('tasks') or []
        vals = {
            'name': data.get('name') or meta_page_id,
            'company_id': company.id,
            'account_id': account.id,
            'meta_page_id': meta_page_id,
            'page_permissions': ','.join(tasks) if tasks else False,
            'active': True,
            'sync_enabled': True,
        }
        token = str(data.get('access_token') or '').strip()
        # A Page Access Token must be a real token: never accept the page
        # id as a token, and never blank a stored token with an empty
        # Meta response.
        if token and token != meta_page_id:
            vals['page_access_token'] = token
        # Include archived pages: a disconnected/archived page must be
        # reactivated and updated, not recreated (unique constraint).
        Page = self.with_context(active_test=False)
        page = Page.search([('meta_page_id', '=', meta_page_id), ('company_id', '=', company.id)], limit=1)
        if page:
            if 'page_access_token' not in vals:
                stored = (page.page_access_token or '').strip()
                if not stored or stored == meta_page_id:
                    raise UserError(_(
                        "Page '%(name)s' (ID %(page_id)s) has no valid stored Page Access Token and Meta "
                        "returned none. Re-run Connect and grant the app access to this page in the "
                        "Meta permissions dialog."
                    ) % {'name': page.name, 'page_id': meta_page_id})
            page.write(vals)
            return page
        if 'page_access_token' not in vals:
            raise UserError(_(
                "Cannot configure Meta Page '%(name)s' (ID %(page_id)s): Meta returned no Page Access Token "
                "for this page. Re-run Connect and grant the app access to this page in the Meta permissions dialog."
            ) % {'name': vals['name'], 'page_id': meta_page_id})
        try:
            with self.env.cr.savepoint():
                return self.create(vals)
        except IntegrityError:
            page = Page.search([('meta_page_id', '=', meta_page_id), ('company_id', '=', company.id)], limit=1)
            if page:
                page.write(vals)
                return page
            raise

    def _upsert_pages_bulk(self, company, account, pages_data):
        """Upsert pages one by one in isolated savepoints so a single
        failing page never stops the remaining ones."""
        results, synced, failed = [], 0, 0
        for p in pages_data:
            label = f"{p.get('name') or '?'} ({p.get('id') or 'no-id'})"
            if not p.get('id'):
                results.append((label, 'skipped: Meta returned no id for this page'))
                continue
            try:
                with self.env.cr.savepoint():
                    self._upsert_from_meta(company, account, p)
                synced += 1
                results.append((label, 'synced'))
            except Exception as exc:
                failed += 1
                # Never leak secrets: sanitize the exception text before it
                # reaches chatter, the sync summary, or the logs.
                safe_msg = account._sanitize_error(
                    exc, [account.user_access_token, account.app_secret, p.get('access_token')])
                results.append((label, f'failed: {safe_msg}'))
                _logger.error('Meta page sync failed for page %s: %s', label, safe_msg)
        return synced, failed, results

    def _fetch_sender_name(self, psid):
        self.ensure_one()
        try:
            data = self.account_id._request('GET', str(psid), token=self.page_access_token,
                                            params={'fields': 'first_name,last_name,name'})
        except Exception as exc:
            _logger.warning('Could not fetch Meta sender profile %s: %s', psid, exc)
            return False
        full = ' '.join(p for p in (data.get('first_name'), data.get('last_name')) if p)
        return full or data.get('name') or False

    def action_subscribe_webhook(self):
        for rec in self:
            data = rec.account_id._request('POST', f'{rec.meta_page_id}/subscribed_apps', token=rec.page_access_token,
                                           data={'subscribed_fields': 'leadgen,messages,messaging_postbacks'})
            if data.get('success'):
                rec.subscribed = True
        return True

    def action_sync_forms(self):
        Form = self.env['meta.form']
        for rec in self:
            data = rec.account_id._request('GET', f'{rec.meta_page_id}/leadgen_forms', token=rec.page_access_token,
                                           params={'fields': 'id,name,status,created_time,questions', 'limit': 100})
            for item in data.get('data', []):
                vals = {
                    'name': item.get('name') or item['id'], 'meta_form_id': item['id'], 'page_id': rec.id,
                    'company_id': rec.company_id.id, 'status': item.get('status'), 'questions_json': item.get('questions') or [],
                    'active': item.get('status') != 'ARCHIVED',
                }
                form = Form.search([('meta_form_id', '=', item['id']), ('company_id', '=', rec.company_id.id)], limit=1)
                form.write(vals) if form else Form.create(vals)
            rec.last_sync_at = fields.Datetime.now()
        return True
