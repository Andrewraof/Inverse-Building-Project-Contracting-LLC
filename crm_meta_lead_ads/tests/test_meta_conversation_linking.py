import threading
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase
from odoo.modules.registry import Registry


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

    def test_lead_field_is_readonly_in_conversation_form(self):
        view = self.env.ref('crm_meta_lead_ads.view_meta_conversation_form')
        self.assertIn('name="lead_id"', view.arch_db)
        self.assertIn('name="lead_id" readonly="1"', view.arch_db)

    def test_direct_lead_write_keeps_both_links_consistent(self):
        conv = self._conversation()
        lead = self._lead()
        conv.write({'lead_id': lead.id})
        self.assertEqual(conv.lead_id, lead)
        self.assertEqual(lead.meta_conversation_id, conv)
        with self.assertRaises(UserError):
            conv.write({'lead_id': False})
        self.assertEqual(conv.lead_id, lead)

    def test_direct_create_with_lead_keeps_both_links_consistent(self):
        lead = self._lead()
        conv = self._conversation('direct-create', lead_id=lead.id)
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

    def test_link_wizard_rejects_other_salespersons_lead(self):
        sales_group = self.env.ref('sales_team.group_sale_salesman')
        meta_group = self.env.ref('crm_meta_lead_ads.group_meta_lead_user')
        user = self.env['res.users'].create({
            'name': 'Restricted Link User', 'login': 'restricted-link@test',
            'company_id': self.env.company.id,
            'company_ids': [(4, self.env.company.id)],
            'group_ids': [(4, meta_group.id), (4, sales_group.id)],
        })
        conv = self._conversation('restricted-link')
        lead = self._lead('Other Salesperson Lead', user_id=self.env.user.id)
        with self.assertRaises(Exception) as raised:
            self._wizard(conv, lead).with_user(user).action_link()
        self.assertIsInstance(raised.exception, (AccessError, UserError))
        self.assertFalse(conv.lead_id)
        self.assertFalse(lead.meta_conversation_id)

    def test_link_wizard_rejects_unavailable_company_as_user(self):
        other_company = self.env['res.company'].create({'name': 'Unavailable Link Co'})
        user = self.env['res.users'].create({
            'name': 'Company Restricted Link User',
            'login': 'company-restricted-link@test',
            'company_id': self.env.company.id,
            'company_ids': [(4, self.env.company.id)],
            'group_ids': [
                (4, self.env.ref('crm_meta_lead_ads.group_meta_lead_user').id),
                (4, self.env.ref('sales_team.group_sale_salesman').id),
            ],
        })
        conv = self._conversation('company-restricted-link')
        lead = self._lead('Unavailable Company Lead', company_id=other_company.id)
        with self.assertRaises(Exception) as raised:
            self._wizard(conv, lead).with_user(user).action_link()
        self.assertIsInstance(raised.exception, (AccessError, UserError))
        self.assertFalse(conv.lead_id)
        self.assertFalse(lead.meta_conversation_id)


class TestMetaConversationConcurrentLink(TransactionCase):
    def test_committed_competing_link_is_not_overwritten(self):
        """A link waiting on a lead row must re-read the winner after the wait."""
        registry = Registry(self.env.cr.dbname)
        suffix = uuid.uuid4().hex
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env['meta.account'].create({
                'name': 'Concurrent Link', 'company_id': self.env.company.id,
                'app_id': 'app-' + suffix, 'app_secret': 'test-secret',
            })
            page = env['meta.page'].create({
                'name': 'Concurrent Link Page', 'company_id': self.env.company.id,
                'account_id': account.id, 'meta_page_id': 'page-' + suffix,
                'page_access_token': 'test-page-token',
            })
            conv_a, conv_b = env['meta.conversation'].create([
                {'company_id': self.env.company.id, 'page_id': page.id,
                 'psid': 'a-' + suffix},
                {'company_id': self.env.company.id, 'page_id': page.id,
                 'psid': 'b-' + suffix},
            ])
            lead = env['crm.lead'].create({
                'name': 'Concurrent Lead', 'company_id': self.env.company.id,
            })
            ids = (account.id, page.id, conv_a.id, conv_b.id, lead.id)
            cr.commit()

        account_id, page_id, conv_a_id, conv_b_id, lead_id = ids
        locked = threading.Event()
        release = threading.Event()
        worker_errors = []

        def competing_transaction():
            try:
                with registry.cursor() as cr:
                    cr.execute('SELECT id FROM crm_lead WHERE id = %s FOR UPDATE',
                               (lead_id,))
                    locked.set()
                    if not release.wait(timeout=30):
                        raise AssertionError('competing transaction was not released')
                    cr.execute('UPDATE crm_lead SET meta_conversation_id = %s WHERE id = %s',
                               (conv_b_id, lead_id))
                    cr.execute('UPDATE meta_conversation SET lead_id = %s WHERE id = %s',
                               (lead_id, conv_b_id))
                    cr.commit()
            except Exception as exc:
                worker_errors.append(exc)
                locked.set()

        worker = threading.Thread(target=competing_transaction)
        timer = None
        try:
            # Use a fresh transaction: the TransactionCase cursor started
            # before the independently committed fixture and cannot see it.
            with registry.cursor() as main_cr:
                env = api.Environment(main_cr, SUPERUSER_ID, {})
                conv_a = env['meta.conversation'].browse(conv_a_id)
                lead = env['crm.lead'].browse(lead_id)
                self.assertFalse(lead.meta_conversation_id)  # prime cache
                worker.start()
                self.assertTrue(locked.wait(timeout=30))
                self.assertFalse(worker_errors)
                timer = threading.Timer(1, release.set)
                timer.start()
                rejected = False
                retry_needed = False
                try:
                    conv_a._link_to_lead(lead)
                except UserError:
                    rejected = True
                except SerializationFailure:
                    # Odoo's request layer retries this transaction with
                    # a fresh snapshot after the competing commit.
                    retry_needed = True
                finally:
                    # Never commit the contender's attempted link.
                    main_cr.rollback()
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive())
            self.assertFalse(worker_errors)
            if retry_needed:
                with registry.cursor() as retry_cr:
                    env = api.Environment(retry_cr, SUPERUSER_ID, {})
                    with self.assertRaises(UserError):
                        env['meta.conversation'].browse(conv_a_id)._link_to_lead(
                            env['crm.lead'].browse(lead_id))
                    retry_cr.rollback()
                rejected = True
            self.assertTrue(rejected, 'A competing committed link was overwritten')
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                lead = env['crm.lead'].browse(lead_id)
                conv_a = env['meta.conversation'].browse(conv_a_id)
                self.assertEqual(lead.meta_conversation_id.id, conv_b_id)
                self.assertFalse(conv_a.lead_id)
        finally:
            release.set()
            if timer:
                timer.cancel()
            if worker.ident is not None:
                worker.join(timeout=30)
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env['crm.lead'].browse(lead_id).unlink()
                env['meta.conversation'].browse([conv_a_id, conv_b_id]).unlink()
                env['meta.page'].browse(page_id).unlink()
                env['meta.account'].browse(account_id).unlink()
                cr.commit()
