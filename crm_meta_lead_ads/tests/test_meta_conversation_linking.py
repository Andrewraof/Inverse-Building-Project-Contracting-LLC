from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestMetaConversationLinking(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Link Meta', 'company_id': cls.env.company.id,
            'app_id': 'app-link', 'app_secret': 'secret-link',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Link Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '970',
            'page_access_token': 'pagetok-LINK',
        })
        cls.Conversation = cls.env['meta.conversation']
        cls.Lead = cls.env['crm.lead']
        cls.Wizard = cls.env['meta.conversation.link.lead.wizard']

    def _conversation(self, psid='link-psid-1', **extra):
        vals = {'company_id': self.env.company.id, 'page_id': self.page.id,
                'psid': psid, 'sender_name': 'Sender %s' % psid}
        vals.update(extra)
        return self.Conversation.create(vals)

    def _lead(self, name='Link Lead', **extra):
        vals = {'name': name, 'type': 'lead', 'company_id': self.env.company.id}
        vals.update(extra)
        return self.Lead.create(vals)

    def _wizard(self, conversation, lead):
        return self.Wizard.create({
            'conversation_id': conversation.id, 'lead_id': lead.id})

    # 1. The wizard links both sides of the relation.
    def test_link_wizard_links_both_sides(self):
        conv = self._conversation()
        lead = self._lead()
        self._wizard(conv, lead).action_link()
        self.assertEqual(conv.lead_id, lead)
        self.assertEqual(lead.meta_conversation_id, conv)

    # 2. A linked conversation cannot be linked again.
    def test_link_refuses_when_conversation_already_linked(self):
        conv = self._conversation()
        lead1 = self._lead('Lead One')
        lead2 = self._lead('Lead Two')
        self._wizard(conv, lead1).action_link()
        with self.assertRaises(UserError):
            self._wizard(conv, lead2).action_link()
        self.assertEqual(conv.lead_id, lead1)

    # 3. Cross-company linking is refused server-side.
    def test_link_refuses_cross_company(self):
        other_company = self.env['res.company'].create({'name': 'Other Co'})
        conv = self._conversation()
        foreign = self._lead('Foreign Lead', company_id=other_company.id)
        with self.assertRaises(UserError):
            self._wizard(conv, foreign).action_link()
        self.assertFalse(conv.lead_id)
        self.assertFalse(foreign.meta_conversation_id)

    # 4. A lead already linked to another conversation is refused.
    def test_link_refuses_lead_linked_to_other_conversation(self):
        conv_a = self._conversation('link-psid-A')
        conv_b = self._conversation('link-psid-B')
        lead = self._lead()
        self._wizard(conv_a, lead).action_link()
        with self.assertRaises(UserError):
            self._wizard(conv_b, lead).action_link()
        self.assertEqual(lead.meta_conversation_id, conv_a)

    # 5. Linking posts an audit note on the lead chatter.
    def test_link_posts_chatter_note(self):
        conv = self._conversation()
        lead = self._lead()
        before = len(lead.message_ids)
        self._wizard(conv, lead).action_link()
        self.assertGreater(len(lead.message_ids), before)

    # 6. action_link_lead opens the wizard for this conversation.
    def test_action_link_lead_opens_wizard(self):
        conv = self._conversation()
        action = conv.action_link_lead()
        self.assertEqual(action['res_model'], 'meta.conversation.link.lead.wizard')
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['context']['default_conversation_id'], conv.id)

    # 7. Creating a lead uses the linked partner's contact details.
    def test_create_lead_uses_partner_details(self):
        partner = self.env['res.partner'].create({
            'name': 'Partner Customer', 'email': 'partner@example.com',
            'phone': '+971501112233',
        })
        conv = self._conversation(partner_id=partner.id)
        lead = conv.action_create_lead()
        self.assertEqual(lead.partner_id, partner)
        self.assertEqual(lead.email_from, 'partner@example.com')
        self.assertEqual(lead.phone, '+971501112233')
        self.assertEqual(conv.lead_id, lead)
        self.assertEqual(lead.meta_conversation_id, conv)

    # 8. Without a partner the sender name stays a display value only.
    def test_create_lead_without_partner(self):
        conv = self._conversation()
        lead = conv.action_create_lead()
        self.assertFalse(lead.partner_id)
        self.assertFalse(lead.email_from)
        self.assertEqual(lead.partner_name, conv.sender_name)

    # 9. A Meta user with sales access can link their own lead.
    # (CRM record rules still apply: linking a lead requires write
    # access to it, same as editing it directly.)
    def test_link_wizard_user_group_access(self):
        user = self.env['res.users'].create({
            'name': 'Link Meta User', 'login': 'link-meta-user@test',
            'company_id': self.env.company.id, 'company_ids': [(4, self.env.company.id)],
            'group_ids': [(4, self.env.ref('crm_meta_lead_ads.group_meta_lead_user').id),
                          (4, self.env.ref('sales_team.group_sale_salesman').id)],
        })
        conv = self._conversation()
        lead = self._lead(user_id=user.id)
        wizard = self._wizard(conv, lead).with_user(user)
        wizard.action_link()
        self.assertEqual(conv.lead_id, lead)
