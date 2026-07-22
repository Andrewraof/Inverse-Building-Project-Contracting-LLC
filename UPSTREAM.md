# Upstream tracking

This repository is an independent Dubai-project copy of a single module
taken from a larger Sunderland monorepo. It is **not** a Git fork/clone of
that repository — only the `hvac_sales_extension` module's files were
copied, then de-branded for Inverse Building Project Contracting LLC.

| | |
|---|---|
| Upstream repository | `Andrewraof/andrew-sunderland` |
| Upstream branch | `main` |
| Upstream module | `hvac_sales_extension` |
| Upstream module version at copy time | `19.0.4.0.0` |
| Exact copied commit | `db79fafbe42ed8d5c0b9dae170b7cf3f7feb1323` |
| Dubai fork version | `19.0.4.0.1` |

## What changed relative to upstream

- Version bumped `19.0.4.0.0` → `19.0.4.0.1`.
- Report header/footer no longer use the hardcoded
  `sunderland_bar.png` / `sunderland_contact.png` images (removed from
  `static/src/img/`); `reports/hvac_contract_layout.xml` now renders the
  **active company's** logo, name, address, phone, email and website
  dynamically instead.
- The "Sunderland Contract Company" copyright header in 5 Python files
  was replaced with a generic upstream-fork notice referencing this file.
- Manifest `name`, `summary`, `description` updated to identify this as
  the Inverse Building Project Contracting LLC — Dubai fork; version
  bumped as above.
- **Not changed**: technical module/folder name (`hvac_sales_extension`),
  model names, XML IDs, report IDs, Python package structure, the
  Kuwait-specific legal wording (see README.md's "Legal clauses requiring
  UAE review" — left untouched deliberately, not rewritten with fabricated
  UAE text), the dynamic clause engine, EN/AR reports, HVAC fields/totals,
  contract/quotation workflow, language-selection wizard, signatures,
  report actions, or security/access rights.

## Comparing future upstream updates

Since this is a copy (not a git remote/fork), there is no `git fetch`
against `andrew-sunderland` to pull updates automatically. To check what
has changed upstream since this copy was taken:

1. Clone (or update a local clone of) `Andrewraof/andrew-sunderland` at
   `main`.
2. Diff its `hvac_sales_extension/` directory against this repository's
   `hvac_sales_extension/` directory, e.g.:

   ```bash
   git clone --depth 1 https://github.com/Andrewraof/andrew-sunderland /tmp/andrew-sunderland
   diff -rq /tmp/andrew-sunderland/hvac_sales_extension \
            ./hvac_sales_extension
   ```

3. Ignore the expected, permanent Dubai-specific differences listed above
   (branding, version number, license header, the two removed images).
4. For any other diff, decide deliberately whether to port it into this
   fork — do not blindly overwrite `hvac_sales_extension/` with the
   upstream copy, since it would silently reintroduce Sunderland branding
   and could overwrite Dubai-specific customizations made after this
   copy.
5. If porting a change that touches the report templates or the clause
   data, re-check it doesn't reintroduce Kuwait-specific wording without
   updating it for the UAE, and re-run the same quality checks used when
   this fork was created (Python compile, XML parse, CSV validation,
   manifest parse, a `Sunderland` reference search, `git diff --check`).
