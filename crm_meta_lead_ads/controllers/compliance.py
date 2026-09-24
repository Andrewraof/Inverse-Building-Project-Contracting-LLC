import base64
import hashlib
import hmac
import json
import uuid
from odoo import http, fields
from odoo.http import request


PRIVACY_POLICY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Privacy Policy - Inverse Group</title>
  <style>
    body { font-family: Arial, sans-serif; line-height: 1.65; color: #24313d; margin: 0; background: #f6f7f9; }
    main { max-width: 820px; margin: 40px auto; padding: 36px; background: #fff; border-radius: 12px; }
    h1, h2 { color: #14213d; } h1 { margin-top: 0; }
    a { color: #1264a3; } footer { margin-top: 32px; color: #5d6873; font-size: 14px; }
  </style>
</head>
<body><main>
  <h1>Privacy Policy - Inverse Group</h1>
  <p><strong>Effective date:</strong> September 21, 2026</p>
  <p>Inverse Group respects your privacy. This policy explains how information submitted through our Meta lead forms and messaging channels is handled in our Odoo CRM system.</p>
  <h2>Information we collect</h2>
  <p>We may collect your name, phone number, email address, and the answers or messages you choose to submit through Meta lead forms or Meta messaging services.</p>
  <h2>How we use information</h2>
  <p>We use this information to contact you, respond to your request, follow up on your enquiry, and manage prospective customer relationships in Odoo CRM.</p>
  <h2>Sharing</h2>
  <p>We do not sell personal information. We may share it only with Meta, Odoo, and service providers necessary to operate our business and deliver the requested service, subject to appropriate safeguards.</p>
  <h2>Retention and security</h2>
  <p>We retain personal information only for a reasonable period needed for the purposes above and apply reasonable administrative and technical safeguards to protect it.</p>
  <h2>Your rights</h2>
  <p>You may request access to, correction of, or deletion of your personal information by contacting us through our <a href="/contactus">Contact Us page</a>.</p>
  <footer>Inverse Building Projects Contracting LLC</footer>
</main></body></html>"""


class MetaComplianceController(http.Controller):

    @http.route('/meta_crm/privacy', type='http', auth='public', methods=['GET'],
                csrf=False, save_session=False)
    def privacy_policy(self, **kw):
        return request.make_response(
            PRIVACY_POLICY_HTML,
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

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
