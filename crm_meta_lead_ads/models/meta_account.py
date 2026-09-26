import logging
import uuid
from datetime import timedelta
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

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
    diagnostic_status = fields.Selection([
        ('unknown', 'Unknown'), ('success', 'Success'),
        ('warning', 'Warning'), ('failure', 'Failure'),
    ], default='unknown', readonly=True, copy=False)
    diagnostic_checked_at = fields.Datetime(readonly=True, copy=False)
    app_mode = fields.Char(readonly=True, copy=False,
                           default='Unknown — check the Meta App Dashboard')
    health_monitor_enabled = fields.Boolean(default=False, copy=False)
    health_owner_id = fields.Many2one('res.users', copy=False)
    health_due_minutes = fields.Integer(default=15)
    health_silence_minutes = fields.Integer(default=0,
                                            help='Zero disables expected-traffic warnings.')
    health_token_warning_days = fields.Integer(default=7)
    health_enabled_at = fields.Datetime(copy=False, readonly=True)
    health_checked_at = fields.Datetime(copy=False, readonly=True)


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
            self.write({'granted_permissions': False,
                        'missing_permissions': False,
                        'permissions_checked_at': False})
            return None, None
        try:
            data = self._request('GET', 'me/permissions',
                                 token=self.user_access_token, params={'limit': 200})
        except Exception:
            # Old scopes cannot be presented as fresh evidence after a failed
            # check. Individual page-token operations remain independent.
            self.write({'granted_permissions': False,
                        'missing_permissions': False,
                        'permissions_checked_at': False})
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
        webhook subscription of every page. Never raises: each check
        writes its own sanitized outcome."""
        self.ensure_one()
        if not self.env.user.has_group('crm_meta_lead_ads.group_meta_lead_manager'):
            raise UserError(_('Only Meta Lead Ads managers may run diagnostics.'))
        if self.company_id not in self.env.companies:
            raise UserError(_('This account is outside your allowed companies.'))
        connection_ok = True
        try:
            self.action_test_connection()
        except Exception as exc:
            connection_ok = False
            safe = self._sanitize_error(
                exc, [self.user_access_token, self.app_secret])
            self.write({'state': 'error', 'error_message': safe})
        granted, missing = self._fetch_permissions()
        page_statuses = []
        for page in self.page_ids.filtered(lambda p: p.active and p.sync_enabled):
            if page.page_access_token:
                status, _error = page._verify_subscription()
                page_statuses.append(status)
            else:
                page_statuses.append('failed')
        if not connection_ok or 'failed' in page_statuses:
            result = 'failure'
        elif missing or 'incomplete' in page_statuses:
            result = 'warning'
        elif granted is None or not page_statuses:
            result = 'unknown'
        else:
            result = 'success'
        self.write({'diagnostic_status': result,
                    'diagnostic_checked_at': fields.Datetime.now()})
        notif_types = {'failure': 'danger', 'warning': 'warning',
                       'unknown': 'info', 'success': 'success'}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Meta Diagnostics'),
                'message': _('Diagnostics refreshed. Check the Diagnostics tab.'),
                'type': notif_types[result],
                'sticky': False,
            },
        }

    @api.constrains('health_owner_id', 'company_id', 'health_due_minutes',
                    'health_silence_minutes', 'health_token_warning_days')
    def _check_health_settings(self):
        for account in self:
            if (account.health_due_minutes < 1 or account.health_silence_minutes < 0
                    or account.health_token_warning_days < 0):
                raise ValidationError(_('Invalid Meta health monitoring threshold.'))
            owner = account.health_owner_id
            if owner and not account._health_owner_eligible(owner):
                raise ValidationError(_('The health owner must be an active internal Meta manager with access to this company.'))

    def _health_owner_eligible(self, owner=None):
        self.ensure_one()
        owner = owner or self.health_owner_id
        return bool(owner and owner.active and not owner.share
                    and self.company_id in owner.company_ids
                    and owner.with_user(owner).has_group(
                        'crm_meta_lead_ads.group_meta_lead_manager'))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            values = dict(values)
            if values.get('health_monitor_enabled') and not values.get('health_enabled_at'):
                values['health_enabled_at'] = fields.Datetime.now()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if 'health_monitor_enabled' in vals and 'health_enabled_at' not in vals:
            # An explicit false disables the silence clock. A later opt-in
            # starts a new observation window instead of using old history.
            for rec in self:
                if bool(vals['health_monitor_enabled']) != bool(rec.health_monitor_enabled):
                    super(MetaAccount, rec).write({
                        'health_enabled_at': fields.Datetime.now()
                        if vals['health_monitor_enabled'] else False,
                    })
        return super().write(vals)

    def _health_snapshot(self, now=None):
        """Local, account/company-scoped numbers and static issue codes only."""
        self.ensure_one()
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        result = {
            'level': 'disabled' if not self.health_monitor_enabled else 'unknown',
            'issues': [], 'checks': [], 'due_queue_count': 0,
            'oldest_due_minutes': 0, 'failed_queue_count': 0,
            'ambiguous_queue_count': 0, 'failed_outbound_24h_count': 0,
            'page_count': 0,
        }
        if not self.health_monitor_enabled:
            return result
        pages = self.env['meta.page'].sudo().search([
            ('account_id', '=', self.id), ('company_id', '=', self.company_id.id),
            ('active', '=', True), ('sync_enabled', '=', True),
        ])
        result['page_count'] = len(pages)
        page_ids = pages.ids
        checks = result['checks']

        def issue(code, page=False, count=1, severity='warning'):
            checks.append({'code': code, 'page_id': page.id if page else False,
                           'count': count, 'severity': severity})
            if code not in result['issues']:
                result['issues'].append(code)

        if self.state in ('error', 'disconnected'):
            issue('account_disconnected', severity='error')
        expiry = self.sudo().token_expires_at
        if expiry:
            if expiry <= now:
                issue('token_expired', severity='error')
            elif expiry <= now + timedelta(days=self.health_token_warning_days):
                issue('token_expiring')
        if self.diagnostic_checked_at and self.diagnostic_checked_at < now - timedelta(hours=24):
            issue('diagnostic_stale')
        elif self.diagnostic_checked_at and self.diagnostic_status == 'failure':
            issue('diagnostic_failed', severity='error')
        company_scope = [('company_id', '=', self.company_id.id),
                         ('page_id', 'in', page_ids)]
        if page_ids:
            Queue = self.env['meta.lead.queue'].sudo()
            # Aggregate in PostgreSQL: neither the cron nor the form should
            # load a large backlog into an ORM recordset. A retry's age starts
            # when it becomes due; processing age starts at last progress.
            self.env.cr.execute("""
                WITH due AS (
                    SELECT CASE
                        WHEN state = 'processing'
                            THEN GREATEST(received_at, COALESCE(write_date, received_at))
                        WHEN state = 'retry'
                            THEN GREATEST(received_at, COALESCE(next_retry_at, received_at))
                        ELSE received_at END AS due_since
                    FROM meta_lead_queue
                    WHERE company_id = %s AND page_id = ANY(%s)
                      AND (state = 'processing' OR
                           (state IN ('pending', 'retry') AND
                            (next_retry_at IS NULL OR next_retry_at <= %s)))
                )
                SELECT COUNT(*), MIN(due_since),
                       COUNT(*) FILTER (WHERE due_since <= %s)
                FROM due
            """, (self.company_id.id, page_ids, now,
                  now - timedelta(minutes=self.health_due_minutes)))
            due_count, oldest, overdue = self.env.cr.fetchone()
            result['due_queue_count'] = due_count
            if oldest:
                result['oldest_due_minutes'] = max(
                    0, int((now - oldest).total_seconds() / 60))
            if overdue:
                issue('queue_overdue', count=overdue)
            for state, key, code in (
                    ('failed', 'failed_queue_count', 'queue_failed'),
                    ('ambiguous', 'ambiguous_queue_count', 'queue_ambiguous')):
                count = Queue.search_count(company_scope + [('state', '=', state)])
                result[key] = count
                if count:
                    issue(code, count=count, severity='error')
            result['failed_outbound_24h_count'] = self.env['meta.message'].sudo().search_count(
                company_scope + [('direction', '=', 'outbound'),
                                 ('send_state', '=', 'failed'),
                                 ('received_at', '>=', now - timedelta(hours=24)),
                                 ('received_at', '<=', now)])
            if result['failed_outbound_24h_count']:
                issue('outbound_failed', count=result['failed_outbound_24h_count'])
        for page in pages:
            checked = page.subscription_checked_at
            if checked and checked < now - timedelta(hours=24):
                issue('subscription_stale', page)
            elif checked and page.subscription_status in ('failed', 'incomplete'):
                issue('subscription_' + page.subscription_status, page)
            if self.health_silence_minutes:
                baseline = page.last_live_webhook_at or self.health_enabled_at
                if baseline and baseline <= now - timedelta(minutes=self.health_silence_minutes):
                    issue('webhook_silence', page)
        if any(item['severity'] == 'error' for item in checks):
            result['level'] = 'error'
        elif checks:
            result['level'] = 'warning'
        elif page_ids and self.diagnostic_status == 'success' and pages.filtered(
                lambda p: p.last_live_webhook_at):
            result['level'] = 'ok'
        return result

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
