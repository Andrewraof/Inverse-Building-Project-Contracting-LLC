{
    'name': 'HVAC Sales Extension (Inverse Building Project Contracting LLC — Dubai)',
    'version': '19.0.4.0.1',
    'category': 'Sales',
    'summary': 'HVAC cooling load, dynamic bilingual contract clauses, language-selection wizard, EN/AR PDF reports — Dubai fork for Inverse Building Project Contracting LLC',
    'description': """
        Independent Dubai-project fork for Inverse Building Project Contracting LLC.
        See README.md and UPSTREAM.md for provenance, configuration and the list
        of legal clauses that still require UAE legal review before use.

        - HVAC specs (BTU/CFM/kW/HP) on products and sale order lines
        - Project-level HVAC totals on sale order
        - Contract workflow stage (draft → sent → contract → sale)
        - Dynamic contract clause library (Sales → Config → HVAC → Contract Clauses)
        - Language-selection wizard when printing (English or Arabic)
        - Separate pure EN and pure AR PDF report templates
        - Report header/footer branding driven by the active company's
          logo, name, address, phone, email and website (Settings → Companies)
    """,
    'author': 'Antigravity',
    'depends': [
        'sale_management',
        'product',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/contract_sequence.xml',
        'data/hvac_contract_clause_data.xml',
        'views/hvac_config_views.xml',
        'views/hvac_contract_clause_views.xml',
        'views/hvac_print_contract_wizard_views.xml',
        'views/hvac_print_quotation_wizard_views.xml',
        'views/product_template_views.xml',
        'views/sale_order_views.xml',
        'reports/hvac_contract_layout.xml',
        'reports/hvac_contract_report.xml',
        'reports/hvac_contract_template_en.xml',
        'reports/hvac_contract_template_ar.xml',
        'reports/hvac_quotation_report.xml',
        'reports/hvac_quotation_template_en.xml',
        'reports/hvac_quotation_template_ar.xml',
        'reports/sale_report_branding.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
