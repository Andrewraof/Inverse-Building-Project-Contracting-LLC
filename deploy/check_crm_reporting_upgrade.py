"""Odoo shell fixture/assertions, ONLY for the named disposable CI database."""
import os

from markupsafe import Markup
from odoo.tools.safe_eval import safe_eval


def run(env, mode):
    if env.cr.dbname != 'meta_connector_reporting_upgrade':
        raise RuntimeError('This fixture is restricted to its disposable CI database')
    Lead = env['crm.lead']
    key = 'reporting-upgrade-fixture'
    if mode == 'prepare':
        lead = Lead.create({
            'name': 'Reporting upgrade fixture', 'meta_lead_id': key,
            'type': 'lead', 'company_id': env.company.id, 'user_id': False,
            'source_id': env.ref('crm_meta_lead_ads.utm_source_facebook').id,
            'description': '<p>Preserved requirements</p>',
        })
        lead.message_post(body=Markup('<p>Preserved internal note</p>'),
                          message_type='comment', subtype_xmlid='mail.mt_note')
        activity = lead.activity_schedule('mail.mail_activity_data_todo',
                                          summary='Completed upgrade fixture')
        activity.action_feedback(feedback='Preserved completed feedback')
        lead.activity_schedule('mail.mail_activity_data_todo',
                               summary='Pending upgrade fixture')
        lead.convert_opportunity(False, False, False)
        env.cr.commit()  # Disposable fixture; never run this on production.
        print('REPORTING_FIXTURE_OK')
        return
    if mode != 'verify':
        raise ValueError('Expected prepare or verify')
    lead = Lead.search([('meta_lead_id', '=', key)])
    assert len(lead) == 1, 'Upgrade lost or duplicated the fixture'
    assert lead.type == 'opportunity' and not lead.user_id
    assert lead.source_id == env.ref('crm_meta_lead_ads.utm_source_facebook')
    assert 'Preserved requirements' in lead.description
    action = env.ref('crm_meta_lead_ads.action_meta_crm_records')
    assert lead in Lead.search(safe_eval(action.domain))
    paths = env.ref('crm_meta_lead_ads.export_meta_leads').export_fields.mapped('name')
    assert len(paths) == 19 and len(paths) == len(set(paths)), 'Duplicate template lines'
    row = lead.export_data(paths)['datas'][0]
    assert not row[paths.index('user_id/name')]
    assert row[paths.index('source_id/name')] == 'Facebook Lead Ads'
    notes = row[paths.index('meta_export_notes')]
    assert 'Preserved requirements' in notes and 'Preserved internal note' in notes
    assert 'Pending upgrade fixture' in row[paths.index('meta_export_open_activities')]
    assert 'Completed upgrade fixture' not in row[paths.index('meta_export_open_activities')]
    assert 'Preserved completed feedback' in row[paths.index('meta_export_done_activities')]
    print('REPORTING_UPGRADE_OK')


run(env, os.environ['REPORTING_UPGRADE_MODE'])
