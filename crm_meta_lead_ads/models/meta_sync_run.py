import json
import logging
import time
from datetime import datetime, timezone

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from .meta_account import MetaPermissionError

_logger = logging.getLogger(__name__)

SYNC_STEPS = ('connection', 'pages', 'forms', 'leads', 'conversations', 'messages')
RUN_TYPE_STEPS = {
    'all': SYNC_STEPS,
    'leads': ('connection', 'leads'),
    'messages': ('connection', 'conversations', 'messages'),
    'forms': ('connection', 'pages', 'forms'),
}
TICK_SECONDS = 45
TICK_QUEUE_RECORDS = 20
LEADS_PAGE_LIMIT = 25


def _parse_graph_dt(value):
    """Parse Graph API ISO-8601 timestamps ('...+0000') to naive UTC."""
    if not value:
        return False
    try:
        return datetime.strptime(str(value), '%Y-%m-%dT%H:%M:%S%z').astimezone(
            timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return False


class MetaSyncRun(models.Model):
    _name = 'meta.sync.run'
    _description = 'Meta Sync Run'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(readonly=True, copy=False, default='New')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    account_id = fields.Many2one('meta.account', required=True, ondelete='cascade', index=True)
    run_type = fields.Selection([
        ('all', 'Full Sync'), ('leads', 'Leads Backfill'),
        ('messages', 'Conversations & Messages'), ('forms', 'Pages & Forms'),
    ], default='all', required=True, readonly=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('running', 'Running'), ('completed', 'Completed'),
        ('completed_warnings', 'Completed with warnings'),
        ('failed', 'Failed'), ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, index=True, copy=False)
    page_ids = fields.Many2many('meta.page', string='Limit to Pages',
                                help='Empty means every page of the account.')
    date_from = fields.Datetime(readonly=True)
    date_to = fields.Datetime(readonly=True)
    max_pages = fields.Integer(default=LEADS_PAGE_LIMIT, readonly=True)
    max_records = fields.Integer(default=1000, readonly=True)
    current_step = fields.Selection([(s, s.title()) for s in SYNC_STEPS], readonly=True, copy=False)
    work_state = fields.Json(readonly=True, copy=False)
    # Counters — numbers only, never PII.
    pages_synced = fields.Integer(readonly=True, copy=False)
    forms_fetched = fields.Integer(readonly=True, copy=False)
    forms_created = fields.Integer(readonly=True, copy=False)
    forms_updated = fields.Integer(readonly=True, copy=False)
    forms_archived = fields.Integer(readonly=True, copy=False)
    leads_fetched = fields.Integer(readonly=True, copy=False)
    leads_queued = fields.Integer(readonly=True, copy=False)
    leads_created = fields.Integer(readonly=True, copy=False)
    leads_matched = fields.Integer(readonly=True, copy=False)
    leads_duplicate = fields.Integer(readonly=True, copy=False)
    leads_ambiguous = fields.Integer(readonly=True, copy=False)
    leads_failed = fields.Integer(readonly=True, copy=False)
    conversations_created = fields.Integer(readonly=True, copy=False)
    conversations_updated = fields.Integer(readonly=True, copy=False)
    messages_created = fields.Integer(readonly=True, copy=False)
    messages_duplicate = fields.Integer(readonly=True, copy=False)
    messages_failed = fields.Integer(readonly=True, copy=False)
    warning_count = fields.Integer(readonly=True, copy=False)
    missing_permissions = fields.Char(readonly=True, copy=False,
                                      help='Permission codenames only.')
    started_at = fields.Datetime(readonly=True, copy=False)
    ended_at = fields.Datetime(readonly=True, copy=False)
    error_message = fields.Text(readonly=True, copy=False)
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True, copy=False)
    line_ids = fields.One2many('meta.sync.run.line', 'run_id', readonly=True)
    active = fields.Boolean(default=True)

    @api.constrains('account_id', 'company_id', 'page_ids')
    def _check_run_scope(self):
        for run in self:
            if run.account_id.company_id != run.company_id:
                raise ValidationError(_('The sync run account belongs to another company.'))
            if any(page.account_id != run.account_id or page.company_id != run.company_id
                   for page in run.page_ids):
                raise ValidationError(_('Every selected page must belong to the sync run account and company.'))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.name == 'New':
                rec.name = 'SR-%05d' % rec.id
        return records

    def _form_action(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name,
            'res_id': self.id, 'view_mode': 'form', 'target': 'current',
        }

    def _active_sibling(self):
        self.ensure_one()
        return self.search([
            ('account_id', '=', self.account_id.id),
            ('state', 'in', ('draft', 'running')),
            ('id', '!=', self.id),
        ], limit=1)

    def action_start(self):
        for run in self:
            if run.state not in ('draft', 'failed'):
                raise UserError(_('Only draft or failed runs can be started.'))
            other = run._active_sibling()
            if other:
                raise UserError(_(
                    'Another sync run (%s) is already active for this Meta account. '
                    'Wait for it to finish or cancel it first.') % other.name)
            run.write({
                'state': 'running', 'started_at': fields.Datetime.now(),
                'ended_at': False, 'error_message': False,
            })
        return True

    def action_cancel(self):
        for run in self:
            if run.state in ('draft', 'running'):
                run.write({'state': 'cancelled', 'ended_at': fields.Datetime.now()})
        return True

    def _bump(self, **counts):
        for fname, delta in counts.items():
            if delta:
                self[fname] = self[fname] + delta

    def _warn(self, line, message):
        self._bump(warning_count=1)
        if line:
            line.write({'state': 'warning', 'message': message})
        _logger.warning('Meta sync run %s: %s', self.name, message)

    def _step_line(self, step):
        line = self.line_ids.filtered(lambda l: l.step == step)[:1]
        now = fields.Datetime.now()
        if not line:
            return self.env['meta.sync.run.line'].create({
                'run_id': self.id, 'step': step, 'state': 'running', 'started_at': now,
            })
        line.write({'state': 'running', 'started_at': line.started_at or now})
        return line

    def _finish_line(self, line, state='done', message=False):
        line.write({'state': state, 'message': message or False,
                    'ended_at': fields.Datetime.now()})

    def _target_pages(self):
        self.ensure_one()
        pages = self.page_ids or self.account_id.page_ids
        return pages.with_context(active_test=False).filtered('sync_enabled').sorted('id')

    # ------------------------------------------------------------------
    # Engine
    # ------------------------------------------------------------------
    @api.model
    def _cron_process_runs(self):
        for run in self.sudo().search([('state', '=', 'running')], order='id', limit=5):
            try:
                with self.env.cr.savepoint():
                    run._process_tick()
            except Exception as exc:
                account = run.account_id
                safe = account._sanitize_error(exc, [account.user_access_token, account.app_secret])
                run.write({'state': 'failed', 'error_message': safe,
                           'ended_at': fields.Datetime.now()})
                run._notify_done()
                _logger.error('Meta sync run %s failed: %s', run.name, safe)
        return True

    def _process_tick(self):
        """Advance the run within one cron tick. All cursors live in
        ``work_state`` so a later tick resumes exactly where this one
        stopped; every fetch/create is idempotent on its own, so
        repeating a chunk after a failure is safe."""
        self.ensure_one()
        deadline = time.monotonic() + TICK_SECONDS
        work = dict(self.work_state or {})
        done_steps = set(work.get('done_steps') or [])
        for step in RUN_TYPE_STEPS[self.run_type]:
            if step in done_steps:
                continue
            self.current_step = step
            if self._execute_step(step, work.setdefault(step, {}), deadline):
                done_steps.add(step)
                work.pop(step, None)
                self.current_step = False
            else:
                work['done_steps'] = sorted(done_steps)
                self.work_state = work
                return
        self.work_state = {'done_steps': sorted(done_steps)}
        self._finalize()

    def _execute_step(self, step, ctx, deadline):
        line = self._step_line(step)
        handler = getattr(self, '_step_%s' % step)
        try:
            # A user-token permission diagnostic cannot establish what a
            # Page token can do. Try Graph, and discard partial step writes
            # if Graph itself rejects the operation.
            with self.env.cr.savepoint():
                finished = handler(ctx, deadline, line)
        except MetaPermissionError as exc:
            message = '%s step: %s' % (step, exc)
            self._warn(line, message)
            self._finish_line(line, state='warning', message=message)
            return True
        if finished and line.state == 'running':
            self._finish_line(line)
        return finished

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def _step_connection(self, ctx, deadline, line):
        account = self.account_id
        account._request('GET', 'me', token=account.user_access_token,
                         params={'fields': 'id,name'})
        granted, missing = account._fetch_permissions()
        if missing:
            self.missing_permissions = ','.join(missing)
            self._warn(line, 'user-token permission diagnostics report missing: %s; '
                       'each sync operation will still be attempted' % ', '.join(missing))
        return True

    def _step_pages(self, ctx, deadline, line):
        account = self.account_id
        Page = self.env['meta.page']
        after = ctx.get('after')
        seen = set(ctx.get('seen') or [])
        while True:
            params = {'fields': 'id,name,access_token,tasks', 'limit': 100}
            if after:
                params['after'] = after
            data = account._request('GET', 'me/accounts',
                                    token=account.user_access_token, params=params)
            items = data.get('data') or []
            for item in items:
                if not item.get('id'):
                    continue
                synced, failed, _results = Page._upsert_pages_bulk(
                    self.company_id, account, [item])
                self._bump(pages_synced=synced)
                if failed:
                    self._bump(warning_count=failed)
            next_after = ((data.get('paging') or {}).get('cursors') or {}).get('after')
            if not next_after or not items:
                return True
            if next_after in seen or next_after == after:
                self._warn(line, 'repeated pagination cursor on pages step; stopping')
                return True
            seen.add(next_after)
            after = next_after
            ctx.update({'after': after, 'seen': sorted(seen)})
            if time.monotonic() > deadline:
                return False

    def _step_forms(self, ctx, deadline, line):
        pages = self._target_pages()
        offset = ctx.get('offset', 0)
        for page in pages[offset:]:
            stats = page._sync_forms_stats()
            page.last_sync_at = fields.Datetime.now()
            self._bump(
                forms_fetched=stats['fetched'], forms_created=stats['created'],
                forms_updated=stats['updated'], forms_archived=stats['archived'])
            offset += 1
            ctx['offset'] = offset
            if time.monotonic() > deadline:
                return False
        return True

    def _step_leads(self, ctx, deadline, line):
        if ctx.get('fetch_done'):
            return self._process_own_queue()
        Queue = self.env['meta.lead.queue'].sudo()
        forms = self.env['meta.form'].with_context(active_test=False).search([
            ('page_id', 'in', self._target_pages().ids),
            ('polling_enabled', '=', True),
        ], order='id')
        offset = ctx.get('form_offset', 0)
        after = ctx.get('after')
        pages_used = ctx.get('pages_used', 0)
        for form in forms[offset:]:
            if pages_used >= self.max_pages or self.leads_fetched >= self.max_records:
                self._warn(line, 'leads page/record budget reached; remaining leads stay for a later run')
                ctx['fetch_done'] = True
                return self._process_own_queue()
            page = form.page_id
            params = {'fields': 'id,created_time', 'limit': 100}
            filtering = self._leads_filtering()
            if filtering:
                params['filtering'] = filtering
            seen = set()
            while True:
                if after:
                    params['after'] = after
                data = page.account_id._request(
                    'GET', f'{form.meta_form_id}/leads',
                    token=page.page_access_token, params=params)
                pages_used += 1
                items = data.get('data') or []
                lead_ids = [str(i['id']) for i in items if i.get('id')]
                _unique, _pre, created, _race = Queue._enqueue_poll_leads(
                    form.company_id, page, form, lead_ids, sync_run=self)
                self._bump(leads_fetched=len(lead_ids), leads_queued=created)
                next_after = ((data.get('paging') or {}).get('cursors') or {}).get('after')
                if not next_after or not items:
                    after = False
                    break
                if next_after in seen or next_after == after:
                    self._warn(line, 'repeated pagination cursor on a leads form; moving on')
                    after = False
                    break
                if pages_used >= self.max_pages or self.leads_fetched >= self.max_records:
                    self._warn(line, 'leads page/record budget reached; remaining leads stay for a later run')
                    ctx['fetch_done'] = True
                    return self._process_own_queue()
                seen.add(next_after)
                after = next_after
                ctx.update({'form_offset': offset, 'after': after, 'pages_used': pages_used})
                if time.monotonic() > deadline:
                    self._process_own_queue()
                    return False
            offset += 1
            ctx.update({'form_offset': offset, 'after': False, 'pages_used': pages_used})
            if time.monotonic() > deadline:
                self._process_own_queue()
                return False
        ctx['fetch_done'] = True
        return self._process_own_queue()

    def _leads_filtering(self):
        clauses = []
        if self.date_from:
            clauses.append({'field': 'time_created', 'operator': 'GREATER_THAN',
                            'value': int(self.date_from.timestamp())})
        if self.date_to:
            clauses.append({'field': 'time_created', 'operator': 'LESS_THAN',
                            'value': int(self.date_to.timestamp())})
        return json.dumps(clauses) if clauses else False

    def _process_own_queue(self):
        Queue = self.env['meta.lead.queue'].sudo()
        now = fields.Datetime.now()
        records = Queue.search([
            ('sync_run_id', '=', self.id), ('state', 'in', ('pending', 'retry')),
            '|', ('next_retry_at', '=', False), ('next_retry_at', '<=', now),
        ], limit=TICK_QUEUE_RECORDS)
        for rec in records:
            rec.process_one()
        outcomes = Queue.search([('sync_run_id', '=', self.id)])
        self.write({
            'leads_created': len(outcomes.filtered(lambda r: r.state == 'done' and r.match_result == 'created')),
            'leads_matched': len(outcomes.filtered(lambda r: r.state == 'done' and r.match_result in ('matched_email', 'matched_phone'))),
            'leads_duplicate': len(outcomes.filtered(lambda r: r.state == 'duplicate')),
            'leads_ambiguous': len(outcomes.filtered(lambda r: r.state == 'ambiguous')),
            'leads_failed': len(outcomes.filtered(lambda r: r.state == 'failed')),
        })
        return not any(outcomes.filtered(lambda r: r.state in ('pending', 'retry', 'processing')))

    def _step_conversations(self, ctx, deadline, line):
        Conv = self.env['meta.conversation'].sudo()
        pages = self._target_pages()
        offset = ctx.get('page_offset', 0)
        after = ctx.get('after')
        for page in pages[offset:]:
            seen = set()
            while True:
                params = {'platform': 'messenger',
                          'fields': 'id,participants,updated_time', 'limit': 50}
                if after:
                    params['after'] = after
                data = page.account_id._request(
                    'GET', f'{page.meta_page_id}/conversations',
                    token=page.page_access_token, params=params)
                items = data.get('data') or []
                for thread in items:
                    participants = (thread.get('participants') or {}).get('data') or []
                    psid = next((str(p.get('id')) for p in participants
                                 if p.get('id') and str(p['id']) != str(page.meta_page_id)), False)
                    if not psid:
                        continue
                    conv = Conv._get_or_create(page, psid)
                    if not conv:
                        continue
                    if conv.meta_thread_id == str(thread.get('id')):
                        continue
                    if conv.meta_thread_id:
                        conv.meta_thread_id = str(thread.get('id'))
                        self._bump(conversations_updated=1)
                    else:
                        conv.meta_thread_id = str(thread.get('id'))
                        if conv.create_date and (fields.Datetime.now() - conv.create_date).total_seconds() < 60:
                            self._bump(conversations_created=1)
                        else:
                            self._bump(conversations_updated=1)
                next_after = ((data.get('paging') or {}).get('cursors') or {}).get('after')
                if not next_after or not items:
                    after = False
                    break
                if next_after in seen or next_after == after:
                    self._warn(line, 'repeated pagination cursor on conversations; moving on')
                    after = False
                    break
                seen.add(next_after)
                after = next_after
                ctx.update({'page_offset': offset, 'after': after})
                if time.monotonic() > deadline:
                    return False
            offset += 1
            ctx.update({'page_offset': offset, 'after': False})
            if time.monotonic() > deadline:
                return False
        return True

    def _step_messages(self, ctx, deadline, line):
        Conv = self.env['meta.conversation'].sudo()
        conversations = Conv.search([
            ('page_id', 'in', self._target_pages().ids),
            ('meta_thread_id', '!=', False),
        ], order='id')
        offset = ctx.get('conv_offset', 0)
        after = ctx.get('after')
        for conv in conversations[offset:]:
            page = conv.page_id
            seen = set()
            while True:
                params = {'fields': 'id,message,from,created_time,attachments', 'limit': 100}
                if after:
                    params['after'] = after
                data = page.account_id._request(
                    'GET', f'{conv.meta_thread_id}/messages',
                    token=page.page_access_token, params=params)
                items = data.get('data') or []
                for item in items:
                    result = self._upsert_history_message(conv, page, item)
                    if result == 'created':
                        self._bump(messages_created=1)
                    elif result == 'duplicate':
                        self._bump(messages_duplicate=1)
                    else:
                        self._bump(messages_failed=1)
                next_after = ((data.get('paging') or {}).get('cursors') or {}).get('after')
                if not next_after or not items:
                    after = False
                    conv._refresh_history_metrics()
                    conv.history_synced_at = fields.Datetime.now()
                    break
                if next_after in seen or next_after == after:
                    self._warn(line, 'repeated pagination cursor on messages; moving on')
                    after = False
                    break
                seen.add(next_after)
                after = next_after
                ctx.update({'conv_offset': offset, 'after': after})
                if time.monotonic() > deadline:
                    return False
            offset += 1
            ctx.update({'conv_offset': offset, 'after': False})
            if time.monotonic() > deadline:
                return False
        return True

    def _upsert_history_message(self, conv, page, item):
        """Upsert one historical thread message by Meta message id. The
        unique constraint plus this pre-check keep webhook-delivered
        copies from duplicating; attachments stay metadata-only (no
        download — SSRF safe)."""
        mid = str(item.get('id') or '')
        if not mid:
            return 'failed'
        Message = self.env['meta.message'].sudo()
        if Message.search([('meta_message_id', '=', mid),
                           ('company_id', '=', conv.company_id.id)], limit=1):
            return 'duplicate'
        sender = item.get('from') or {}
        sender_id = str(sender.get('id') or '')
        direction = 'outbound' if sender_id == str(page.meta_page_id) else 'inbound'
        attachments = []
        for attachment in (item.get('attachments') or {}).get('data') or []:
            attachments.append({
                'type': attachment.get('mime_type') or 'attachment',
                'url': attachment.get('file_url') or False,
                'name': attachment.get('name') or False,
            })
        text = item.get('message') or ''
        if not text and attachments:
            text = '[%s]' % attachments[0]['type']
        try:
            with self.env.cr.savepoint():
                Message.create({
                    'company_id': conv.company_id.id, 'page_id': page.id,
                    'conversation_id': conv.id, 'sender_psid': sender_id or conv.psid,
                    'sender_name': sender.get('name') or False,
                    'message_text': text, 'meta_message_id': mid,
                    'sent_at': _parse_graph_dt(item.get('created_time')) or fields.Datetime.now(),
                    'direction': direction,
                    'send_state': 'sent' if direction == 'outbound' else False,
                    'attachments_json': attachments,
                })
        except Exception:
            competitor = Message.search([('meta_message_id', '=', mid),
                                         ('company_id', '=', conv.company_id.id)], limit=1)
            if competitor:
                return 'duplicate'
            raise
        return 'created'

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------
    def _finalize(self):
        self.ensure_one()
        state = 'completed_warnings' if (self.warning_count or self.missing_permissions) else 'completed'
        self.write({'state': state, 'ended_at': fields.Datetime.now(),
                    'current_step': False})
        _logger.info(
            'Meta sync run %s finished (%s): pages=%s forms(f=%s,c=%s,u=%s,a=%s) '
            'leads(f=%s,q=%s,c=%s,m=%s,d=%s,a=%s,f=%s) conv(c=%s,u=%s) '
            'msg(c=%s,d=%s,f=%s) warnings=%s',
            self.name, state, self.pages_synced,
            self.forms_fetched, self.forms_created, self.forms_updated, self.forms_archived,
            self.leads_fetched, self.leads_queued, self.leads_created, self.leads_matched,
            self.leads_duplicate, self.leads_ambiguous, self.leads_failed,
            self.conversations_created, self.conversations_updated,
            self.messages_created, self.messages_duplicate, self.messages_failed,
            self.warning_count)
        self._notify_done()

    def _notify_done(self):
        """One completion activity for the run owner, honoring the
        crm_meta_lead_ads.sync_notify setting (always/issues/never)."""
        self.ensure_one()
        mode = self.env['ir.config_parameter'].sudo().get_param(
            'crm_meta_lead_ads.sync_notify', 'issues')
        if mode == 'never' or (mode == 'issues' and self.state == 'completed'):
            return
        todo = self.env.ref('mail.mail_activity_data_todo')
        model_id = self.env['ir.model']._get_id(self._name)
        existing = self.env['mail.activity'].sudo().search([
            ('res_model_id', '=', model_id), ('res_id', '=', self.id),
            ('activity_type_id', '=', todo.id),
        ], limit=1)
        if existing:
            return
        self.env['mail.activity'].sudo().create({
            'activity_type_id': todo.id, 'res_model_id': model_id, 'res_id': self.id,
            'user_id': self.user_id.id or self.env.user.id,
            'summary': _('Meta sync run %s: %s') % (self.name, self.state),
            'date_deadline': fields.Date.today(),
        })


class MetaSyncRunLine(models.Model):
    _name = 'meta.sync.run.line'
    _description = 'Meta Sync Run Step'
    _order = 'id'

    run_id = fields.Many2one('meta.sync.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='run_id.company_id', store=True, index=True)
    step = fields.Selection([(s, s.title()) for s in SYNC_STEPS], required=True, readonly=True)
    state = fields.Selection([
        ('pending', 'Pending'), ('running', 'Running'), ('done', 'Done'),
        ('warning', 'Warning'), ('failed', 'Failed'),
    ], default='pending', required=True, readonly=True)
    message = fields.Char(readonly=True)
    started_at = fields.Datetime(readonly=True)
    ended_at = fields.Datetime(readonly=True)
