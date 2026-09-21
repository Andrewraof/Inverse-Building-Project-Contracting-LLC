import logging
import uuid
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


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
            raise UserError(_('Meta API request failed: %s') % exc) from exc
        if response.status_code >= 400 or (isinstance(data, dict) and data.get('error')):
            err = data.get('error', {}) if isinstance(data, dict) else {}
            self._handle_graph_error(err)
            raise UserError(_('Meta API error: %s') % (err.get('message') or response.text))
        return data

    def _handle_graph_error(self, err):
        self.ensure_one()
        code = err.get('code')
        subcode = err.get('error_subcode')
        msg = err.get('message') or ''
        if code == 190:
            self.write({'state': 'error', 'error_message': f'{code}/{subcode}: {msg}'})
            self.activity_schedule('mail.mail_activity_data_todo', summary=_('Meta token requires re-authentication'), note=msg)
        elif code in (200, 368):
            self.write({'state': 'error', 'error_message': f'{code}: {msg}'})

    def action_disconnect(self):
        for rec in self:
            rec.page_ids.write({'active': False, 'sync_enabled': False})
            rec.write({'state': 'disconnected', 'user_access_token': False, 'error_message': False})
        return True
