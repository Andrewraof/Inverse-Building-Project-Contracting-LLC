import logging
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class MetaRoutingRule(models.Model):
    _name = 'meta.routing.rule'
    _description = 'Meta Lead/Conversation Routing Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    applies_on = fields.Selection([
        ('lead', 'Meta Lead'), ('conversation', 'Meta Conversation'),
    ], default='lead', required=True, index=True)
    # Conditions — every set condition must match (AND); empty = wildcard.
    page_id = fields.Many2one('meta.page', index=True)
    form_id = fields.Many2one('meta.form', index=True)
    meta_campaign_id = fields.Char()
    meta_adset_id = fields.Char()
    meta_ad_id = fields.Char()
    meta_platform = fields.Selection([('facebook', 'Facebook'), ('instagram', 'Instagram')])
    city = fields.Char(help='Case-insensitive contains match on the mapped city value.')
    service = fields.Char(help='Case-insensitive contains match on the mapped service value.')
    keyword = fields.Char(help='Case-insensitive contains match inside one answer.')
    keyword_field = fields.Char(help='Meta question key the keyword is searched in.')
    # Outcome
    team_id = fields.Many2one('crm.team')
    user_id = fields.Many2one('res.users')
    priority = fields.Selection([('0', 'Normal'), ('1', 'Low'), ('2', 'High'), ('3', 'Very High')])
    tag_ids = fields.Many2many('crm.tag')
    lead_type = fields.Selection([('lead', 'Lead'), ('opportunity', 'Opportunity')])
    activity_type_id = fields.Many2one('mail.activity.type')
    activity_delay = fields.Integer(default=0, help='Days until the follow-up activity is due.')
    override_manual = fields.Boolean(
        default=False,
        help='Off: never replace an already assigned salesperson/team. '
             'On: the rule may replace them.')

    @api.constrains('company_id', 'page_id', 'form_id', 'team_id', 'user_id')
    def _check_routing_company(self):
        for rule in self:
            if rule.page_id and rule.page_id.company_id != rule.company_id:
                raise ValidationError(_('The routing page belongs to another company.'))
            if rule.form_id and rule.form_id.company_id != rule.company_id:
                raise ValidationError(_('The routing form belongs to another company.'))
            if rule.form_id and rule.page_id and rule.form_id.page_id != rule.page_id:
                raise ValidationError(_('The routing form does not belong to the selected page.'))
            if rule.team_id and rule.team_id.company_id and rule.team_id.company_id != rule.company_id:
                raise ValidationError(_('The routing sales team belongs to another company.'))
            if rule.user_id and rule.company_id not in rule.user_id.company_ids:
                raise ValidationError(_('The routing user has no access to this company.'))

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def _matches(self, context):
        """All set conditions must match. ``context`` keys: page_id,
        form_id, campaign_id, adset_id, ad_id, platform, answers (dict of
        Meta question key -> joined answer text)."""
        self.ensure_one()
        answers = context.get('answers') or {}

        def answer_value(key):
            return (answers.get(key) or '').lower()

        if self.page_id and self.page_id.id != context.get('page_id'):
            return False
        if self.form_id and self.form_id.id != context.get('form_id'):
            return False
        for fname, key in (('meta_campaign_id', 'campaign_id'),
                           ('meta_adset_id', 'adset_id'),
                           ('meta_ad_id', 'ad_id')):
            if self[fname] and str(self[fname]) != str(context.get(key) or ''):
                return False
        if self.meta_platform and self.meta_platform != context.get('platform'):
            return False
        if self.city and self.city.lower() not in answer_value('city'):
            return False
        if self.service and self.service.lower() not in answer_value('service'):
            return False
        if self.keyword:
            haystack = answer_value(self.keyword_field) if self.keyword_field \
                else ' '.join(str(v).lower() for v in answers.values())
            if self.keyword.lower() not in haystack:
                return False
        return True

    @api.model
    def _find_rule(self, company, applies_on, context):
        """First matching active rule by sequence (first-match wins)."""
        rules = self.search([
            ('company_id', '=', company.id), ('applies_on', '=', applies_on),
            ('active', '=', True),
        ], order='sequence, id')
        for rule in rules:
            if rule._matches(context):
                return rule
        return self.browse()

    # ------------------------------------------------------------------
    # Applying
    # ------------------------------------------------------------------
    @api.model
    def apply_for_lead(self, lead, queue=None, answers=None):
        """Apply the first matching rule to a freshly created lead.
        Assignment (user/team) is only written when the field is empty,
        unless the rule explicitly allows overriding manual assignment.
        Returns the applied rule (or empty)."""
        context = {
            'page_id': queue.page_id.id if queue else False,
            'form_id': queue.form_id.id if queue and queue.form_id else False,
            'campaign_id': lead.meta_campaign_id,
            'adset_id': lead.meta_adset_id,
            'ad_id': lead.meta_ad_id,
            'platform': lead.meta_platform,
            'answers': answers or {},
        }
        rule = self._find_rule(lead.company_id, 'lead', context)
        if not rule:
            return rule
        vals = {}
        if rule.team_id and (rule.override_manual or not lead.team_id):
            vals['team_id'] = rule.team_id.id
        if rule.user_id and (rule.override_manual or not lead.user_id):
            vals['user_id'] = rule.user_id.id
        if rule.priority:
            vals['priority'] = rule.priority
        if rule.lead_type and lead.type == 'lead' and rule.lead_type == 'opportunity':
            vals['type'] = 'opportunity'
        if rule.tag_ids:
            vals['tag_ids'] = [(4, tag.id) for tag in rule.tag_ids]
        vals['meta_routing_rule_id'] = rule.id
        lead.write(vals)
        if rule.activity_type_id:
            lead.activity_schedule(
                activity_type_id=rule.activity_type_id.id,
                summary=_('Meta routing follow-up: %s') % rule.name,
                user_id=(rule.user_id or lead.user_id or self.env.user).id,
                date_deadline=fields.Date.today() + timedelta(days=rule.activity_delay),
            )
        lead.message_post(
            body=_('Meta routing rule applied: %s') % rule.name,
            message_type='comment', subtype_xmlid='mail.mt_note')
        _logger.info(
            'Meta routing rule %s applied to CRM lead %s (queue %s).',
            rule.id, lead.id, queue.id if queue else '-')
        return rule

    @api.model
    def apply_for_conversation(self, conversation):
        """Suggest an assignee for a conversation that has none. Never
        replaces a manually chosen assignee."""
        if conversation.assigned_user_id:
            return self.browse()
        context = {'page_id': conversation.page_id.id, 'answers': {}}
        rule = self._find_rule(conversation.company_id, 'conversation', context)
        if rule and rule.user_id:
            conversation.assigned_user_id = rule.user_id.id
            _logger.info(
                'Meta routing rule %s assigned conversation %s.',
                rule.id, conversation.id)
            return rule
        return self.browse()

    # ------------------------------------------------------------------
    # Preview (read-only)
    # ------------------------------------------------------------------
    def action_preview_matches(self):
        """Open the queue records this rule would match — no data is
        modified. For conversation rules, opens matching conversations."""
        self.ensure_one()
        if self.applies_on == 'conversation':
            domain = [('company_id', '=', self.company_id.id)]
            if self.page_id:
                domain.append(('page_id', '=', self.page_id.id))
            return {
                'type': 'ir.actions.act_window', 'name': _('Preview: %s') % self.name,
                'res_model': 'meta.conversation', 'domain': domain,
                'view_mode': 'list,form', 'target': 'current',
            }
        domain = [('company_id', '=', self.company_id.id)]
        if self.page_id:
            domain.append(('page_id', '=', self.page_id.id))
        if self.form_id:
            domain.append(('form_id', '=', self.form_id.id))
        return {
            'type': 'ir.actions.act_window', 'name': _('Preview: %s') % self.name,
            'res_model': 'meta.lead.queue', 'domain': domain,
            'view_mode': 'list,form', 'target': 'current',
        }
