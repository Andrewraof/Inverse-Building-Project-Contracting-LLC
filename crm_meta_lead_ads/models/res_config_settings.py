from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    meta_app_id = fields.Char(config_parameter='crm_meta_lead_ads.app_id')
    meta_app_secret = fields.Char(config_parameter='crm_meta_lead_ads.app_secret')
    meta_verify_token = fields.Char(config_parameter='crm_meta_lead_ads.verify_token')
    meta_api_version = fields.Char(config_parameter='crm_meta_lead_ads.api_version', default='v25.0')
    meta_http_timeout = fields.Integer(config_parameter='crm_meta_lead_ads.http_timeout', default=15)
    meta_max_attempts = fields.Integer(config_parameter='crm_meta_lead_ads.max_attempts', default=8)
    meta_privacy_policy_url = fields.Char(config_parameter='crm_meta_lead_ads.privacy_policy_url')
    meta_deletion_status_base_url = fields.Char(config_parameter='crm_meta_lead_ads.deletion_status_base_url')
