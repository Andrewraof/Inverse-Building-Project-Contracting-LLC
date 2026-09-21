# Meta Lead Ads CRM Connector — Odoo 19 Community

Technical module: `crm_meta_lead_ads`

## Included
- Meta OAuth connection for managed Facebook Pages
- Page webhook subscription (`leadgen`)
- HMAC-SHA256 verification of incoming webhooks
- Buffered ingestion queue and exponential retry
- PostgreSQL-level idempotency for queue rows and CRM leads
- Dynamic per-form field mapping
- Facebook / Instagram UTM sources
- Hourly recovery polling for missed leads
- Multi-company record rules and company-bound configuration
- Token invalidation handling and administrator activity alert
- Immutable-style audit interface / payload logs
- Meta user-data deletion callback and status URL
- Odoo CRM lead traceability fields

## Installation
1. Copy `crm_meta_lead_ads` into an Odoo 19 addons path.
2. Restart Odoo and update Apps List.
3. Install **Meta Lead Ads CRM Connector**.
4. Add administrators to **Meta Lead Ads Manager**.
5. Open Settings > Meta Lead Ads and set App ID, App Secret, Verify Token and Graph API version.
6. Set Meta Webhook Callback URL to `https://YOUR_DOMAIN/meta_crm/webhook` and subscribe to `leadgen`.
7. Set Meta Data Deletion Callback URL to `https://YOUR_DOMAIN/meta_crm/data_deletion`.
8. Save settings and click **Connect Facebook Page**.
9. Open CRM > Meta Lead Ads > Pages; subscribe the desired Page and sync forms.
10. Open Forms & Mapping; generate mappings and adjust custom-question destinations.

## Production notes
- HTTPS is mandatory for public Meta callbacks.
- On self-hosted Odoo, enable `proxy_mode = True` behind Nginx.
- Keep `web.base.url` stable; OAuth callback URLs must match exactly.
- Never expose App Secret or Page Access Tokens to non-system users.
- Graph API version is configurable so the connector can be upgraded without rewriting business logic.

## Meta app review
Live production access is controlled by Meta. Configure only the permissions that your current Meta app/use-case requires and verify them against Meta's current official documentation before submitting App Review.
