import logging
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# A recorded subscription failure older than this is downgraded to a
# stale-check warning instead of being asserted as a current failure.
STALE_CHECK_HOURS = 24
# Outbound failures are reported over this trailing window.
OUTBOUND_FAILURE_WINDOW_HOURS = 24
# Bounded cron batch: at most this many enabled accounts per pass.
MONITOR_BATCH_LIMIT = 25

CHECK_CODES = [
    ('account_error', 'Account error/disconnected'),
    ('token_expired', 'Token expired'),
    ('token_expiring', 'Token expiring soon'),
    ('subscription_failed', 'Page subscription failed/incomplete'),
    ('subscription_stale', 'Page subscription check is stale'),
    ('queue_backlog', 'Overdue queue backlog'),
    ('queue_stuck', 'Queue records stuck in processing'),
    ('queue_failures', 'Failed/ambiguous queue records'),
    ('outbound_failed', 'Recorded outbound failures (24h)'),
    ('silence', 'No recent webhook observed'),
]


class MetaHealthAlert(models.Model):
    """One stored incident per (account, page, check code).

    States: open → acknowledged → resolved. A recurring condition reopens
    the resolved record (new episode) instead of creating a second row.
    The unique ``alert_key`` is the hard guarantee against duplicates:
    under Odoo's REPEATABLE READ isolation, a truly concurrent competing
    insert surfaces as a serialization failure (40001) that propagates to
    the caller, and convergence happens in a FRESH transaction (the next
    cron pass pre-checks and reuses the competitor's row) — an
    in-transaction re-read cannot see a competitor row committed after
    this transaction's snapshot. Only state changes post chatter — an
    unchanged cron pass writes timestamps and counters silently.
    Notifications use the dedicated Meta Health activity type only; no
    external contact is ever subscribed.
    """
    _name = 'meta.health.alert'
    _description = 'Meta Connector Health Alert'
    _inherit = ['mail.thread']
    _order = 'last_seen_at desc, id desc'

    name = fields.Char(readonly=True)
    company_id = fields.Many2one('res.company', required=True, index=True)
    account_id = fields.Many2one('meta.account', required=True, ondelete='cascade', index=True)
    page_id = fields.Many2one('meta.page', ondelete='cascade', index=True)
    check_code = fields.Selection(CHECK_CODES, required=True, readonly=True, index=True)
    alert_key = fields.Char(required=True, readonly=True, index=True, copy=False)
    severity = fields.Selection([('warning', 'Warning'), ('error', 'Error')],
                                required=True, readonly=True)
    state = fields.Selection([
        ('open', 'Open'), ('acknowledged', 'Acknowledged'), ('resolved', 'Resolved'),
    ], default='open', required=True, tracking=True, index=True)
    first_seen_at = fields.Datetime(readonly=True)
    last_seen_at = fields.Datetime(readonly=True)
    resolved_at = fields.Datetime(readonly=True)
    summary_count = fields.Integer(readonly=True,
                                   help='Numeric summary (e.g. affected record count).')
    detail = fields.Char(readonly=True,
                         help='Static, secret-sanitized message. Never tokens or customer data.')
    notification_state = fields.Selection([
        ('pending', 'Pending'), ('notified', 'Notified'), ('no_recipient', 'No eligible recipient'),
    ], default='pending', required=True, readonly=True)

    _unique_alert_key = models.Constraint(
        'UNIQUE(alert_key)',
        'A health alert with this account/page/check already exists.')

    # ------------------------------------------------------------------
    # Eligibility and notification
    # ------------------------------------------------------------------
    def _eligible_owner(self):
        """Re-validate the configured owner at notification time: active,
        internal, Meta manager, with access to the alert company. Access
        may have changed since configuration; an ineligible owner keeps
        the alert visible and flagged instead of notifying anyone else."""
        self.ensure_one()
        owner = self.account_id.health_owner_id
        if (owner and owner.active and not owner.share
                and owner.has_group('crm_meta_lead_ads.group_meta_lead_manager')
                and self.company_id in owner.company_ids):
            return owner
        return self.env['res.users'].browse()

    def _health_activity_type(self):
        return self.env.ref('crm_meta_lead_ads.mail_activity_data_meta_health')

    def _notify_owner(self):
        """At most one open Meta Health activity per incident, only at the
        start of an episode (create or reopen after recovery)."""
        activity_type = self._health_activity_type()
        model_id = self.env['ir.model']._get_id(self._name)
        for alert in self:
            owner = alert._eligible_owner()
            if not owner:
                alert.notification_state = 'no_recipient'
                continue
            existing = self.env['mail.activity'].sudo().search([
                ('res_model_id', '=', model_id), ('res_id', '=', alert.id),
                ('activity_type_id', '=', activity_type.id),
            ], limit=1)
            if not existing:
                self.env['mail.activity'].sudo().create({
                    'activity_type_id': activity_type.id,
                    'res_model_id': model_id, 'res_id': alert.id,
                    'user_id': owner.id,
                    'summary': _('Meta health: %s (%s)') % (alert.name, alert.account_id.name),
                    'note': alert.detail or False,
                    'date_deadline': fields.Date.today(),
                })
            alert.notification_state = 'notified'

    def _complete_own_activity(self):
        """Complete ONLY this incident's dedicated Meta Health activities.
        To-Dos and any other activity types on the same record (or anywhere
        else) are never touched."""
        activity_type = self._health_activity_type()
        activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', self._name), ('res_id', 'in', self.ids),
            ('activity_type_id', '=', activity_type.id),
        ])
        if activities:
            activities.action_done()

    def _resolve(self):
        for alert in self:
            alert.write({'state': 'resolved', 'resolved_at': fields.Datetime.now()})
            alert._complete_own_activity()

    def action_acknowledge(self):
        """Suppress reminders for the current episode. The alert stays
        visible; a new episode after recovery may notify again."""
        if not self.env.user.has_group('crm_meta_lead_ads.group_meta_lead_manager'):
            raise UserError(_('Only Meta Lead Ads managers can acknowledge health alerts.'))
        for alert in self.filtered(lambda a: a.state == 'open'):
            alert.state = 'acknowledged'
        return True

    # ------------------------------------------------------------------
    # Transaction-safe upsert
    # ------------------------------------------------------------------
    def _alert_key(self, account, page_id, check_code):
        return '%s:%s:%s' % (account.id, page_id or 0, check_code)

    def _upsert_alert(self, account, page_id, check_code, severity, detail, summary_count):
        """Keep exactly one incident per (account, page, check code).

        An open/acknowledged record is updated silently (no chatter, no
        new activity). A resolved record reopens as a new episode and
        notifies again.

        Concurrency truth under Odoo's REPEATABLE READ: a truly
        concurrent competing insert blocks on the winner's uncommitted
        tuple and then raises a serialization failure (40001) — NOT an
        IntegrityError — which propagates to the caller (the monitor cron
        catches it in the per-account savepoint and retries on the next
        pass, in a fresh transaction whose pre-check then sees and reuses
        the winner's row). The savepoint + IntegrityError re-read below
        resolves only collisions whose competitor row is visible to THIS
        transaction's snapshot; a competitor committed after the snapshot
        is invisible to the re-read, and in that case the re-read finds
        nothing and the IntegrityError is deliberately re-raised — the
        collision is resolved by a fresh transaction, never by an
        in-transaction re-read."""
        page_id = page_id or False
        key = self._alert_key(account, page_id, check_code)
        now = fields.Datetime.now()
        Alert = self.sudo()
        existing = Alert.search([('alert_key', '=', key)], limit=1)
        if existing:
            vals = {
                'last_seen_at': now, 'severity': severity,
                'detail': detail, 'summary_count': summary_count,
            }
            if existing.state == 'resolved':
                vals.update({
                    'state': 'open', 'resolved_at': False, 'first_seen_at': now,
                    'notification_state': 'pending',
                })
                existing.write(vals)
                existing._notify_owner()
            else:
                existing.write(vals)
            return existing
        label = dict(CHECK_CODES).get(check_code, check_code)
        vals = {
            'name': label, 'company_id': account.company_id.id,
            'account_id': account.id, 'page_id': page_id,
            'check_code': check_code, 'alert_key': key,
            'severity': severity, 'state': 'open',
            'first_seen_at': now, 'last_seen_at': now,
            'summary_count': summary_count, 'detail': detail,
        }
        try:
            with self.env.cr.savepoint():
                alert = Alert.create(vals)
        except IntegrityError:
            # A concurrent monitor pass inserted the same key between the
            # pre-check and our insert. Reuse that incident; if no such
            # row exists the error is something else and must surface.
            alert = Alert.search([('alert_key', '=', key)], limit=1)
            if not alert:
                raise
            alert.write({
                'last_seen_at': now, 'severity': severity,
                'detail': detail, 'summary_count': summary_count,
            })
            return alert
        alert._notify_owner()
        return alert

    # ------------------------------------------------------------------
    # Local monitoring assessment (no Graph API calls)
    # ------------------------------------------------------------------
    @api.model
    def _cron_health_monitor(self, batch_limit=MONITOR_BATCH_LIMIT):
        """Every 5 minutes: bounded, local-only assessment of enabled
        accounts, each in its own savepoint so one failing account never
        blocks the rest. Remote evidence refresh stays with the manual
        Run Diagnostics action."""
        Account = self.env['meta.account'].sudo()
        accounts = Account.search(
            [('health_monitoring_enabled', '=', True)],
            order='health_last_check_at asc nulls first, id asc',
            limit=batch_limit)
        for account in accounts:
            try:
                with self.env.cr.savepoint():
                    self._assess_account(account)
            except Exception as exc:
                safe = account._sanitize_error(
                    exc, [account.user_access_token, account.app_secret])
                _logger.error(
                    'Meta health monitor failed for account %s: %s', account.id, safe)
        return True

    @api.model
    def _assess_account(self, account):
        """Evaluate every alert condition for one account against stored
        data only. Every query is scoped to the account and its company;
        sudo is used so the cron user sees the same scope regardless of
        record rules. Conditions that no longer hold resolve their
        incident and complete only that incident's activity."""
        now = fields.Datetime.now()
        company = account.company_id
        active = set()
        account_sudo = account.sudo()
        secrets = [account_sudo.user_access_token, account_sudo.app_secret]
        if not account.health_monitoring_enabled:
            # Disabled monitoring means no alerts, ever — even for a manual
            # Check Now. The health level stays "Disabled".
            return False

        def clean(text):
            return account._sanitize_error(text or '', secrets)[:200]

        # Known account error/disconnection while monitoring is enabled.
        if account.state in ('error', 'disconnected'):
            severity = 'error' if account.state == 'error' else 'warning'
            detail = clean(account.error_message) or _('Account state: %s') % account.state
            active.add(('account_error', False, severity, detail, 0))

        # Known token expiry / impending expiry. Unknown expiry stays
        # unknown — no alert is fabricated for a missing expiry date.
        if account.token_expires_at:
            if account.token_expires_at <= now:
                active.add(('token_expired', False, 'error',
                            _('The Meta user token has expired.'), 0))
            elif account.token_expires_at <= now + timedelta(
                    days=account.health_token_expiry_warn_days):
                active.add(('token_expiring', False, 'warning',
                            _('The Meta user token expires at %s.') % account.token_expires_at, 0))

        pages = account.page_ids.filtered('sync_enabled')
        all_pages = self.env['meta.page'].with_context(active_test=False).sudo().search([
            ('account_id', '=', account.id), ('company_id', '=', company.id)])

        # Page subscription checks (archived pages excluded). Only a
        # freshly checked failure/incomplete state asserts a current
        # problem; older checks downgrade to a stale-check warning.
        for page in pages:
            if page.subscription_status not in ('failed', 'incomplete') \
                    or not page.subscription_checked_at:
                continue
            age = now - page.subscription_checked_at
            if age <= timedelta(hours=STALE_CHECK_HOURS):
                severity = 'error' if page.subscription_status == 'failed' else 'warning'
                detail = clean(page.subscription_error) or _(
                    'Page subscription check reported: %s') % page.subscription_status
                active.add(('subscription_failed', page.id, severity, detail, 0))
            else:
                active.add(('subscription_stale', page.id, 'warning', _(
                    'The last subscription check for this page is older than %s hours; '
                    'the recorded failure may no longer be current.') % STALE_CHECK_HOURS, 0))

        Queue = self.env['meta.lead.queue'].sudo()
        queue_scope = [('company_id', '=', company.id), ('page_id', 'in', all_pages.ids)]
        cutoff = now - timedelta(minutes=account.health_backlog_threshold_minutes)

        # Due backlog: pending/retry records past the threshold. Retries
        # scheduled in the future do not count.
        due = Queue.search(queue_scope + [
            ('state', 'in', ('pending', 'retry')), ('received_at', '<=', cutoff),
            '|', ('next_retry_at', '=', False), ('next_retry_at', '<=', now)])
        if due:
            active.add(('queue_backlog', False, 'warning', _(
                '%s queue record(s) are due for processing and older than the '
                'configured %s-minute threshold.') % (len(due), account.health_backlog_threshold_minutes),
                len(due)))

        # Processing records without progress beyond the threshold.
        stuck = Queue.search_count(queue_scope + [
            ('state', '=', 'processing'), ('received_at', '<=', cutoff)])
        if stuck:
            active.add(('queue_stuck', False, 'warning', _(
                '%s queue record(s) have been in processing longer than the '
                'configured %s-minute threshold.') % (stuck, account.health_backlog_threshold_minutes),
                stuck))

        # Failed/ambiguous queue records requiring operator intervention.
        failures = Queue.search_count(queue_scope + [('state', 'in', ('failed', 'ambiguous'))])
        if failures:
            active.add(('queue_failures', False, 'error', _(
                '%s failed or ambiguous queue record(s) require operator intervention.')
                % failures, failures))

        # Recorded outbound failures in the last 24h. A recorded failure is
        # not proof the recipient never received the message.
        Message = self.env['meta.message'].sudo()
        outbound_failed = Message.search_count(
            queue_scope + [
                ('direction', '=', 'outbound'), ('send_state', '=', 'failed'),
                ('sent_at', '>=', now - timedelta(hours=OUTBOUND_FAILURE_WINDOW_HOURS))])
        if outbound_failed:
            active.add(('outbound_failed', False, 'warning', _(
                '%s recorded outbound send failure(s) in the last %s hours. These are '
                'recorded failures; Meta may still have delivered some messages.')
                % (outbound_failed, OUTBOUND_FAILURE_WINDOW_HOURS), outbound_failed))

        # Optional expected-traffic silence. The clock starts at monitoring
        # enablement when no live receipt is known, and the warning stays
        # explicitly uncertain: silence is not proof of disconnection.
        if account.health_silence_minutes > 0:
            silence_cutoff = now - timedelta(minutes=account.health_silence_minutes)
            for page in pages:
                baseline = page.last_webhook_event_at or account.health_monitoring_since
                if baseline and baseline <= silence_cutoff:
                    active.add(('silence', page.id, 'warning', _(
                        'No authenticated webhook event observed for this page since %s. '
                        'Verify that this silence is expected — a quiet page is not '
                        'necessarily disconnected.') % baseline, 0))

        for code, page_id, severity, detail, count in sorted(active, key=str):
            self._upsert_alert(account, page_id, code, severity, detail, count)

        # Recovery: resolve incidents whose condition no longer holds.
        active_keys = {self._alert_key(account, page_id, code)
                       for code, page_id, _severity, _detail, _count in active}
        open_alerts = self.sudo().search([
            ('account_id', '=', account.id),
            ('company_id', '=', company.id),
            ('state', 'in', ('open', 'acknowledged'))])
        for alert in open_alerts:
            if alert.alert_key not in active_keys:
                alert._resolve()

        account.sudo().write({'health_last_check_at': now})
        return True


class MetaAccountHealth(models.Model):
    """Monitoring settings, live-receipt evidence and aggregated health
    metrics for a Meta account. All metrics are computed from local data
    with aggregate queries scoped by company AND account — opening the
    health screen never calls Meta."""
    _inherit = 'meta.account'

    health_monitoring_enabled = fields.Boolean(
        default=False, tracking=True,
        help='Disabled by default. While disabled the monitor creates no alerts.')
    health_monitoring_since = fields.Datetime(
        readonly=True, copy=False,
        help='Set when monitoring is enabled; starts the expected-traffic silence clock.')
    health_owner_id = fields.Many2one(
        'res.users', string='Monitoring Owner',
        help='Active internal Meta Lead Ads manager with access to this company. '
             'Re-validated before every notification.')
    health_backlog_threshold_minutes = fields.Integer(
        default=15,
        help='Due queue records older than this raise a backlog warning.')
    health_silence_minutes = fields.Integer(
        default=0, string='Expected-traffic silence interval (minutes)',
        help='0 = disabled. When set, an account/page with no authenticated webhook '
             'event for this long raises an explicitly uncertain "verify expected '
             'traffic" warning — silence is not proof of disconnection.')
    health_token_expiry_warn_days = fields.Integer(
        default=7,
        help='Warn this many days before the Meta user token expires.')
    health_last_check_at = fields.Datetime(readonly=True, copy=False,
                                           help='Last local monitor assessment. If stale, the monitor itself is not running.')
    last_webhook_event_at = fields.Datetime(
        readonly=True, copy=False,
        help='Last authenticated inbound webhook event matched to this account. '
             'Never backfilled from historical imports; empty means Unknown. '
             'Persisted only when the webhook request commits — a request '
             'aborted by a leadgen enqueue failure loses the stamp.')
    alert_ids = fields.One2many('meta.health.alert', 'account_id', string='Health Alerts')

    health_level = fields.Selection([
        ('disabled', 'Disabled'), ('unknown', 'Unknown'),
        ('ok', 'No detected issues'), ('warning', 'Warning'), ('error', 'Error'),
    ], compute='_compute_health_level', readonly=True)
    health_open_alert_count = fields.Integer(compute='_compute_health_level', readonly=True)
    health_due_backlog_count = fields.Integer(compute='_compute_health_metrics', readonly=True)
    health_oldest_due_minutes = fields.Integer(compute='_compute_health_metrics', readonly=True)
    health_failed_ambiguous_count = fields.Integer(compute='_compute_health_metrics', readonly=True)
    health_outbound_failed_24h = fields.Integer(compute='_compute_health_metrics', readonly=True)
    health_last_sync_state = fields.Char(compute='_compute_health_metrics', readonly=True)
    health_last_sync_at = fields.Datetime(compute='_compute_health_metrics', readonly=True)
    health_last_live_message_at = fields.Datetime(compute='_compute_health_metrics', readonly=True)
    health_last_live_lead_at = fields.Datetime(compute='_compute_health_metrics', readonly=True)

    def _compute_health_level(self):
        Alert = self.env['meta.health.alert'].sudo()
        for account in self:
            if not account.health_monitoring_enabled:
                account.health_level = 'disabled'
                account.health_open_alert_count = 0
                continue
            # Scoped to this account (which is bound to one company); sudo
            # keeps the result independent of the viewer's active company.
            alerts = Alert.search([
                ('account_id', '=', account.id),
                ('state', 'in', ('open', 'acknowledged'))])
            account.health_open_alert_count = len(alerts)
            if any(a.severity == 'error' for a in alerts):
                account.health_level = 'error'
            elif alerts:
                account.health_level = 'warning'
            elif account.health_last_check_at:
                account.health_level = 'ok'
            else:
                account.health_level = 'unknown'

    def _health_pages(self):
        self.ensure_one()
        return self.env['meta.page'].with_context(active_test=False).sudo().search([
            ('account_id', '=', self.id), ('company_id', '=', self.company_id.id)])

    def _compute_health_metrics(self):
        """Aggregate counts only — no message bodies, tokens, PSIDs or
        contact data. Queries are scoped to the account's company and the
        account's own pages; sudo makes the scope explicit and independent
        of the viewer's active company."""
        Queue = self.env['meta.lead.queue'].sudo()
        Message = self.env['meta.message'].sudo()
        Run = self.env['meta.sync.run'].sudo()
        now = fields.Datetime.now()
        for account in self:
            pages = account._health_pages()
            scope = [('company_id', '=', account.company_id.id), ('page_id', 'in', pages.ids)]
            due = Queue.search(scope + [
                ('state', 'in', ('pending', 'retry')),
                '|', ('next_retry_at', '=', False), ('next_retry_at', '<=', now)],
                order='received_at asc, id asc')
            account.health_due_backlog_count = len(due)
            oldest = due[:1].received_at
            account.health_oldest_due_minutes = int((now - oldest).total_seconds() // 60) if oldest else 0
            account.health_failed_ambiguous_count = Queue.search_count(
                scope + [('state', 'in', ('failed', 'ambiguous'))])
            account.health_outbound_failed_24h = Message.search_count(scope + [
                ('direction', '=', 'outbound'), ('send_state', '=', 'failed'),
                ('sent_at', '>=', now - timedelta(hours=OUTBOUND_FAILURE_WINDOW_HOURS))])
            run = Run.search([
                ('account_id', '=', account.id), ('company_id', '=', account.company_id.id),
                ('state', 'in', ('completed', 'completed_warnings', 'failed', 'cancelled'))],
                order='id desc', limit=1)
            account.health_last_sync_state = (
                dict(Run._fields['state'].selection).get(run.state) if run else False)
            account.health_last_sync_at = run.ended_at if run else False
            message_ts = [p.last_live_message_at for p in pages if p.last_live_message_at]
            lead_ts = [p.last_live_lead_enqueued_at for p in pages if p.last_live_lead_enqueued_at]
            account.health_last_live_message_at = max(message_ts) if message_ts else False
            account.health_last_live_lead_at = max(lead_ts) if lead_ts else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('health_monitoring_enabled') and not vals.get('health_monitoring_since'):
                vals['health_monitoring_since'] = fields.Datetime.now()
        return super().create(vals_list)

    def write(self, vals):
        # Start the silence clock when monitoring switches on.
        if vals.get('health_monitoring_enabled') and 'health_monitoring_since' not in vals:
            enabling = self.filtered(lambda a: not a.health_monitoring_enabled)
            if enabling:
                vals = dict(vals, health_monitoring_since=fields.Datetime.now())
        return super().write(vals)

    @api.constrains('health_owner_id', 'company_id')
    def _check_health_owner(self):
        for account in self:
            owner = account.health_owner_id
            if not owner:
                continue
            if (not owner.active or owner.share
                    or not owner.has_group('crm_meta_lead_ads.group_meta_lead_manager')
                    or account.company_id not in owner.company_ids):
                raise ValidationError(_(
                    'The monitoring owner must be an active internal Meta Lead Ads '
                    'manager with access to the account company.'))

    @api.constrains('health_backlog_threshold_minutes', 'health_silence_minutes',
                    'health_token_expiry_warn_days')
    def _check_health_thresholds(self):
        for account in self:
            if account.health_backlog_threshold_minutes < 1:
                raise ValidationError(_('The due-backlog threshold must be at least 1 minute.'))
            if account.health_silence_minutes < 0:
                raise ValidationError(_('The silence interval cannot be negative (0 disables it).'))
            if account.health_token_expiry_warn_days < 0:
                raise ValidationError(_('The token-expiry warning interval cannot be negative.'))

    def _check_health_manager_access(self):
        """Guard every public health action: the caller must be a Meta
        manager AND the account's company must be inside the caller's
        allowed companies BEFORE any sudo escalation happens below. The
        company read is the only escalation here and returns an id, so a
        cross-company RPC call fails with a clean UserError instead of
        silently running sudo-scoped queries on another company's data."""
        if not self.env.user.has_group('crm_meta_lead_ads.group_meta_lead_manager'):
            raise UserError(_('Only Meta Lead Ads managers can use health actions.'))
        allowed = self.env.user.company_ids
        for account in self:
            if account.sudo().company_id not in allowed:
                raise UserError(_(
                    'This Meta account belongs to a company you cannot access.'))

    def action_health_check_now(self):
        """Run the local monitoring assessment now (manager only). Local
        data only — this never calls Meta. The sudo below is safe only
        because the guard above already proved company membership; the
        assessment itself is local-data-only and company-scoped, and it
        must read admin-restricted fields (e.g. token_expires_at)."""
        self._check_health_manager_access()
        Alert = self.env['meta.health.alert']
        for account in self:
            Alert._assess_account(account.sudo())
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Meta Health'),
                'message': _('Local health assessment updated.'),
                'type': 'success', 'sticky': False,
            },
        }

    def action_health_open_queue(self):
        """Drill down to this account's ingestion queue, keeping the
        account scope; normal record rules still apply."""
        self._check_health_manager_access()
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Ingestion Queue'),
            'res_model': 'meta.lead.queue',
            'view_mode': 'list,form',
            'domain': [('page_id', 'in', self._health_pages().ids)],
            'target': 'current',
        }


class MetaPageHealth(models.Model):
    """Live-receipt evidence per page, kept separate from processing
    success. These timestamps are stamped only after webhook signature
    validation and a page/account match; they are never backfilled from
    queue received_at (historical imports write that too), so existing
    pages start Unknown.

    Limitation: a stamp persists only when the webhook request
    transaction commits. A leadgen enqueue failure aborts the request
    (non-2xx so Meta redelivers) and the stamp rolls back with it, so a
    missing stamp never proves non-delivery."""
    _inherit = 'meta.page'

    last_webhook_event_at = fields.Datetime(
        readonly=True, copy=False,
        help='Last authenticated inbound webhook event matched to this page. '
             'Persisted only when the webhook request commits.')
    last_live_message_at = fields.Datetime(
        readonly=True, copy=False,
        help='Last live inbound Messenger message recorded from the webhook. '
             'Persisted only when the webhook request commits.')
    last_live_lead_enqueued_at = fields.Datetime(
        readonly=True, copy=False,
        help='Last live lead newly enqueued from the webhook. '
             'Persisted only when the webhook request commits.')

    def _note_live_receipt(self, kind='event'):
        """Stamp live webhook evidence. ``kind`` is 'event' (any
        authenticated matched event), 'message' (a new live inbound
        message was recorded) or 'lead' (a new live lead was enqueued).
        Receipt is recorded independently of later processing success.

        Limitation: the stamp persists only if the webhook request
        transaction commits. A leadgen enqueue failure deliberately
        aborts the request (non-2xx so Meta redelivers), rolling the
        stamp back with it — a missing stamp therefore never proves the
        event was not delivered, only that no committed request recorded
        it."""
        now = fields.Datetime.now()
        vals = {'last_webhook_event_at': now}
        if kind == 'message':
            vals['last_live_message_at'] = now
        elif kind == 'lead':
            vals['last_live_lead_enqueued_at'] = now
        for page in self:
            page.sudo().write(vals)
            page.account_id.sudo().write({'last_webhook_event_at': now})
