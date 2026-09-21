from odoo.tests.common import TransactionCase


class TestMetaConnector(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['meta.account'].create({
            'name': 'Test Meta', 'company_id': cls.env.company.id,
            'app_id': 'app', 'app_secret': 'secret',
        })
        cls.page = cls.env['meta.page'].create({
            'name': 'Test Page', 'company_id': cls.env.company.id,
            'account_id': cls.account.id, 'meta_page_id': '100', 'page_access_token': 'token',
        })
        cls.form = cls.env['meta.form'].create({
            'name': 'Test Form', 'company_id': cls.env.company.id,
            'page_id': cls.page.id, 'meta_form_id': '200',
        })

    def test_queue_idempotency(self):
        q1 = self.env['meta.lead.queue'].enqueue_event(self.env.company, self.page, 'L1', self.form.meta_form_id, {})
        q2 = self.env['meta.lead.queue'].enqueue_event(self.env.company, self.page, 'L1', self.form.meta_form_id, {})
        self.assertEqual(q1.id, q2.id)

    def test_default_mapping_generation(self):
        self.form.action_generate_default_mappings()
        self.assertTrue(self.form.mapping_ids.filtered(lambda m: m.meta_field_name == 'email' and m.odoo_field_name == 'email_from'))
