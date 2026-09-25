import hashlib
import json
import logging
import random
import time
from datetime import timedelta
from psycopg2 import IntegrityError
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from .meta_dedup import normalize_email, normalize_phone

_logger = logging.getLogger(__name__)

POLL_MAX_PAGES = 10
POLL_OVERLAP_SECONDS = 300

# Technical keys allowed in the audit log payload. field_data and any
# customer contact answers (email/phone/name) must never reach
# meta.lead.log.payload_json — the full payload lives only in the
# protected fields (queue.fetched_payload, lead.meta_raw_payload).
AUDIT_PAYLOAD_KEYS = (
    'id', 'created_time', 'form_id', 'platform', 'is_organic',
    'ad_id', 'ad_name', 'adset_id', 'adset_name',
    'campaign_id', 'campaign_name',
)


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
    state = fields.Selection([('pending','Pending'),('processing','Processing'),('done','Done'),('duplicate','Duplicate'),('ambiguous','Ambiguous'),('retry','Retry'),('failed','Failed')], default='pending', required=True, index=True)
    match_result = fields.Selection([('created','Created'),('matched_email','Matched by Email'),('matched_phone','Matched by Phone'),('duplicate_meta_id','Duplicate Meta Lead ID'),('ambiguous','Ambiguous'),('failed','Failed')], readonly=True, index=True, copy=False)
    priority = fields.Integer(default=10)
    attempts = fields.Integer(default=0)
    next_retry_at = fields.Datetime(index=True)
    error_message = fields.Text(readonly=True)
    crm_lead_id = fields.Many2one('crm.lead', readonly=True, index=True)
    received_at = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    processed_at = fields.Datetime(readonly=True)
    sync_run_id = fields.Many2one('meta.sync.run', readonly=True, index=True, copy=False,
                                  help='Sync run that enqueued this record, if any.')
    manual_lead_id = fields.Many2one('crm.lead', copy=False,
                                     help='Chosen by a manager to resolve an ambiguous event.')
    processing_seconds = fields.Float(compute='_compute_processing_seconds', store=True,
                                      readonly=True)

    @api.depends('received_at', 'processed_at')
    def _compute_processing_seconds(self):
        for rec in self:
            if rec.received_at and rec.processed_at:
                rec.processing_seconds = (rec.processed_at - rec.received_at).total_seconds()
            else:
                rec.processing_seconds = 0.0

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
        fields_list = 'id,created_time,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,form_id,field_data,platform,is_organic'
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

    def _ensure_identity(self, lead, match_type):
        """Link this queue event's Meta lead ID to the CRM lead, keeping
        every Meta lead ID in its own audit row. Idempotent and safe
        against a concurrent insert of the same ID. An existing row for
        the same ID must point at the SAME lead — a conflict is raised,
        never swallowed, so no wrong linkage can slip through."""
        self.ensure_one()
        Identity = self.env['meta.lead.identity'].sudo()
        domain = [('meta_lead_id', '=', self.meta_lead_id), ('company_id', '=', self.company_id.id)]
        existing = Identity.search(domain, limit=1)
        if existing:
            if existing.crm_lead_id != lead:
                raise UserError(_(
                    'Meta lead identity conflict: this Meta lead ID is already '
                    'linked to CRM lead %s.') % existing.crm_lead_id.id)
            return existing
        try:
            with self.env.cr.savepoint():
                return Identity.create({
                    'company_id': self.company_id.id, 'crm_lead_id': lead.id,
                    'meta_lead_id': self.meta_lead_id, 'queue_id': self.id,
                    'match_type': match_type,
                })
        except IntegrityError:
            # Race: a concurrent transaction inserted the row. Accept it
            # only if it provably links the same lead and company —
            # anything else means a real inconsistency and must surface.
            competitor = Identity.search(domain, limit=1)
            if (competitor and competitor.crm_lead_id == lead
                    and competitor.company_id == self.company_id):
                return competitor
            raise

    def _normalized_keys(self, mapped):
        self.ensure_one()
        country = self.company_id.country_id
        uae = bool(country and country.code == 'AE')
        email = normalize_email(mapped.get('email_from'))
        phone = normalize_phone(mapped.get('phone'), uae_context=uae)
        return email, phone

    def _dedup_lock_keys(self, email, phone):
        """Deterministic 64-bit advisory-lock keys, one per normalized
        match key, scoped to the company. SHA-256 (never Python hash(),
        which differs between processes), sorted so concurrent workers
        always lock in the same order and cannot deadlock."""
        self.ensure_one()
        keys = []
        for channel, value in (('email', email), ('phone', phone)):
            if value:
                material = 'meta_dedup:%s:%s:%s' % (self.company_id.id, channel, value)
                digest = hashlib.sha256(material.encode('utf-8')).digest()
                keys.append(int.from_bytes(digest[:8], 'big', signed=True))
        return sorted(keys)

    def _acquire_dedup_locks(self, email, phone):
        """Take transaction-scoped advisory locks for every normalized
        key this event carries, in a fixed order. Two events that could
        resolve to the same lead always share at least one key (the
        matched channel), so they are serialized; the caller re-searches
        after locking."""
        for key in self._dedup_lock_keys(email, phone):
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', (key,))

    def _safe_audit_payload(self, payload):
        """Technical-only payload for meta.lead.log: IDs, platform and
        campaign metadata. Never field_data or customer contact data."""
        if not isinstance(payload, dict):
            return {}
        return {k: payload.get(k) for k in AUDIT_PAYLOAD_KEYS if payload.get(k) is not None}

    def _match_existing_lead(self, email, phone):
        """Conservative same-company dedup match on normalized contact
        data. Returns (outcome, lead, detail): exact normalized email or
        phone matches link to the existing lead; conflicting or multiple
        candidates are ambiguous and never auto-linked; the contact name
        alone is never a match key. Archived leads stay untouched."""
        self.ensure_one()
        Lead = self.env['crm.lead'].sudo()
        company_id = self.company_id.id
        email_lead = phone_lead = False
        if email:
            recs = Lead.search([('company_id', '=', company_id), ('meta_norm_email', '=', email)], limit=2)
            if len(recs) > 1:
                return 'ambiguous', False, 'normalized email matches several CRM leads (ids: %s)' % recs.ids
            email_lead = recs
        if phone:
            recs = Lead.search([('company_id', '=', company_id), ('meta_norm_phone', '=', phone)], limit=2)
            if len(recs) > 1:
                return 'ambiguous', False, 'normalized phone matches several CRM leads (ids: %s)' % recs.ids
            phone_lead = recs
        if email_lead and phone_lead and email_lead != phone_lead:
            return 'ambiguous', False, 'email and phone match different CRM leads (ids: %s, %s)' % (email_lead.id, phone_lead.id)
        if email_lead:
            return 'matched_email', email_lead, ''
        if phone_lead:
            return 'matched_phone', phone_lead, ''
        return 'created', False, ''

    def _fill_matched_lead(self, lead, mapped, payload, form, platform, source):
        """Fill ONLY empty fields on a matched existing lead. Never
        overwrites existing data and never touches salesperson, team,
        stage, won/lost or active state."""
        self.ensure_one()
        fill = {}
        for fname in ('contact_name', 'email_from', 'phone'):
            if not lead[fname] and mapped.get(fname):
                fill[fname] = mapped[fname]
        meta_vals = {
            'meta_form_id': form.id if form else False,
            'meta_page_id': self.page_id.id,
            'meta_platform': platform,
            'meta_ad_id': payload.get('ad_id'),
            'meta_adgroup_id': payload.get('adset_id'),
            'meta_adset_id': payload.get('adset_id'),
            'meta_campaign_id': payload.get('campaign_id'),
            'meta_ad_name': payload.get('ad_name'),
            'meta_adset_name': payload.get('adset_name'),
            'meta_campaign_name': payload.get('campaign_name'),
            'source_id': source.id if source else False,
        }
        for fname, value in meta_vals.items():
            if value and not lead[fname]:
                fill[fname] = value
        if not lead.meta_raw_payload:
            fill['meta_raw_payload'] = payload
        if fill:
            lead.write(fill)

    def _post_match_chatter(self, lead, outcome, form):
        self.ensure_one()
        label = 'email' if outcome == 'matched_email' else 'phone'
        lead.message_post(
            body=_('New Meta Lead Ads inquiry linked to this lead (matched by %s). Page: %s. Form: %s. Meta lead ID: %s.') % (
                label, self.page_id.name or '-', form.name if form else '-', self.meta_lead_id),
            message_type='comment', subtype_xmlid='mail.mt_note')

    def _resolve_crm_lead(self, payload):
        """Resolve this queue event to a CRM lead. Returns
        (lead, outcome, detail) where outcome is one of created,
        matched_email, matched_phone, duplicate_meta_id, ambiguous.
        Every search is company-scoped; a Meta lead ID owned by another
        company is never linked cross-company."""
        self.ensure_one()
        Lead = self.env['crm.lead'].sudo()
        existing = Lead.search([
            ('meta_lead_id', '=', self.meta_lead_id),
            ('company_id', '=', self.company_id.id)], limit=1)
        if existing:
            self._ensure_identity(existing, 'duplicate_meta_id')
            return existing, 'duplicate_meta_id', ''
        mapped, form = self._mapping_values(payload)
        email, phone = self._normalized_keys(mapped)
        # Serialize concurrent events that could resolve to the same
        # lead, then search AFTER the lock: a competitor that committed
        # meanwhile is found by this search, not by a duplicate create.
        self._acquire_dedup_locks(email, phone)
        outcome, matched, detail = self._match_existing_lead(email, phone)
        if outcome == 'ambiguous':
            return False, 'ambiguous', detail
        platform = (payload.get('platform') or 'unknown').lower()
        if platform not in ('facebook', 'instagram'):
            platform = 'unknown'
        source = self._resolve_source(platform)
        if matched:
            self._fill_matched_lead(matched, mapped, payload, form, platform, source)
            self._ensure_identity(matched, outcome)
            self._post_match_chatter(matched, outcome, form)
            return matched, outcome, ''
        name = mapped.pop('name', False) or mapped.get('contact_name') or mapped.get('email_from') or _('Meta Lead %s') % self.meta_lead_id
        vals = {
            **mapped, 'name': name, 'company_id': self.company_id.id, 'meta_lead_id': self.meta_lead_id,
            'meta_form_id': form.id if form else False, 'meta_page_id': self.page_id.id,
            'meta_platform': platform, 'meta_ad_id': payload.get('ad_id'), 'meta_adgroup_id': payload.get('adset_id'),
            'meta_adset_id': payload.get('adset_id'), 'meta_campaign_id': payload.get('campaign_id'),
            'meta_ad_name': payload.get('ad_name'), 'meta_adset_name': payload.get('adset_name'),
            'meta_campaign_name': payload.get('campaign_name'),
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
                lead = Lead.with_company(self.company_id).create(vals)
        except IntegrityError:
            lead = Lead.search([('meta_lead_id', '=', self.meta_lead_id)], limit=1)
            if lead and lead.company_id and lead.company_id != self.company_id:
                # The global UNIQUE(meta_lead_id) on crm.lead collided
                # with another company's lead. Never link cross-company;
                # park the event for manual review instead.
                return False, 'ambiguous', (
                    'meta lead ID already belongs to a CRM lead in another '
                    'company (lead id: %s); manual review required' % lead.id)
            if lead:
                self._ensure_identity(lead, 'duplicate_meta_id')
                return lead, 'duplicate_meta_id', ''
            # An IntegrityError with no competing lead is something else
            # entirely — never swallow it.
            raise
        self._ensure_identity(lead, 'created')
        answers = {x.get('name'): ', '.join(str(v) for v in x.get('values') or [])
                   for x in payload.get('field_data') or [] if x.get('name')}
        self.env['meta.routing.rule'].sudo().apply_for_lead(lead, queue=self, answers=answers)
        return lead, 'created', ''

    def _schedule_retry(self, message, fatal=False):
        self.ensure_one()
        max_attempts = int(self.env['ir.config_parameter'].sudo().get_param('crm_meta_lead_ads.max_attempts', '8'))
        attempts = self.attempts + 1
        if fatal or attempts >= max_attempts:
            self.write({'state': 'failed', 'match_result': 'failed', 'attempts': attempts, 'error_message': message, 'processed_at': fields.Datetime.now()})
            self._log('error', 'failed', message)
            self._schedule_attention_activity(_('Meta lead event failed permanently (queue %s)') % self.id)
            return
        delay = min(2 ** attempts, 60) + random.randint(0, 3)
        self.write({'state': 'retry', 'attempts': attempts, 'error_message': message, 'next_retry_at': fields.Datetime.now() + timedelta(minutes=delay)})
        self._log('warning', 'retry_scheduled', message)

    OUTCOME_STATE = {
        'created': 'done', 'matched_email': 'done', 'matched_phone': 'done',
        'duplicate_meta_id': 'duplicate', 'ambiguous': 'ambiguous',
    }

    def process_one(self):
        self.ensure_one()
        if self.state not in ('pending', 'retry', 'failed', 'ambiguous'):
            return
        self.state = 'processing'
        try:
            payload = self._fetch_lead()
            self.fetched_payload = payload
            lead, outcome, detail = self._resolve_crm_lead(payload)
            self.write({
                'state': self.OUTCOME_STATE[outcome], 'match_result': outcome,
                'crm_lead_id': lead.id if lead else False,
                'processed_at': fields.Datetime.now(), 'error_message': detail or False,
            })
            level = 'warning' if outcome == 'ambiguous' else 'info'
            self._log(level, outcome, detail or 'CRM lead %s' % lead.id,
                      self._safe_audit_payload(payload))
            if outcome == 'ambiguous':
                self._schedule_attention_activity(
                    _('Meta lead event needs manual resolution (queue %s)') % self.id)
        except Exception as exc:
            account = self.page_id.account_id
            message = account._sanitize_error(exc, [
                self.page_id.page_access_token,
                account.user_access_token, account.app_secret,
            ])
            # Log the sanitized message only — never exc_info, whose
            # traceback text could carry tokens or customer data.
            _logger.error('Meta lead processing failed for queue %s: %s', self.id, message)
            self._schedule_retry(message, fatal=account.state == 'error')

    @api.model
    def _cron_process_queue(self, limit=50):
        now = fields.Datetime.now()
        batch = self.sudo().search([
            ('state', 'in', ['pending', 'retry']), '|', ('next_retry_at', '=', False), ('next_retry_at', '<=', now)
        ], order='priority desc,id', limit=limit)
        for rec in batch:
            rec.process_one()
        return True

    def _enqueue_poll_leads(self, company, page, form, lead_ids, sync_run=None):
        """Bulk-enqueue polled leads: one pre-check search per form, then
        inserts only for IDs missing from the queue. The unique constraint
        and the savepoint/IntegrityError fallback remain as race protection
        against concurrent webhook delivery only — never as the normal
        duplicate path (which produced thousands of ERROR logs)."""
        unique_ids = list(dict.fromkeys(str(i) for i in lead_ids if i))
        if not unique_ids:
            return 0, 0, 0, 0
        existing = set(self.sudo().search([
            ('meta_lead_id', 'in', unique_ids), ('company_id', '=', company.id),
        ]).mapped('meta_lead_id'))
        pre_existing = 0
        created = 0
        race_duplicates = 0
        for lead_id in unique_ids:
            if lead_id in existing:
                pre_existing += 1
                continue
            vals = {
                'company_id': company.id, 'page_id': page.id, 'form_id': form.id,
                'meta_lead_id': lead_id, 'raw_webhook': {'recovery_poll': True},
            }
            if sync_run:
                vals['sync_run_id'] = sync_run.id
            try:
                with self.env.cr.savepoint():
                    self.sudo().create(vals)
                created += 1
            except IntegrityError:
                # A conflict is a race duplicate only if a competing row
                # provably exists now (e.g. the webhook won the race).
                # Otherwise the IntegrityError means something else entirely
                # and must propagate — never silently drop a Meta lead ID.
                competitor = self.sudo().search([
                    ('meta_lead_id', '=', lead_id), ('company_id', '=', company.id),
                ], limit=1)
                if competitor:
                    race_duplicates += 1
                else:
                    raise
        return len(unique_ids), pre_existing, created, race_duplicates

    def _poll_form_leads(self, form):
        """Fetch lead IDs for one form with safe cursor pagination.

        Uses paging.cursors.after as a plain parameter on the same Graph
        endpoint (never paging.next as a full URL, so no external host can
        be injected), stops at POLL_MAX_PAGES or on a repeated cursor.
        Returns (lead_ids, pages)."""
        page = form.page_id
        params = {'fields': 'id,created_time', 'limit': 100}
        if form.last_sync_date:
            # Live-verified on Graph v25.0: the leads edge silently ignores
            # plain `since`; time filtering works only via `filtering`.
            since = int(form.last_sync_date.timestamp()) - POLL_OVERLAP_SECONDS
            params['filtering'] = json.dumps([{
                'field': 'time_created', 'operator': 'GREATER_THAN', 'value': since,
            }])
        lead_ids = []
        seen_cursors = set()
        after = False
        pages = 0
        while True:
            if after:
                params['after'] = after
            data = page.account_id._request(
                'GET', f'{form.meta_form_id}/leads', token=page.page_access_token, params=params)
            pages += 1
            items = data.get('data') or []
            lead_ids.extend(str(item['id']) for item in items if item.get('id'))
            next_after = ((data.get('paging') or {}).get('cursors') or {}).get('after')
            if not next_after or not items:
                break
            if next_after in seen_cursors or next_after == after:
                _logger.warning(
                    'Meta polling: repeated pagination cursor on form %s; stopping at page %s',
                    form.id, pages)
                break
            if pages >= POLL_MAX_PAGES:
                _logger.warning(
                    'Meta polling: page limit %s reached on form %s; older leads wait for the next cycle',
                    POLL_MAX_PAGES, form.id)
                break
            seen_cursors.add(next_after)
            after = next_after
        return lead_ids, pages

    @api.model
    def _cron_poll_meta_leads(self):
        forms = self.env['meta.form'].sudo().search([('active','=',True),('polling_enabled','=',True),('page_id.sync_enabled','=',True)])
        for form in forms:
            sync_started_at = fields.Datetime.now()
            timer = time.monotonic()
            try:
                lead_ids, pages = self._poll_form_leads(form)
                unique, pre_existing, created, race_duplicates = self._enqueue_poll_leads(
                    form.company_id, form.page_id, form, lead_ids)
                # Anchor the watermark at the start of the sync, not the end,
                # so a lead arriving during pagination is never skipped.
                form.last_sync_date = sync_started_at
                _logger.info(
                    'Meta polling form %s: fetched=%s pages=%s unique=%s existing=%s created=%s race=%s duration=%.1fs',
                    form.id, len(lead_ids), pages, unique, pre_existing,
                    created, race_duplicates, time.monotonic() - timer)
            except Exception as exc:
                account = form.page_id.account_id
                message = account._sanitize_error(exc, [
                    form.page_id.page_access_token,
                    account.user_access_token, account.app_secret,
                ])
                self.env['meta.lead.log'].sudo().create({'company_id': form.company_id.id, 'level': 'error', 'action': 'poll_failed', 'message': message})
        return True

    # ------------------------------------------------------------------
    # Manual operations from the UI
    # ------------------------------------------------------------------
    def action_retry_selected(self):
        """Requeue failed/ambiguous/retry records for processing. Safe
        against duplicates: process_one always resolves through the dedup
        matcher and the unique constraints, never a blind create."""
        retryable = self.filtered(lambda r: r.state in ('failed', 'ambiguous', 'retry'))
        for rec in retryable:
            rec.write({'state': 'pending', 'next_retry_at': False, 'error_message': False})
            rec._log('info', 'manual_retry', 'Requeued manually by user %s.' % self.env.user.id)
        return True

    @api.model
    def action_retry_all_failed(self):
        records = self.search([('state', '=', 'failed')])
        records.action_retry_selected()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('Meta Ingestion Queue'),
                'message': _('%s failed record(s) requeued.') % len(records),
                'type': 'success', 'sticky': False, 'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def action_reset_pending(self):
        """Manager-only reset of any non-processing record to pending."""
        if not self.env.user.has_group('crm_meta_lead_ads.group_meta_lead_manager'):
            raise UserError(_('Only Meta Lead Ads managers can reset queue records.'))
        for rec in self.filtered(lambda r: r.state != 'processing'):
            rec.write({'state': 'pending', 'next_retry_at': False, 'error_message': False})
            rec._log('warning', 'manual_reset', 'Reset to pending by user %s.' % self.env.user.id)
        return True

    def action_link_manual_lead(self):
        """Resolve an ambiguous event by linking the manager-chosen CRM
        lead. Never overwrites the lead's data and never creates a new
        lead; the identity row keeps this Meta lead ID auditable."""
        for rec in self:
            if rec.state != 'ambiguous':
                raise UserError(_('Only ambiguous records can be resolved manually.'))
            if not rec.manual_lead_id:
                raise UserError(_('Choose a CRM lead in "Manual Lead" first.'))
            lead = rec.manual_lead_id
            if lead.company_id and lead.company_id != rec.company_id:
                raise UserError(_('The chosen lead belongs to another company.'))
            rec._ensure_identity(lead, 'manual')
            rec.write({
                'state': 'done', 'crm_lead_id': lead.id,
                'processed_at': fields.Datetime.now(), 'error_message': False,
            })
            rec._log('info', 'manual_resolution',
                     'Ambiguous event linked to CRM lead %s by user %s.'
                     % (lead.id, self.env.user.id),
                     {'id': rec.meta_lead_id})
            lead.message_post(
                body=_('Meta Lead Ads queue event %s was linked to this lead manually.') % rec.meta_lead_id,
                message_type='comment', subtype_xmlid='mail.mt_note')
        return True

    def _schedule_attention_activity(self, summary):
        """One open activity per queue record for the fallback inbox user
        when an event needs human attention (terminal failure or
        ambiguous). Honors the crm_meta_lead_ads.queue_notify switch."""
        self.ensure_one()
        if self.env['ir.config_parameter'].sudo().get_param(
                'crm_meta_lead_ads.queue_notify', 'True') in ('False', '0', 'false'):
            return
        configured = self.env['ir.config_parameter'].sudo().get_param(
            'crm_meta_lead_ads.inbox_default_user_id')
        try:
            user = self.env['res.users'].sudo().browse(int(configured)).exists()
        except (TypeError, ValueError):
            user = self.env['res.users'].browse()
        if not user:
            return
        todo = self.env.ref('mail.mail_activity_data_todo')
        model_id = self.env['ir.model']._get_id(self._name)
        existing = self.env['mail.activity'].sudo().search([
            ('res_model_id', '=', model_id), ('res_id', '=', self.id),
            ('activity_type_id', '=', todo.id),
        ], limit=1)
        if not existing:
            self.env['mail.activity'].sudo().create({
                'activity_type_id': todo.id, 'res_model_id': model_id,
                'res_id': self.id, 'user_id': user.id, 'summary': summary,
                'date_deadline': fields.Date.today(),
            })
