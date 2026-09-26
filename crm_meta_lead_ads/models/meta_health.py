"""Local Meta connector incidents; no Graph calls or customer data."""

import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


CHECK_LABELS = {
    'account_disconnected': 'Account connection needs attention',
    'token_expired': 'Known token expiry',
    'token_expiring': 'Known token expiry approaching',
    'diagnostic_stale': 'Diagnostics older than 24 hours',
    'diagnostic_failed': 'Latest diagnostics reported a failure',
    'subscription_stale': 'Subscription check older than 24 hours',
    'subscription_failed': 'Latest subscription check failed',
    'subscription_incomplete': 'Latest subscription check incomplete',
    'queue_overdue': 'Due ingestion queue is overdue',
    'queue_failed': 'Failed ingestion records require review',
    'queue_ambiguous': 'Ambiguous ingestion records require review',
    'outbound_failed': 'Outbound messages recorded as failed',
    'webhook_silence': 'No recent webhook observed; verify expected traffic',
}

_logger = logging.getLogger(__name__)


class MetaHealthAlert(models.Model):
    _name = 'meta.health.alert'
    _description = 'Meta Connector Health Incident'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'last_seen_at desc, id desc'

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one('res.company', required=True, index=True, readonly=True)
    account_id = fields.Many2one('meta.account', required=True, index=True,
                                 ondelete='cascade', readonly=True)
    page_id = fields.Many2one('meta.page', index=True, ondelete='cascade', readonly=True)
    # SQL UNIQUE treats NULL as distinct; 0 is the account-level page key.
    page_key = fields.Integer(default=0, required=True, readonly=True)
    check_code = fields.Char(required=True, readonly=True)
    severity = fields.Selection([('warning', 'Warning'), ('error', 'Error')],
                                required=True, readonly=True)
    state = fields.Selection([('open', 'Open'), ('acknowledged', 'Acknowledged'),
                              ('resolved', 'Resolved')], default='open', required=True,
                             tracking=True)
    count = fields.Integer(readonly=True)
    first_seen_at = fields.Datetime(required=True, readonly=True)
    last_seen_at = fields.Datetime(required=True, readonly=True)
    resolved_at = fields.Datetime(readonly=True)

    _unique_incident = models.Constraint(
        'UNIQUE(account_id, page_key, check_code)',
        'This Meta health incident is already recorded.')

    @api.constrains('company_id', 'account_id', 'page_id', 'page_key')
    def _check_scope(self):
        for alert in self:
            if (alert.company_id != alert.account_id.company_id
                    or (alert.page_id and (
                        alert.page_id.account_id != alert.account_id
                        or alert.page_id.company_id != alert.company_id))
                    or alert.page_key != (alert.page_id.id or 0)):
                raise ValidationError(_('Invalid Meta health incident scope.'))

    def _check_manager(self):
        if not self.env.user.has_group('crm_meta_lead_ads.group_meta_lead_manager'):
            raise UserError(_('Only Meta Lead Ads managers may change health incidents.'))
        for alert in self:
            if alert.company_id not in self.env.companies:
                raise UserError(_('This incident is outside your allowed companies.'))

    def action_acknowledge(self):
        self._check_manager()
        self.filtered(lambda a: a.state == 'open').write({'state': 'acknowledged'})
        return True

    def action_resolve(self):
        self._check_manager()
        self._resolve(fields.Datetime.now())
        return True

    def _dedicated_activities(self):
        self.ensure_one()
        return self.env['mail.activity'].sudo().search([
            ('res_model_id', '=', self.env['ir.model']._get_id(self._name)),
            ('res_id', '=', self.id),
            ('activity_type_id', '=', self.env.ref(
                'crm_meta_lead_ads.mail_activity_type_meta_health').id),
        ])

    def _notify_owner_once(self):
        self.ensure_one()
        account = self.account_id
        activities = self._dedicated_activities()
        if not account._health_owner_eligible():
            activities.unlink()
            return
        if activities:
            if activities[:1].user_id != account.health_owner_id:
                activities[:1].write({'user_id': account.health_owner_id.id})
            return
        self.env['mail.activity'].sudo().create({
            'res_model_id': self.env['ir.model']._get_id(self._name),
            'res_id': self.id,
            'activity_type_id': self.env.ref(
                'crm_meta_lead_ads.mail_activity_type_meta_health').id,
            'user_id': account.health_owner_id.id,
            'summary': self.name,
            'date_deadline': fields.Date.today(),
        })

    def _resolve(self, at):
        for alert in self.filtered(lambda a: a.state != 'resolved'):
            alert.write({'state': 'resolved', 'resolved_at': at})
            # Complete only the dedicated activity tied to this incident.
            alert._dedicated_activities().action_feedback(
                feedback=_('Meta health condition cleared.'))

    @api.model
    def _upsert_check(self, account, check, now):
        page_id = check['page_id'] or False
        domain = [('account_id', '=', account.id), ('page_key', '=', page_id or 0),
                  ('check_code', '=', check['code']),
                  ('company_id', '=', account.company_id.id)]
        alert = self.sudo().search(domain, limit=1)
        if not alert:
            values = {
                'name': CHECK_LABELS[check['code']],
                'company_id': account.company_id.id, 'account_id': account.id,
                'page_id': page_id, 'page_key': page_id or 0,
                'check_code': check['code'], 'severity': check['severity'],
                'count': check['count'], 'first_seen_at': now,
                'last_seen_at': now,
            }
            try:
                with self.env.cr.savepoint():
                    alert = self.sudo().create(values)
            except IntegrityError:
                alert = self.sudo().search(domain, limit=1)
                if not alert:
                    raise
        reopened = alert.state == 'resolved'
        values = {'last_seen_at': now, 'count': check['count'],
                  'severity': check['severity']}
        if reopened:
            values.update({'state': 'open', 'first_seen_at': now,
                           'resolved_at': False})
        alert.sudo().write(values)
        if not account._health_owner_eligible():
            alert._dedicated_activities().unlink()
        if alert.state == 'open':
            alert._notify_owner_once()
        return alert


class MetaAccountHealth(models.Model):
    _inherit = 'meta.account'

    health_alert_ids = fields.One2many('meta.health.alert', 'account_id',
                                       readonly=True)
    health_owner_eligible = fields.Boolean(compute='_compute_health_screen',
                                          string='Eligible alert owner')
    health_level = fields.Selection([
        ('disabled', 'Disabled'), ('unknown', 'Unknown'),
        ('ok', 'No detected issues'), ('warning', 'Warning'),
        ('error', 'Error')], compute='_compute_health_screen')
    health_due_queue_count = fields.Integer(compute='_compute_health_screen')
    health_oldest_due_minutes = fields.Integer(compute='_compute_health_screen')
    health_failed_queue_count = fields.Integer(compute='_compute_health_screen')
    health_ambiguous_queue_count = fields.Integer(compute='_compute_health_screen')
    health_failed_outbound_count = fields.Integer(compute='_compute_health_screen')
    health_last_sync_state = fields.Char(compute='_compute_health_screen')
    health_last_sync_at = fields.Datetime(compute='_compute_health_screen')

    def _compute_health_screen(self):
        now = fields.Datetime.now()
        Run = self.env['meta.sync.run'].sudo()
        for account in self:
            snapshot = account._health_snapshot(now)
            account.health_owner_eligible = account._health_owner_eligible()
            account.health_level = snapshot['level']
            account.health_due_queue_count = snapshot['due_queue_count']
            account.health_oldest_due_minutes = snapshot['oldest_due_minutes']
            account.health_failed_queue_count = snapshot['failed_queue_count']
            account.health_ambiguous_queue_count = snapshot['ambiguous_queue_count']
            account.health_failed_outbound_count = snapshot['failed_outbound_24h_count']
            last = Run.search([('account_id', '=', account.id),
                               ('company_id', '=', account.company_id.id)],
                              order='id desc', limit=1)
            account.health_last_sync_state = last.state or False
            account.health_last_sync_at = last.write_date or False

    def _health_drilldown(self, model, title, page=False):
        self.ensure_one()
        if not self.env.user.has_group('crm_meta_lead_ads.group_meta_lead_manager'):
            raise UserError(_('Only Meta Lead Ads managers may inspect health details.'))
        if self.company_id not in self.env.companies:
            raise UserError(_('This account is outside your allowed companies.'))
        if page and (page.account_id != self or page.company_id != self.company_id):
            raise UserError(_('The selected page does not belong to this account.'))
        domain = [('company_id', '=', self.company_id.id),
                  ('page_id.account_id', '=', self.id)]
        if page:
            domain.append(('page_id', '=', page.id))
        else:
            domain.append(('page_id', 'in', self.page_ids.ids))
        return {'type': 'ir.actions.act_window', 'name': title,
                'res_model': model, 'view_mode': 'list,form', 'domain': domain}

    def action_health_queue(self, page=False):
        return self._health_drilldown('meta.lead.queue', _('Meta Ingestion Queue'), page)

    def action_health_messages(self, page=False):
        return self._health_drilldown('meta.message', _('Meta Messages'), page)

    def _reconcile_health(self, snapshot, now):
        self.ensure_one()
        Alert = self.env['meta.health.alert'].sudo()
        current = set()
        for check in snapshot['checks']:
            current.add((check['page_id'] or 0, check['code']))
            Alert._upsert_check(self, check, now)
        existing = Alert.search([('account_id', '=', self.id),
                                 ('company_id', '=', self.company_id.id),
                                 ('state', '!=', 'resolved')])
        for alert in existing:
            if (alert.page_key, alert.check_code) not in current:
                alert._resolve(now)

    @api.model
    def _cron_assess_health(self, limit=25, now=None):
        """Bounded, opt-in, local-only pass; account row lock serializes runs."""
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        if self:
            accounts = self.sudo().filtered('health_monitor_enabled')[:limit]
        else:
            accounts = self.sudo().search([('health_monitor_enabled', '=', True)],
                                          order='health_checked_at asc, id asc',
                                          limit=limit)
        for account in accounts:
            try:
                with self.env.cr.savepoint():
                    self.env.cr.execute(
                        'SELECT id FROM meta_account WHERE id = %s FOR UPDATE',
                        (account.id,))
                    account.invalidate_recordset()
                    account._reconcile_health(account._health_snapshot(now), now)
                    account.write({'health_checked_at': now})
            except Exception:
                # Keep other accounts moving; do not put Graph errors, tokens
                # or customer data in logs from this local-only monitor.
                _logger.warning('Meta health assessment skipped for account id=%s',
                                account.id)
                continue
        return True
