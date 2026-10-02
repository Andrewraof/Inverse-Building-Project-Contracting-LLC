from datetime import datetime, timezone

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


CUTOVER_KEY = 'crm_meta_lead_ads.contact_review_from'
REVIEW_GROUP = 'crm_meta_lead_ads.group_meta_contact_reviewer'


class MetaContactQueue(models.Model):
    _inherit = 'meta.lead.queue'

    contact_review_required = fields.Boolean(readonly=True, copy=False, index=True)
    review_partner_id = fields.Many2one('res.partner', readonly=True, copy=False,
                                        ondelete='restrict')
    match_result = fields.Selection(selection_add=[('contact_review', 'Contact awaiting review')],
                                    ondelete={'contact_review': 'set null'})

    @api.model_create_multi
    def create(self, vals_list):
        cutoff = self.env['ir.config_parameter'].sudo().get_param(CUTOVER_KEY)
        if cutoff and fields.Datetime.now() >= fields.Datetime.to_datetime(cutoff):
            # Restrict the event immediately, before its customer payload is fetched.
            vals_list = [dict(vals, contact_review_required=True) for vals in vals_list]
        return super().create(vals_list)

    def _requires_contact_review(self, payload):
        self.ensure_one()
        if self.review_partner_id:
            return True
        if not self.contact_review_required:
            return False
        cutoff = self.env['ir.config_parameter'].sudo().get_param(CUTOVER_KEY)
        created = payload.get('created_time')
        if created and cutoff:
            try:
                moment = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
                if moment.tzinfo:
                    moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
                if moment < fields.Datetime.to_datetime(cutoff):
                    # Historical enquiries retain their existing processing path.
                    self.contact_review_required = False
                    return False
            except (ValueError, TypeError):
                pass  # Unknown age is held for management, never auto-assigned.
        return True

    def _save_review_contact(self, payload):
        self.ensure_one()
        # A row lock also serializes a manager retry with a cron worker.
        self.env.cr.execute('SELECT id FROM meta_lead_queue WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['review_partner_id'])
        partner = self.review_partner_id
        if not partner:
            mapped, _form = self._mapping_values(payload)
            # One isolated intake contact per enquiry. Do not hide or modify an
            # existing shared customer merely because their email/phone matches.
            partner = self.env['res.partner'].sudo().with_context(
                tracking_disable=True, mail_create_nosubscribe=True,
                mail_create_nolog=True).create({
                    'name': mapped.get('contact_name') or mapped.get('partner_name')
                            or _('Meta enquiry %s') % self.meta_lead_id,
                    'email': mapped.get('email_from'), 'phone': mapped.get('phone'),
                    'city': mapped.get('city'), 'company_id': self.company_id.id,
                    'user_id': False, 'meta_review_state': 'pending',
                    'meta_review_queue_id': self.id,
                })
            self.review_partner_id = partner
        self.write({'state': 'done', 'match_result': 'contact_review',
                    'processed_at': fields.Datetime.now(), 'error_message': False})
        self._log('info', 'contact_review', 'Enquiry stored for management review.')
        return partner

    def _schedule_attention_activity(self, summary):
        # The old fallback inbox owner may be a salesperson. Intake failures
        # must not disclose the pending enquiry through an activity/email.
        if self.contact_review_required:
            return
        return super()._schedule_attention_activity(summary)


class MetaReviewPartner(models.Model):
    _inherit = 'res.partner'

    meta_review_state = fields.Selection([
        ('pending', 'Pending management review'), ('approved', 'Approved'),
        ('rejected', 'Rejected')], readonly=True, copy=False, index=True)
    meta_review_queue_id = fields.Many2one('meta.lead.queue', readonly=True,
                                          copy=False, ondelete='restrict', index=True)
    meta_review_lead_id = fields.Many2one('crm.lead', readonly=True, copy=False,
                                         ondelete='set null')
    meta_review_user_id = fields.Many2one('res.users', string='Salesperson after approval',
                                         copy=False)
    meta_reviewed_by = fields.Many2one('res.users', readonly=True, copy=False)
    meta_reviewed_at = fields.Datetime(readonly=True, copy=False)

    _unique_meta_review_queue = models.Constraint(
        'UNIQUE(meta_review_queue_id)', 'This enquiry already has an intake contact.')

    def _check_meta_reviewer(self):
        if not (self.env.su or self.env.user.has_group(REVIEW_GROUP)
                or self.env.user.has_group('base.group_system')):
            raise AccessError(_('Only management reviewers can approve Meta contacts.'))
        self.check_access('write')
        if any(p.company_id and p.company_id not in self.env.companies for p in self):
            raise AccessError(_('The contact belongs to another company.'))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(any(k.startswith('meta_review') for k in v) for v in vals_list):
            raise AccessError(_('Meta review contacts are created by the intake process.'))
        return super().create(vals_list)

    def write(self, vals):
        protected = {'meta_review_state', 'meta_review_queue_id', 'meta_review_lead_id',
                     'meta_reviewed_by', 'meta_reviewed_at'}
        if not self.env.su and protected.intersection(vals):
            raise AccessError(_('Use the management review actions to change review status.'))
        if 'meta_review_user_id' in vals:
            self._check_meta_reviewer()
        # Keep pending enquiries private and detached from shared partners/users.
        if any(p.meta_review_state in ('pending', 'rejected') for p in self):
            if {'company_id', 'parent_id', 'user_id'}.intersection(vals):
                raise AccessError(_('Approve the enquiry before changing its ownership.'))
        return super().write(vals)

    def action_meta_approve(self):
        self.ensure_one()
        self._check_meta_reviewer()
        queue = self.meta_review_queue_id
        if not queue:
            raise UserError(_('This contact is not a Meta enquiry.'))
        self.env.cr.execute('SELECT id FROM meta_lead_queue WHERE id = %s FOR UPDATE', (queue.id,))
        self.invalidate_recordset(['meta_review_state', 'meta_review_lead_id'])
        if self.meta_review_state == 'approved':
            return True
        if self.meta_review_state != 'pending':
            raise UserError(_('Only pending enquiries can be approved.'))
        salesperson = self.meta_review_user_id
        if not salesperson or not salesperson.active or salesperson.share:
            raise UserError(_('Choose an active internal salesperson before approval.'))
        if self.company_id not in salesperson.company_ids:
            raise UserError(_('The salesperson must have access to this company.'))
        payload = queue.fetched_payload or {}
        mapped, form = queue._mapping_values(payload)
        source = queue._resolve_source(payload.get('platform'))
        # Deliberately NO sudo: existing lead-creator controls remain authoritative.
        lead = self.env['crm.lead'].with_company(self.company_id).create({
            **mapped, 'name': mapped.get('name') or self.name,
            'contact_name': self.name, 'email_from': self.email, 'phone': self.phone,
            'partner_id': self.id, 'company_id': self.company_id.id, 'type': 'lead',
            'user_id': salesperson.id, 'team_id': form.sales_team_id.id if form else False,
            'source_id': source.id if source else False, 'meta_lead_id': queue.meta_lead_id,
            'meta_page_id': queue.page_id.id, 'meta_form_id': form.id if form else False,
            'meta_platform': payload.get('platform') if payload.get('platform') in ('facebook', 'instagram') else 'unknown',
            'meta_ad_id': payload.get('ad_id'), 'meta_ad_name': payload.get('ad_name'),
            'meta_adset_id': payload.get('adset_id'), 'meta_adgroup_id': payload.get('adset_id'),
            'meta_adset_name': payload.get('adset_name'),
            'meta_campaign_id': payload.get('campaign_id'),
            'meta_campaign_name': payload.get('campaign_name'),
            'meta_is_organic': bool(payload.get('is_organic')),
        })
        # The raw audit field is system-only. Set only this internal field after
        # CRM creation has succeeded with the reviewer's real permissions.
        lead.sudo().write({'meta_raw_payload': payload})
        queue._ensure_identity(lead, 'created')
        queue.sudo().write({'crm_lead_id': lead.id})
        # The authorized transition occurs after successful lead creation, in
        # the same transaction; failure leaves the contact pending and private.
        super(MetaReviewPartner, self.sudo()).write({
            'meta_review_state': 'approved', 'meta_review_lead_id': lead.id,
            'meta_reviewed_by': self.env.uid, 'meta_reviewed_at': fields.Datetime.now(),
            'user_id': salesperson.id,
        })
        return True

    def action_meta_reject(self):
        self._check_meta_reviewer()
        for partner in self:
            queue = partner.meta_review_queue_id
            if queue:
                self.env.cr.execute('SELECT id FROM meta_lead_queue WHERE id = %s FOR UPDATE', (queue.id,))
            partner.invalidate_recordset(['meta_review_state'])
            if partner.meta_review_state != 'pending':
                raise UserError(_('Only pending enquiries can be rejected.'))
            super(MetaReviewPartner, partner.sudo()).write({
                'meta_review_state': 'rejected', 'meta_reviewed_by': self.env.uid,
                'meta_reviewed_at': fields.Datetime.now(),
            })
        return True
