import logging

from psycopg2 import IntegrityError

from odoo import fields, models, _
from odoo.exceptions import UserError

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
    page_access_token = fields.Text(required=True, groups='base.group_system', copy=False)
    page_permissions = fields.Char(readonly=True, copy=False)
    sync_enabled = fields.Boolean(default=True, tracking=True)
    subscribed = fields.Boolean(default=False, readonly=True)
    form_ids = fields.One2many('meta.form', 'page_id')
    last_sync_at = fields.Datetime(readonly=True)

    _unique_page_company = models.Constraint('UNIQUE(meta_page_id, company_id)', 'This Meta Page is already configured for this company.')

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
            'page_access_token': data.get('access_token'),
            'page_permissions': ','.join(tasks) if tasks else False,
            'active': True,
            'sync_enabled': True,
        }
        page = self.search([('meta_page_id', '=', meta_page_id), ('company_id', '=', company.id)], limit=1)
        if page:
            page.write(vals)
            return page
        try:
            with self.env.cr.savepoint():
                return self.create(vals)
        except IntegrityError:
            page = self.search([('meta_page_id', '=', meta_page_id), ('company_id', '=', company.id)], limit=1)
            if page:
                page.write(vals)
                return page
            raise

    def action_subscribe_webhook(self):
        for rec in self:
            data = rec.account_id._request('POST', f'{rec.meta_page_id}/subscribed_apps', token=rec.page_access_token,
                                           data={'subscribed_fields': 'leadgen'})
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
