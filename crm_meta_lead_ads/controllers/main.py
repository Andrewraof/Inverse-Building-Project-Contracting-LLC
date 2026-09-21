import hashlib
import hmac
import logging
import secrets
from urllib.parse import urlencode
import requests
from werkzeug.utils import redirect
from odoo import http, fields, _
from odoo.http import request

_logger = logging.getLogger(__name__)


class MetaLeadController(http.Controller):

    def _param(self, key, default=False):
        return request.env['ir.config_parameter'].sudo().get_param(key, default)

    @http.route('/meta_crm/connect', type='http', auth='user', methods=['GET'], csrf=False)
    def connect(self, **kw):
        app_id = self._param('crm_meta_lead_ads.app_id')
        if not app_id:
            return request.not_found()
        state = secrets.token_urlsafe(32)
        request.session['meta_oauth_state'] = state
        base_url = self._param('web.base.url').rstrip('/')
        callback = f'{base_url}/meta_crm/oauth_callback'
        scopes = ','.join([
            'leads_retrieval', 'pages_show_list', 'pages_read_engagement',
            'pages_manage_ads', 'pages_manage_metadata',
        ])
        params = {'client_id': app_id, 'redirect_uri': callback, 'state': state, 'scope': scopes, 'response_type': 'code'}
        return redirect('https://www.facebook.com/dialog/oauth?' + urlencode(params))

    @http.route('/meta_crm/oauth_callback', type='http', auth='user', methods=['GET'], csrf=False)
    def oauth_callback(self, code=None, state=None, error=None, error_description=None, **kw):
        if error:
            return request.make_response(f'Meta authorization failed: {error_description or error}', status=400)
        expected = request.session.pop('meta_oauth_state', None)
        if not expected or not state or not hmac.compare_digest(expected, state):
            return request.make_response('Invalid OAuth state.', status=403)
        if not code:
            return request.make_response('Missing OAuth authorization code.', status=400)

        ICP = request.env['ir.config_parameter'].sudo()
        app_id = ICP.get_param('crm_meta_lead_ads.app_id')
        app_secret = ICP.get_param('crm_meta_lead_ads.app_secret')
        api_version = ICP.get_param('crm_meta_lead_ads.api_version', 'v25.0')
        base_url = ICP.get_param('web.base.url').rstrip('/')
        callback = f'{base_url}/meta_crm/oauth_callback'
        timeout = int(ICP.get_param('crm_meta_lead_ads.http_timeout', '15'))

        token_url = f'https://graph.facebook.com/{api_version}/oauth/access_token'
        r = requests.get(token_url, params={'client_id': app_id, 'client_secret': app_secret, 'redirect_uri': callback, 'code': code}, timeout=timeout)
        short = r.json()
        if r.status_code >= 400 or short.get('error'):
            return request.make_response('Meta token exchange failed.', status=400)
        short_token = short.get('access_token')

        r = requests.get(token_url, params={'grant_type': 'fb_exchange_token', 'client_id': app_id, 'client_secret': app_secret, 'fb_exchange_token': short_token}, timeout=timeout)
        long_data = r.json()
        if r.status_code >= 400 or long_data.get('error'):
            return request.make_response('Meta long-lived token exchange failed.', status=400)
        user_token = long_data.get('access_token')
        expires_in = long_data.get('expires_in')

        me = requests.get(f'https://graph.facebook.com/{api_version}/me', params={'access_token': user_token, 'fields': 'id,name'}, timeout=timeout).json()
        pages_resp = requests.get(f'https://graph.facebook.com/{api_version}/me/accounts', params={'access_token': user_token, 'fields': 'id,name,access_token', 'limit': 100}, timeout=timeout)
        pages_data = pages_resp.json()
        if pages_resp.status_code >= 400 or pages_data.get('error'):
            return request.make_response('Unable to retrieve managed Pages from Meta.', status=400)

        company = request.env.company
        Account = request.env['meta.account'].sudo().with_company(company)
        account = Account.search([('meta_user_id', '=', str(me.get('id'))), ('company_id', '=', company.id)], limit=1)
        vals = {
            'name': me.get('name') or _('Meta Account'), 'meta_user_id': str(me.get('id') or ''), 'company_id': company.id,
            'app_id': app_id, 'app_secret': app_secret, 'user_access_token': user_token, 'state': 'connected',
            'error_message': False, 'last_connected_at': fields.Datetime.now(),
        }
        if expires_in:
            vals['token_expires_at'] = fields.Datetime.add(fields.Datetime.now(), seconds=int(expires_in))
        account.write(vals) if account else None
        if not account:
            account = Account.create(vals)

        Page = request.env['meta.page'].sudo().with_company(company)
        for p in pages_data.get('data', []):
            if not p.get('id') or not p.get('access_token'):
                continue
            page = Page.search([('meta_page_id', '=', str(p['id'])), ('company_id', '=', company.id)], limit=1)
            pvals = {'name': p.get('name') or p['id'], 'company_id': company.id, 'account_id': account.id,
                     'meta_page_id': str(p['id']), 'page_access_token': p['access_token'], 'active': True, 'sync_enabled': True}
            page.write(pvals) if page else Page.create(pvals)
        return redirect('/web#action=crm_meta_lead_ads.action_meta_page')

    @http.route('/meta_crm/webhook', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def webhook_verify(self, **kw):
        mode = request.httprequest.args.get('hub.mode')
        token = request.httprequest.args.get('hub.verify_token') or ''
        challenge = request.httprequest.args.get('hub.challenge') or ''
        expected = self._param('crm_meta_lead_ads.verify_token', '') or ''
        if mode == 'subscribe' and expected and hmac.compare_digest(token, expected):
            return request.make_response(challenge, headers=[('Content-Type', 'text/plain')], status=200)
        return request.make_response('Forbidden', status=403)

    def _receive_webhook(self, secret, account=None):
        raw = request.httprequest.get_data(cache=True) or b''
        signature = request.httprequest.headers.get('X-Hub-Signature-256', '')
        if not secret or not signature.startswith('sha256='):
            return request.make_response('Forbidden', status=403)
        expected = 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return request.make_response('Forbidden', status=403)
        payload = request.httprequest.get_json(silent=True) or {}
        if payload.get('object') != 'page':
            return request.make_response('EVENT_RECEIVED', status=200)

        Page = request.env['meta.page'].sudo()
        Queue = request.env['meta.lead.queue'].sudo()
        for entry in payload.get('entry', []):
            entry_page_id = str(entry.get('id') or '')
            for change in entry.get('changes', []):
                if change.get('field') != 'leadgen':
                    continue
                value = change.get('value') or {}
                leadgen_id = value.get('leadgen_id')
                page_meta_id = str(value.get('page_id') or entry_page_id)
                if not leadgen_id or not page_meta_id:
                    continue
                domain = [('meta_page_id', '=', page_meta_id), ('active', '=', True), ('sync_enabled', '=', True)]
                if account:
                    domain.append(('account_id', '=', account.id))
                pages = Page.search(domain)
                for page in pages:
                    Queue.enqueue_event(page.company_id, page, leadgen_id, value.get('form_id'), payload)
        return request.make_response('EVENT_RECEIVED', status=200)

    @http.route('/meta_crm/webhook', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def webhook_receive(self, **kw):
        return self._receive_webhook(self._param('crm_meta_lead_ads.app_secret', '') or '')

    @http.route('/meta_crm/webhook/<string:webhook_key>', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def webhook_verify_account(self, webhook_key, **kw):
        account = request.env['meta.account'].sudo().search([('webhook_key', '=', webhook_key)], limit=1)
        if not account:
            return request.make_response('Forbidden', status=403)
        mode = request.httprequest.args.get('hub.mode')
        token = request.httprequest.args.get('hub.verify_token') or ''
        challenge = request.httprequest.args.get('hub.challenge') or ''
        expected = self._param('crm_meta_lead_ads.verify_token', '') or ''
        if mode == 'subscribe' and expected and hmac.compare_digest(token, expected):
            return request.make_response(challenge, headers=[('Content-Type', 'text/plain')], status=200)
        return request.make_response('Forbidden', status=403)

    @http.route('/meta_crm/webhook/<string:webhook_key>', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def webhook_receive_account(self, webhook_key, **kw):
        account = request.env['meta.account'].sudo().search([('webhook_key', '=', webhook_key)], limit=1)
        if not account:
            return request.make_response('Forbidden', status=403)
        return self._receive_webhook(account.app_secret, account=account)
