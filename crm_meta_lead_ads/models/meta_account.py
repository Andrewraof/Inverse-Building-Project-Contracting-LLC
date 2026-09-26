import logging
import uuid
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Permissions the connector is designed around (Graph API current).
REQUIRED_PERMISSIONS = (
    'leads_retrieval', 'pages_show_list', 'pages_read_engagement',
    'pages_manage_metadata', 'pages_messaging',
)


class MetaPermissionError(UserError):
    """Graph explicitly denied an operation for lack of permission."""


class MetaAccount(models.Model):
    _name = 'meta.account'
    _description = 'Meta Lead Ads Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(required=True, tracking=True)
    connection_type = fields.Selection([('provider_oauth', 'Provider OAuth'), ('system_user', 'Client-Owned / System User')], default='provider_oauth', required=True, tracking=True)
    webhook_key = fields.Char(default=lambda self: uuid.uuid4().hex, required=True, readonly=True, copy=False, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    meta_user_id = fields.Char(readonly=True, index=True)
    app_id = fields.Char(required=True, groups='base.group_system')
    app_secret = fields.Char(required=True, groups='base.group_system')
    user_access_token = fields.Text(groups='base.group_system', copy=False)
    token_expires_at = fields.Datetime(groups='base.group_system', copy=False)
    state = fields.Selection([
        ('draft', 'Draft'), ('connected', 'Connected'), ('error', 'Error'), ('disconnected', 'Disconnected')
    ], default='draft', required=True, tracking=True, index=True)
    error_message = fields.Text(readonly=True)
    page_ids = fields.One2many('meta.page', 'account_id')
    webhook_url = fields.Char(compute='_compute_urls')
    deletion_url = fields.Char(compute='_compute_urls')
    last_connected_at = fields.Datetime(readonly=True)
    # Diagnostics — codenames only, never tokens.
    granted_permissions = fields.Char(readonly=True, copy=False)
    missing_permissions = fields.Char(readonly=True, copy=False)
    permissions_checked_at = fields.Datetime(readonly=True, copy=False)
    app_mode = fields.Char(readonly=True, copy=False,
                           default='Unknown — check the Meta App Dashboard')
    last_diagnostic_at = fields.Datetime(readonly=True, copy=False)
    last_diagnostic_outcome = fields.Selection([
        ('success', 'Success'), ('warning', 'Warning'),
        ('failure', 'Failure'), ('unknown', 'Unknown'),
    ], readonly=True, copy=False)


    @api.depends('webhook_key')
    def _compute_urls(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        for rec in self:
            rec.webhook_url = f'{base}/meta_crm/webhook/{rec.webhook_key}' if rec.webhook_key else False
            rec.deletion_url = f'{base}/meta_crm/data_deletion/{rec.webhook_key}' if rec.webhook_key else False

    def action_test_connection(self):
        for rec in self:
            token = rec.user_access_token or rec.page_ids[:1].page_access_token
            if not token:
                raise UserError(_('Configure a User/System User token or at least one Page token first.'))
            rec._request('GET', 'me', token=token, params={'fields': 'id,name'})
            rec.write({'state': 'connected', 'error_message': False})
        return True

    def _api_version(self):
        return self.env['ir.config_parameter'].sudo().get_param('crm_meta_lead_ads.api_version', 'v25.0')

    def _graph_url(self, path):
        return f"https://graph.facebook.com/{self._api_version()}/{path.lstrip('/')}"

    @api.model
    def _sanitize_error(self, message, token=None):
        message = str(message or '')
        tokens = token if isinstance(token, (list, tuple)) else [token]
        for tok in tokens:
            if tok:
                message = message.replace(str(tok), '***')
        return message

    def _request(self, method, path, token=None, **kwargs):
        self.ensure_one()
        headers = kwargs.pop('headers', {})
        params = kwargs.pop('params', {})
        if token:
            params['access_token'] = token
        timeout = int(self.env['ir.config_parameter'].sudo().get_param('crm_meta_lead_ads.http_timeout', '15'))
        try:
            response = requests.request(method, self._graph_url(path), headers=headers, params=params, timeout=timeout, **kwargs)
            data = response.json() if response.content else {}
        except Exception as exc:
            raise UserError(_('Meta API request failed: %s') % self._sanitize_error(exc, token)) from exc
        if response.status_code >= 400 or (isinstance(data, dict) and data.get('error')):
            err = data.get('error', {}) if isinstance(data, dict) else {}
            self._handle_graph_error(err, token=token)
            if err.get('code') in (10, 200):
                # Do not persist the remote error text: it may contain a
                # token or customer data. The step and code are sufficient.
                raise MetaPermissionError(
                    _('Meta denied permission for this operation (code %s).')
                    % err['code'])
            raise UserError(_('Meta API error: %s') % self._sanitize_error(err.get('message') or response.text, token))
        return data

    def _handle_graph_error(self, err, token=None):
        self.ensure_one()
        code = err.get('code')
        subcode = err.get('error_subcode')
        msg = self._sanitize_error(err.get('message') or '', token)
        if code == 190:
            self.write({'state': 'error', 'error_message': f'{code}/{subcode}: {msg}'})
            self.activity_schedule('mail.mail_activity_data_todo', summary=_('Meta token requires re-authentication'), note=msg)
        elif code in (200, 368):
            self.write({'state': 'error', 'error_message': f'{code}: {msg}'})

    def _fetch_permissions(self):
        """Fetch granted permissions via GET me/permissions and refresh the
        diagnostics fields. Returns (granted, missing) codename lists, or
        (None, None) when the endpoint is unavailable — callers must keep
        going in that case (the endpoint may be restricted in some modes)."""
        self.ensure_one()
        if not self.user_access_token:
            return None, None
        try:
            data = self._request('GET', 'me/permissions',
                                 token=self.user_access_token, params={'limit': 200})
        except Exception:
            return None, None
        if not data.get('data'):
            # An empty edge does not prove that every scope was denied.
            # Let each Graph API operation establish its own access result.
            self.write({
                'granted_permissions': False,
                'missing_permissions': False,
                'permissions_checked_at': False,
            })
            return None, None
        granted = {p.get('name') for p in data.get('data') or []
                   if p.get('status') == 'granted' and p.get('name')}
        missing = [p for p in REQUIRED_PERMISSIONS if p not in granted]
        self.write({
            'granted_permissions': ','.join(sorted(granted)) or False,
            'missing_permissions': ','.join(missing) or False,
            'permissions_checked_at': fields.Datetime.now(),
        })
        return sorted(granted), missing

    def action_run_diagnostics(self):
        """Refresh connection state, granted/missing permissions and the
        webhook subscription of every page, then aggregate the outcomes
        into an explicit success/warning/failure/unknown result. Never
        raises: each check writes its own sanitized outcome, so recorded
        findings survive even when a later check cannot run. A failed
        connection or page subscription can never produce a success
        banner; empty permission evidence stays unknown, not a denial.

        Access: the interactive guard runs FIRST (Meta manager group +
        company membership, verified on the caller's own recordset);
        ONLY THEN does the body escalate with sudo, which the protected
        reads require (tokens/app_secret are base.group_system-only).
        The returned notification is static text — no secrets and no
        other company's data ever reach the caller."""
        self.ensure_one()
        self._check_health_manager_access()
        account = self.sudo()
        connection_ok = True
        try:
            account.action_test_connection()
        except Exception as exc:
            safe = account._sanitize_error(
                exc, [account.user_access_token, account.app_secret])
            account.write({'state': 'error', 'error_message': safe})
            connection_ok = False
        granted, missing = account._fetch_permissions()
        subscription_states = []
        for page in account.with_context(active_test=False).page_ids:
            if page.page_access_token:
                status, _error = page._verify_subscription()
                subscription_states.append(status)
        if not connection_ok or 'failed' in subscription_states:
            outcome, notif_type = 'failure', 'danger'
            message = _('Diagnostics found failures: the connection or a page '
                        'subscription check failed. Check the Diagnostics tab and '
                        'each page subscription status.')
        elif missing or 'incomplete' in subscription_states:
            outcome, notif_type = 'warning', 'warning'
            message = _('Diagnostics completed with warnings. Check the Diagnostics tab.')
        elif granted is None and subscription_states:
            # Connection and subscriptions verified; only the permission
            # report is unavailable. Unavailable evidence is not a denial.
            outcome, notif_type = 'warning', 'warning'
            message = _('Connection and page subscriptions verified, but the '
                        'permission report was unavailable. Check the Diagnostics tab.')
        elif granted is None:
            outcome, notif_type = 'unknown', 'info'
            message = _('Diagnostics could verify only part of the setup; permission '
                        'evidence is unknown (an empty result is not a denial). '
                        'Check the Diagnostics tab.')
        else:
            outcome, notif_type = 'success', 'success'
            message = _('Diagnostics passed: connection, permissions and page '
                        'subscriptions verified.')
        account.write({
            'last_diagnostic_at': fields.Datetime.now(),
            'last_diagnostic_outcome': outcome,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Meta Diagnostics'),
                'message': message,
                'type': notif_type,
                'sticky': outcome in ('failure', 'unknown'),
            },
        }

    def action_sync_all_meta_data(self):
        """Create (or reopen) the single active full-sync run for this
        account. The heavy work is done by the meta.sync.run cron in
        resumable ticks — this HTTP request returns immediately."""
        self.ensure_one()
        Run = self.env['meta.sync.run']
        existing = Run.search([
            ('account_id', '=', self.id),
            ('state', 'in', ('draft', 'running')),
        ], limit=1)
        if existing:
            return existing._form_action()
        run = Run.create({
            'account_id': self.id,
            'company_id': self.company_id.id,
            'run_type': 'all',
        })
        run.action_start()
        return run._form_action()

    def action_disconnect(self):
        Page = self.env['meta.page'].with_context(active_test=False)
        for rec in self:
            Page.search([('account_id', '=', rec.id)]).write({
                'active': False, 'sync_enabled': False, 'page_access_token': False,
            })
            rec.write({'state': 'disconnected', 'user_access_token': False, 'error_message': False})
        return True
