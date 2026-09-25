from odoo.tests import Form, new_test_user
from odoo.tests.common import TransactionCase


class TestMetaLeadAccess(TransactionCase):
    """A plain CRM salesperson without any Meta Lead Ads group must be able
    to open and edit pipeline leads, including leads carrying Meta identity
    links, without an AccessError on meta.lead.identity."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.salesman = new_test_user(
            cls.env, login='meta_access_salesman',
            groups='base.group_user,sales_team.group_sale_salesman')
        cls.meta_user = new_test_user(
            cls.env, login='meta_access_meta_user',
            groups='base.group_user,sales_team.group_sale_salesman,'
                   'crm_meta_lead_ads.group_meta_lead_user')
        cls.lead = cls.env['crm.lead'].create({
            'name': 'Meta Access Lead', 'type': 'opportunity',
            'user_id': cls.salesman.id, 'meta_lead_id': 'access-1',
        })
        cls.env['meta.lead.identity'].create({
            'company_id': cls.lead.company_id.id, 'crm_lead_id': cls.lead.id,
            'meta_lead_id': 'access-1', 'match_type': 'created',
        })

    def test_salesman_without_meta_group_can_edit_lead(self):
        self.assertFalse(self.salesman.has_group('crm_meta_lead_ads.group_meta_lead_user'))
        lead = self.lead.with_user(self.salesman)
        self.assertEqual(lead.meta_identity_count, 1)
        with Form(lead) as form:
            form.name = 'Meta Access Lead Edited'
        self.assertEqual(self.lead.name, 'Meta Access Lead Edited')

    def test_meta_user_still_sees_identity_links(self):
        lead = self.lead.with_user(self.meta_user)
        self.assertEqual(lead.meta_identity_count, 1)
        self.assertEqual(lead.meta_identity_ids.meta_lead_id, 'access-1')
        with Form(lead) as form:
            form.name = 'Meta Access Lead By Meta User'
        self.assertEqual(self.lead.name, 'Meta Access Lead By Meta User')
