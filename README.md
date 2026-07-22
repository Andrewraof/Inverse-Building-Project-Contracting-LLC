# Inverse Building Project Contracting LLC — Dubai HVAC Sales Extension

Independent Dubai-project deployment of the `hvac_sales_extension` Odoo module,
forked from the Sunderland `andrew-sunderland` repository. This repository
contains **only** the `hvac_sales_extension` module — no other Sunderland
addon is included here.

## Module purpose

Adds HVAC-specific sales/contract functionality on top of standard Odoo
Sales:

- HVAC specs (BTU/CFM/kW/HP) on products and sale order lines.
- Project-level HVAC totals on the sale order.
- A contract workflow stage (draft → sent → contract → sale).
- A dynamic, bilingual (EN/AR) contract clause library, editable at
  **Sales → Configuration → HVAC → Contract Clauses**.
- A language-selection wizard when printing a quotation/contract.
- Separate pure-English and pure-Arabic PDF report templates.
- Report header/footer branding driven entirely by the active company's
  record (logo, name, address, phone, email, website) — no hardcoded
  company branding.

## Odoo version

Odoo **19.0 Community**.

## Dependencies

- `sale_management`
- `product`

No other module dependency. Does not require, and is not coupled to, any
other Sunderland addon (`project_budget_boq_sunderland`, HR modules, site
attendance modules, etc.).

## Installation

1. Place `hvac_sales_extension/` on an addons path visible to your Odoo 19
   instance.
2. Update the apps list (Settings → Update Apps List).
3. Install **HVAC Sales Extension (Inverse Building Project Contracting
   LLC — Dubai)**.

## Configuration

1. **Company branding** (see below) — set the company's logo, name, and
   contact details once; every report picks them up automatically.
2. **Contract clauses** — review and adjust the seeded clause library at
   Sales → Configuration → HVAC → Contract Clauses. See "Legal clauses
   requiring UAE review" below before using this module for real
   contracts.
3. **HVAC brands / gas types / transaction types** — configure master
   data under Sales → Configuration → HVAC as needed.

### Configuring the Dubai company logo and contact information

The report header and footer are driven entirely by Odoo's own company
record — nothing is hardcoded in this module. To brand reports for
Inverse Building Project Contracting LLC:

1. Go to **Settings → Companies**, open (or create) the company record
   for Inverse Building Project Contracting LLC.
2. Set the **Logo** — it renders in the report header.
3. Set **Name**, **Street / Street2 / City / Country**, **Phone**,
   **Email**, and **Website** — these render in the report footer.
4. No further action is needed; both the EN and AR contract/quotation
   PDF templates use the same shared layout (`hvac_contract_layout`) and
   will reflect the active company automatically.

## Legal clauses requiring UAE review

**Do not use this module for real contracts before UAE legal counsel has
reviewed the items below.** Per explicit instruction, no Kuwait-specific
legal wording has been rewritten or replaced with fabricated UAE/Dubai
wording in this fork — it has been left exactly as upstream, for a
qualified reviewer to update deliberately.

1. **Governing Law & Jurisdiction paragraph** — hardcoded directly in the
   PDF report templates (not part of the editable clause library):
   `reports/hvac_contract_template_en.xml`,
   `reports/hvac_contract_template_ar.xml`,
   `reports/hvac_quotation_template_en.xml`,
   `reports/hvac_quotation_template_ar.xml`. States that the contract is
   governed by the laws of, and subject to the courts of, the **State of
   Kuwait** ("دولة الكويت"). Must be replaced with the correct UAE/Dubai
   governing-law and jurisdiction wording.
2. **Article 2 — Temperature Guarantee** (`hvac_clause_art2` in
   `data/hvac_contract_clause_data.xml`, editable via the Contract
   Clauses screen): references compliance with **MEW** (Kuwait's
   Ministry of Electricity and Water) building/insulation
   specifications. Needs the equivalent applicable UAE/Dubai standard
   (e.g. DEWA or the relevant municipality code).
3. **"Works Not Included" sub-clause 5** (`hvac_clause_art8_5`, child of
   Article 8, same data file): "Compliance with MEW regulations for
   building and thermal insulation." — same MEW (Kuwait) reference as
   above, needs the UAE/Dubai equivalent.
4. `reports/hvac_contract_templates.xml` is present upstream but is
   **not** registered in `__manifest__.py`'s `data` list, so it is never
   loaded by Odoo. It also contains the same Kuwait jurisdiction wording
   (an older/duplicate draft). Left untouched since it is inert; flagged
   here for completeness.

None of the above were rewritten in this fork. The dynamic clause engine
itself (add/edit/reorder clauses, parent/child structure, EN/AR fields)
is fully preserved and usable for the UAE review.

## Source repository and commit

- Source repository: `Andrewraof/andrew-sunderland`
- Source branch: `main`
- Source module: `hvac_sales_extension`
- Upstream module version: `19.0.4.0.0`
- Exact copied commit: `db79fafbe42ed8d5c0b9dae170b7cf3f7feb1323`

See `UPSTREAM.md` for full provenance and how to compare future upstream
changes.

## Dubai fork version

`19.0.4.0.1` — first Dubai-specific branding revision of the upstream
`19.0.4.0.0` module.
