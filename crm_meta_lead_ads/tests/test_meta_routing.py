from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestMetaRouting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Rule = cls.env['meta.routing.rule']
        cls.account = cls.env['meta.account'].create({
            'name': 'Routing Meta', 'company_id': cls.env.company.id,
            'app_id': 'app-rt', 'app_secret': 'secret-rt',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Routing Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '800',
            'page_access_token': 'pagetok-RT',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Routing Form', 'company_id': cls.env.company.id,
            'page_id': cls.page.id, 'meta_form_id': 'FRT',
        })
        cls.team = cls.env['crm.team'].create({'name': 'Routing Team'})
        cls.agent = cls.env['res.users'].create({
            'name': 'Routing Agent', 'login': 'routing-agent@test',
            'company_id': cls.env.company.id, 'company_ids': [(4, cls.env.company.id)],
        })

    def _queue(self, **extra):
        vals = {'company_id': self.env.company.id, 'page_id': self.page.id,
                'form_id': self.form.id, 'meta_lead_id': extra.pop('meta_lead_id', 'QRT1')}
        vals.update(extra)
        return self.env['meta.lead.queue'].create(vals)

    def _lead(self, **extra):
        vals = {'name': 'RT Lead', 'type': 'lead', 'company_id': self.env.company.id}
        vals.update(extra)
        return self.env['crm.lead'].create(vals)

    # 1. Page condition matches and assigns team/user.
    def test_page_condition_assigns(self):
        self.Rule.create({
            'name': 'Page rule', 'page_id': self.page.id,
            'team_id': self.team.id, 'user_id': self.agent.id,
        })
        lead = self._lead()
        rule = self.Rule.apply_for_lead(lead, queue=self._queue())
        self.assertTrue(rule)
        self.assertEqual(lead.team_id, self.team)
        self.assertEqual(lead.user_id, self.agent)
        self.assertEqual(lead.meta_routing_rule_id, rule)

    # 2. Campaign ID condition.
    def test_campaign_condition(self):
        self.Rule.create({'name': 'Camp', 'meta_campaign_id': 'C1', 'user_id': self.agent.id})
        hit = self._lead(meta_campaign_id='C1')
        miss = self._lead(meta_campaign_id='C2')
        self.assertTrue(self.Rule.apply_for_lead(hit))
        self.assertFalse(self.Rule.apply_for_lead(miss))

    # 3. Keyword condition searches the chosen question's answer.
    def test_keyword_condition(self):
        self.Rule.create({
            'name': 'KW', 'keyword': 'villa', 'keyword_field': 'project_type',
            'user_id': self.agent.id,
        })
        lead = self._lead()
        self.assertTrue(self.Rule.apply_for_lead(
            lead, answers={'project_type': 'Villa renovation'}))
        self.assertFalse(self.Rule.apply_for_lead(
            self._lead(), answers={'project_type': 'Office fitout'}))

    # 4. First rule by sequence wins.
    def test_sequence_first_match_wins(self):
        second = self.Rule.create({
            'name': 'Second', 'sequence': 20, 'page_id': self.page.id,
            'user_id': self.agent.id,
        })
        first = self.Rule.create({
            'name': 'First', 'sequence': 5, 'page_id': self.page.id,
            'team_id': self.team.id,
        })
        lead = self._lead()
        applied = self.Rule.apply_for_lead(lead, queue=self._queue())
        self.assertEqual(applied, first)
        self.assertNotEqual(applied, second)

    # 5. A manually assigned salesperson is never replaced by default.
    def test_no_manual_override_by_default(self):
        other = self.env['res.users'].create({
            'name': 'Manual Agent', 'login': 'manual-agent@test',
            'company_id': self.env.company.id, 'company_ids': [(4, self.env.company.id)],
        })
        self.Rule.create({'name': 'R', 'page_id': self.page.id, 'user_id': self.agent.id})
        lead = self._lead(user_id=other.id)
        self.Rule.apply_for_lead(lead, queue=self._queue())
        self.assertEqual(lead.user_id, other)

    # 6. override_manual allows replacement.
    def test_override_manual(self):
        other = self.env['res.users'].create({
            'name': 'Manual Agent 2', 'login': 'manual-agent-2@test',
            'company_id': self.env.company.id, 'company_ids': [(4, self.env.company.id)],
        })
        self.Rule.create({
            'name': 'R2', 'page_id': self.page.id, 'user_id': self.agent.id,
            'override_manual': True,
        })
        lead = self._lead(user_id=other.id)
        self.Rule.apply_for_lead(lead, queue=self._queue())
        self.assertEqual(lead.user_id, self.agent)

    # 7. Company isolation: rules of another company never apply.
    def test_company_isolation(self):
        company_b = self.env['res.company'].create({'name': 'RT Co B'})
        self.Rule.create({
            'name': 'Foreign', 'company_id': company_b.id,
        })
        lead = self._lead()
        self.assertFalse(self.Rule.apply_for_lead(lead))
        self.assertFalse(lead.meta_routing_rule_id)

    def test_rejects_foreign_page_form_team_and_user(self):
        company_b = self.env['res.company'].create({'name': 'RT Foreign B'})
        foreign_account = self.env['meta.account'].create({
            'name': 'Foreign Meta', 'company_id': company_b.id,
            'app_id': 'foreign-app', 'app_secret': 'foreign-secret',
        })
        foreign_page = self.env['meta.page'].create({
            'name': 'Foreign Page', 'company_id': company_b.id,
            'account_id': foreign_account.id, 'meta_page_id': '801',
            'page_access_token': 'foreign-page-token',
        })
        foreign_form = self.env['meta.form'].create({
            'name': 'Foreign Form', 'company_id': company_b.id,
            'page_id': foreign_page.id, 'meta_form_id': 'FRT-B',
        })
        foreign_team = self.env['crm.team'].create({
            'name': 'Foreign Team', 'company_id': company_b.id,
        })
        foreign_user = self.env['res.users'].create({
            'name': 'Foreign Agent', 'login': 'routing-foreign@test',
            'company_id': company_b.id, 'company_ids': [(6, 0, [company_b.id])],
        })
        for field_name, record in (
                ('page_id', foreign_page), ('form_id', foreign_form),
                ('team_id', foreign_team), ('user_id', foreign_user)):
            with self.subTest(field=field_name), self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.Rule.create({'name': 'Cross-company', field_name: record.id})

    # 8. Conversation assignment: rule fills an empty assignee only.
    def test_conversation_assignment(self):
        rule = self.Rule.create({
            'name': 'Conv', 'applies_on': 'conversation',
            'page_id': self.page.id, 'user_id': self.agent.id,
        })
        conv = self.env['meta.conversation'].create({
            'company_id': self.env.company.id, 'page_id': self.page.id,
            'psid': 'PS-RT1',
        })
        applied = self.Rule.apply_for_conversation(conv)
        self.assertEqual(applied, rule)
        self.assertEqual(conv.assigned_user_id, self.agent)
        # An already assigned conversation is untouched.
        conv.assigned_user_id = self.env.user
        self.assertFalse(self.Rule.apply_for_conversation(conv))
        self.assertEqual(conv.assigned_user_id, self.env.user)

    # 9. Priority, tags and lead type are applied.
    def test_outcome_fields(self):
        tag = self.env['crm.lead.tag'].create({'name': 'MetaHot'})
        todo = self.env.ref('mail.mail_activity_data_todo')
        self.Rule.create({
            'name': 'Full', 'page_id': self.page.id, 'priority': '3',
            'tag_ids': [(4, tag.id)], 'lead_type': 'opportunity',
            'activity_type_id': todo.id, 'activity_delay': 2,
        })
        lead = self._lead()
        self.Rule.apply_for_lead(lead, queue=self._queue())
        self.assertEqual(lead.priority, '3')
        self.assertIn(tag, lead.tag_ids)
        self.assertEqual(lead.type, 'opportunity')
        self.assertTrue(lead.activity_ids)
