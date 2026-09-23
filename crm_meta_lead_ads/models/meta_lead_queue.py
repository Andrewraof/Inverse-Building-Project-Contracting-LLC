import logging
import random
from datetime import timedelta
from psycopg2 import IntegrityError
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class MetaLeadQueue(models.Model):
    _name = 'meta.lead.queue'
    _description = 'Meta Lead Ingestion Queue'
    _order = 'priority desc, id'

    company_id = fields.Many2one('res.company', required=True, index=True)
    page_id = fields.Many2one('meta.page', required=True, ondelete='cascade', index=True)
    form_id = fields.Many2one('meta.form', ondelete='set null', index=True)
    meta_lead_id = fields.Char(required=True, index=True)
    raw_webhook = fields.Json(readonly=True)
    fetched_payload = fields.Json(readonly=True)
    state = fields.Selection([('pending','Pending'),('processing','Processing'),('done','Done'),('duplicate','Duplicate'),('retry','Retry'),('failed','Failed')], default='pending', required=True, index=True)
    priority = fields.Integer(default=10)
    attempts = fields.Integer(default=0)
    next_retry_at = fields.Datetime(index=True)
    error_message = fields.Text(readonly=True)
    crm_lead_id = fields.Many2one('crm.lead', readonly=True, index=True)
    received_at = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    processed_at = fields.Datetime(readonly=True)

    _unique_queued_lead = models.Constraint('UNIQUE(meta_lead_id, company_id)', 'This Meta lead is already queued for this company.')

    @api.model
    def enqueue_event(self, company, page, leadgen_id, form_id=None, payload=None):
        vals = {'company_id': company.id, 'page_id': page.id, 'meta_lead_id': str(leadgen_id), 'raw_webhook': payload or {}}
        if form_id:
            form = self.env['meta.form'].sudo().search([('meta_form_id', '=', str(form_id)), ('company_id', '=', company.id)], limit=1)
            vals['form_id'] = form.id if form else False
        try:
            with self.env.cr.savepoint():
                return self.sudo().create(vals)
        except IntegrityError:
            return self.sudo().search([('meta_lead_id', '=', str(leadgen_id)), ('company_id', '=', company.id)], limit=1)

    def _log(self, level, action, message='', payload=None):
        self.ensure_one()
        self.env['meta.lead.log'].sudo().create({
            'company_id': self.company_id.id, 'queue_id': self.id, 'meta_lead_id': self.meta_lead_id,
            'level': level, 'action': action, 'message': message, 'payload_json': payload or {},
        })

    def _fetch_lead(self):
        self.ensure_one()
        # page_id/adgroup_id were removed from the leadgen node; valid fields
        # on current Graph versions are adset_id/campaign_id instead.
        fields_list = 'id,created_time,ad_id,adset_id,campaign_id,form_id,field_data,platform,is_organic'
        return self.page_id.account_id._request('GET', self.meta_lead_id, token=self.page_id.page_access_token, params={'fields': fields_list})

    def _mapping_values(self, payload):
        self.ensure_one()
        form = self.form_id or self.env['meta.form'].search([('meta_form_id', '=', str(payload.get('form_id'))), ('company_id', '=', self.company_id.id)], limit=1)
        data = {x.get('name'): x.get('values', []) for x in payload.get('field_data', []) if x.get('name')}
        vals = {}
        if form:
            for m in form.mapping_ids.filtered('active'):
                values = data.get(m.meta_field_name, [])
                if not values:
                    continue
                field = self.env['crm.lead']._fields.get(m.odoo_field_name)
                value = (m.join_separator or ', ').join(str(v) for v in values)
                if field and field.type in ('char', 'text', 'html', 'selection'):
                    vals[m.odoo_field_name] = value
        if not vals.get('contact_name') and data.get('full_name'):
            vals['contact_name'] = ' '.join(data['full_name'])
        if not vals.get('email_from') and data.get('email'):
            vals['email_from'] = data['email'][0]
        if not vals.get('phone') and data.get('phone_number'):
            vals['phone'] = data['phone_number'][0]
        return vals, form

    def _resolve_source(self, platform):
        xmlid = 'crm_meta_lead_ads.utm_source_instagram' if platform == 'instagram' else 'crm_meta_lead_ads.utm_source_facebook'
        return self.env.ref(xmlid, raise_if_not_found=False)

    def _create_crm_lead(self, payload):
        self.ensure_one()
        existing = self.env['crm.lead'].sudo().search([('meta_lead_id', '=', self.meta_lead_id)], limit=1)
        if existing:
            return existing, True
        mapped, form = self._mapping_values(payload)
        platform = (payload.get('platform') or 'unknown').lower()
        if platform not in ('facebook', 'instagram'):
            platform = 'unknown'
        source = self._resolve_source(platform)
        name = mapped.pop('name', False) or mapped.get('contact_name') or mapped.get('email_from') or _('Meta Lead %s') % self.meta_lead_id
        vals = {
            **mapped, 'name': name, 'company_id': self.company_id.id, 'meta_lead_id': self.meta_lead_id,
            'meta_form_id': form.id if form else False, 'meta_page_id': self.page_id.id,
            'meta_platform': platform, 'meta_ad_id': payload.get('ad_id'), 'meta_adgroup_id': payload.get('adset_id'),
            'meta_is_organic': bool(payload.get('is_organic')), 'meta_raw_payload': payload,
            'team_id': form.sales_team_id.id if form and form.sales_team_id else False,
            'user_id': form.user_id.id if form and form.user_id else False,
            'type': form.lead_type if form else 'lead', 'source_id': source.id if source else False,
        }
        # conservative partner matching within same company/allowed global contacts
        domain = []
        if vals.get('email_from'):
            domain = [('email', '=ilike', vals['email_from'])]
        elif vals.get('phone'):
            domain = [('phone', '=', vals['phone'])]
        if domain:
            partner = self.env['res.partner'].sudo().search(domain + ['|', ('company_id','=',False), ('company_id','=',self.company_id.id)], limit=1)
            if partner:
                vals['partner_id'] = partner.id
        try:
            with self.env.cr.savepoint():
                return self.env['crm.lead'].sudo().with_company(self.company_id).create(vals), False
        except IntegrityError:
            return self.env['crm.lead'].sudo().search([('meta_lead_id', '=', self.meta_lead_id)], limit=1), True

    def _schedule_retry(self, message, fatal=False):
        self.ensure_one()
        max_attempts = int(self.env['ir.config_parameter'].sudo().get_param('crm_meta_lead_ads.max_attempts', '8'))
        attempts = self.attempts + 1
        if fatal or attempts >= max_attempts:
            self.write({'state': 'failed', 'attempts': attempts, 'error_message': message, 'processed_at': fields.Datetime.now()})
            self._log('error', 'failed', message)
            return
        delay = min(2 ** attempts, 60) + random.randint(0, 3)
        self.write({'state': 'retry', 'attempts': attempts, 'error_message': message, 'next_retry_at': fields.Datetime.now() + timedelta(minutes=delay)})
        self._log('warning', 'retry_scheduled', message)

    def process_one(self):
        self.ensure_one()
        if self.state not in ('pending', 'retry', 'failed'):
            return
        self.state = 'processing'
        try:
            payload = self._fetch_lead()
            self.fetched_payload = payload
            lead, duplicate = self._create_crm_lead(payload)
            self.write({'state': 'duplicate' if duplicate else 'done', 'crm_lead_id': lead.id, 'processed_at': fields.Datetime.now(), 'error_message': False})
            self._log('info', 'duplicate' if duplicate else 'created', f'CRM lead {lead.id}', payload)
        except Exception as exc:
            _logger.exception('Meta lead processing failed for %s', self.meta_lead_id)
            fatal = self.page_id.account_id.state == 'error'
            self._schedule_retry(str(exc), fatal=fatal)

    @api.model
    def _cron_process_queue(self, limit=50):
        now = fields.Datetime.now()
        batch = self.sudo().search([
            ('state', 'in', ['pending', 'retry']), '|', ('next_retry_at', '=', False), ('next_retry_at', '<=', now)
        ], order='priority desc,id', limit=limit)
        for rec in batch:
            rec.process_one()
        return True

    @api.model
    def _cron_poll_meta_leads(self):
        forms = self.env['meta.form'].sudo().search([('active','=',True),('polling_enabled','=',True),('page_id.sync_enabled','=',True)])
        for form in forms:
            page = form.page_id
            params = {'fields': 'id,created_time', 'limit': 100}
            if form.last_sync_date:
                params['since'] = int(form.last_sync_date.timestamp())
            try:
                data = page.account_id._request('GET', f'{form.meta_form_id}/leads', token=page.page_access_token, params=params)
                for item in data.get('data', []):
                    self.enqueue_event(form.company_id, page, item['id'], form.meta_form_id, {'recovery_poll': True})
                form.last_sync_date = fields.Datetime.now()
            except Exception as exc:
                self.env['meta.lead.log'].sudo().create({'company_id': form.company_id.id, 'level': 'error', 'action': 'poll_failed', 'message': str(exc)})
        return True
