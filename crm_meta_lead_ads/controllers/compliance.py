import base64
import hashlib
import hmac
import json
import uuid
from odoo import http, fields
from odoo.http import request


class MetaComplianceController(http.Controller):

    def _decode_signed_request(self, signed_request, secret):
        try:
            encoded_sig, payload = signed_request.split('.', 1)
            pad = lambda s: s + '=' * (-len(s) % 4)
            sig = base64.urlsafe_b64decode(pad(encoded_sig))
            data_raw = base64.urlsafe_b64decode(pad(payload))
            expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(sig, expected):
                return None
            data = json.loads(data_raw.decode())
            if str(data.get('algorithm', '')).upper() != 'HMAC-SHA256':
                return None
            return data
        except Exception:
            return None

    @http.route('/meta_crm/data_deletion', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def data_deletion(self, **kw):
        ICP = request.env['ir.config_parameter'].sudo()
        secret = ICP.get_param('crm_meta_lead_ads.app_secret', '')
        signed = request.httprequest.form.get('signed_request') or ''
        data = self._decode_signed_request(signed, secret) if secret else None
        if not data or not data.get('user_id'):
            return request.make_json_response({'error': 'invalid_request'}, status=400)
        confirmation = uuid.uuid4().hex
        user_id = str(data['user_id'])
        account = request.env['meta.account'].sudo().search([('meta_user_id', '=', user_id)])
        account.action_disconnect()
        request.env['meta.lead.log'].sudo().create({
            'company_id': account[:1].company_id.id if account else request.env.company.id,
            'level': 'warning', 'action': 'data_deletion_request',
            'message': f'Meta user {user_id}; confirmation {confirmation}',
            'payload_json': {'user_id': user_id, 'confirmation_code': confirmation, 'received_at': fields.Datetime.now().isoformat()},
        })
        base = ICP.get_param('crm_meta_lead_ads.deletion_status_base_url') or ICP.get_param('web.base.url')
        return request.make_json_response({'url': f"{base.rstrip('/')}/meta_crm/data_deletion/status/{confirmation}", 'confirmation_code': confirmation})

    @http.route('/meta_crm/data_deletion/status/<string:confirmation>', type='http', auth='public', methods=['GET'], csrf=False, save_session=False)
    def deletion_status(self, confirmation, **kw):
        log = request.env['meta.lead.log'].sudo().search([('action','=','data_deletion_request'), ('message','ilike', confirmation)], limit=1)
        if not log:
            return request.make_json_response({'status': 'not_found'}, status=404)
        return request.make_json_response({'status': 'completed', 'confirmation_code': confirmation})
