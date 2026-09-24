import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

REQUIRED_SUBSCRIPTION_FIELDS = ('leadgen', 'messages', 'messaging_postbacks')
FORM_SYNC_MAX_PAGES = 10


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
    subscription_status = fields.Selection([
        ('unknown', 'Unknown'), ('verified', 'Verified'),
        ('incomplete', 'Incomplete'), ('failed', 'Failed'),
    ], default='unknown', readonly=True)
    subscription_checked_at = fields.Datetime(readonly=True)
    subscription_error = fields.Char(readonly=True)
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
            safe = self.account_id._sanitize_error(exc, self._subscription_secrets())
            masked = ('%s…' % str(psid)[:4]) if psid else '?'
            _logger.warning('Could not fetch Meta sender profile %s: %s', masked, safe)
            return False
        full = ' '.join(p for p in (data.get('first_name'), data.get('last_name')) if p)
        return full or data.get('name') or False

    def _subscription_secrets(self):
        self.ensure_one()
        return [self.page_access_token, self.account_id.user_access_token,
                self.account_id.app_secret]

    def _write_subscription_state(self, status, error=False):
        self.write({
            'subscribed': status == 'verified',
            'subscription_status': status,
            'subscription_checked_at': fields.Datetime.now(),
            'subscription_error': error or False,
        })

    def _verify_subscription(self):
        """Confirm via GET that our app is subscribed to this page with
        every required field. Persists the outcome and returns
        ``(status, error)`` — it never raises after writing state,
        because raising would roll back the persisted fields."""
        self.ensure_one()
        account = self.account_id
        try:
            data = account._request(
                'GET', '%s/subscribed_apps' % self.meta_page_id,
                token=self.page_access_token,
                params={'fields': 'id,name,subscribed_fields'})
        except Exception as exc:
            safe = account._sanitize_error(exc, self._subscription_secrets())
            self._write_subscription_state('failed', safe)
            return 'failed', safe
        apps = data.get('data') or []
        ours = next((a for a in apps if str(a.get('id')) == str(account.app_id)), None)
        if not ours:
            error = _('Our Meta app (ID %s) is not subscribed to this page.') % account.app_id
            self._write_subscription_state('failed', error)
            return 'failed', error
        present = ours.get('subscribed_fields') or []
        if isinstance(present, str):
            present = [f.strip() for f in present.split(',') if f.strip()]
        missing = [f for f in REQUIRED_SUBSCRIPTION_FIELDS if f not in present]
        if missing:
            error = _('The app is subscribed but missing required fields: %s') % ', '.join(missing)
            self._write_subscription_state('incomplete', error)
            return 'incomplete', error
        self._write_subscription_state('verified')
        return 'verified', False

    def _subscription_notification(self, status, error=False):
        if status == 'verified':
            notif_type, message = 'success', _(
                'Subscription verified: the app is subscribed with all required fields.')
        elif status == 'incomplete':
            notif_type, message = 'warning', error
        else:
            notif_type, message = 'danger', error
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Meta Page Subscription'),
                'message': message or _('Unknown subscription state.'),
                'type': notif_type,
                'sticky': notif_type != 'success',
            },
        }

    def action_check_subscription(self):
        self.ensure_one()
        status, error = self._verify_subscription()
        return self._subscription_notification(status, error)

    def action_subscribe_webhook(self):
        self.ensure_one()
        account = self.account_id
        try:
            account._request('POST', '%s/subscribed_apps' % self.meta_page_id,
                             token=self.page_access_token,
                             data={'subscribed_fields': ','.join(REQUIRED_SUBSCRIPTION_FIELDS)})
        except Exception as exc:
            # Persist the failure instead of raising: a raised exception
            # would roll back the status fields with the request.
            safe = account._sanitize_error(exc, self._subscription_secrets())
            self._write_subscription_state('failed', safe)
            return self._subscription_notification('failed', safe)
        status, error = self._verify_subscription()
        return self._subscription_notification(status, error)

    def _sync_form_vals(self, item):
        self.ensure_one()
        return {
            'name': item.get('name') or item['id'], 'meta_form_id': item['id'], 'page_id': self.id,
            'company_id': self.company_id.id, 'status': item.get('status'),
            'questions_json': item.get('questions') or [],
            'active': item.get('status') != 'ARCHIVED',
        }

    def _upsert_form_from_meta(self, Form, item):
        """Upsert one leadgen form keyed by (meta_form_id, company_id);
        returns 'created' or 'updated'. ``Form`` must already carry
        ``active_test=False`` so archived forms are found and updated in
        place instead of colliding with the unique constraint. Only sync
        fields are written — mappings, sales team, assigned user, lead
        type, polling settings and last_sync_date are never touched. The
        unique constraint stays the final guard against a concurrent sync:
        on IntegrityError the competitor row is re-fetched and updated,
        and any error without a competitor is re-raised."""
        self.ensure_one()
        vals = self._sync_form_vals(item)
        domain = [('meta_form_id', '=', item['id']), ('company_id', '=', self.company_id.id)]
        form = Form.search(domain, limit=1)
        if form:
            form.write(vals)
            return 'updated'
        try:
            with self.env.cr.savepoint():
                Form.create(vals)
            return 'created'
        except IntegrityError:
            form = Form.search(domain, limit=1)
            if not form:
                raise
            form.write(vals)
            return 'updated'

    def action_sync_forms(self):
        Form = self.env['meta.form'].with_context(active_test=False)
        for rec in self:
            fetched = created = updated = archived = pages = 0
            seen_cursors, after = set(), None
            try:
                while True:
                    params = {'fields': 'id,name,status,created_time,questions', 'limit': 100}
                    if after:
                        params['after'] = after
                    data = rec.account_id._request(
                        'GET', f'{rec.meta_page_id}/leadgen_forms',
                        token=rec.page_access_token, params=params)
                    pages += 1
                    items = data.get('data') or []
                    fetched += len(items)
                    for item in items:
                        if not item.get('id'):
                            continue
                        if rec._upsert_form_from_meta(Form, item) == 'created':
                            created += 1
                        else:
                            updated += 1
                        if item.get('status') == 'ARCHIVED':
                            archived += 1
                    cursors = (data.get('paging') or {}).get('cursors') or {}
                    new_after = cursors.get('after')
                    if not new_after or not items:
                        break
                    if new_after in seen_cursors:
                        _logger.warning(
                            'Meta form sync for page %s: repeated pagination cursor; stopping.',
                            rec.meta_page_id)
                        break
                    if pages >= FORM_SYNC_MAX_PAGES:
                        _logger.warning(
                            'Meta form sync for page %s: page limit %s reached; stopping.',
                            rec.meta_page_id, FORM_SYNC_MAX_PAGES)
                        break
                    seen_cursors.add(new_after)
                    after = new_after
            except IntegrityError:
                raise
            except Exception as exc:
                safe = rec.account_id._sanitize_error(exc, rec._subscription_secrets())
                raise UserError(_('Meta form sync failed: %s') % safe) from exc
            rec.last_sync_at = fields.Datetime.now()
            _logger.info(
                'Meta form sync page %s: fetched=%s created=%s updated=%s archived=%s pages=%s',
                rec.meta_page_id, fetched, created, updated, archived, pages)
        return True
