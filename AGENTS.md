# AGENTS.md — تعليمات العمل على هذا المستودع

## نظرة عامة

هذا المستودع يحتوي موديولات Odoo 19 Community الخاصة بشركة
**Inverse Building Projects Contracting LLC**، وأهمها:

- `crm_meta_lead_ads/` — ربط Meta Lead Ads وMessenger مع Odoo CRM.
- `hvac_sales_extension/` — توسعات مبيعات HVAC.

## قواعد إلزامية

0. **اقرأ `README.md` وملفات `docs/`** (خصوصًا `PROJECT_STATUS.md`
   و`CURRENT_TASK.md`) قبل أي تعديل.
1. **ممنوع وضع أي Tokens أو App Secrets أو كلمات مرور** في الكود أو
   التوثيق أو commit messages. الأسرار تُدار من واجهة Odoo
   (Settings → Meta Lead Ads) أو من متغيرات بيئة السيرفر.
2. **التوافق مع Odoo 19 Community** إلزامي. لا تستخدم APIs أُزيلت
   أو سلوكًا خاصًا بإصدارات أقدم.
3. **لا تحذف أو تغيّر وظائف قائمة بدون ضرورة** — أقل تعديل ممكن
   يحقق الهدف.
4. أي تعديل في منطق الأعمال (models/controllers) **يجب أن يصاحبه
   تحديث الاختبارات** في `crm_meta_lead_ads/tests/` — أضف اختبارًا
   لكل إصلاح.
5. **ممنوع النشر أو تعديل الإنتاج دون طلب صريح**؛ النشر يتم فقط عبر
   GitHub Actions بعد `git push` على `main`.
6. **التسليم**: مع كل مهمة سلّم ملخصًا لما تم، قائمة الملفات
   المعدلة، `git diff` كاملًا، أوامر ونتائج الاختبارات، والمخاطر.

## النشر (CI/CD)

- أي `git push origin main` يشغّل workflow:
  اختبارات → `deploy/validate_addon.py` → نشر عبر SSH على السيرفر
  (إيقاف `odoo19.service`، مزامنة الملفات، تحديث الموديول
  `-u crm_meta_lead_ads`، إعادة التشغيل).
- تفاصيل التصميم في `docs/superpowers/specs/2026-09-21-github-autodeploy-design.md`.

## بنية موديول crm_meta_lead_ads

| المسار | الدور |
|---|---|
| `models/meta_account.py` | حساب Meta: OAuth tokens، `_request()` للـ Graph API |
| `models/meta_page.py` | الصفحات: `_upsert_from_meta()`، اشتراك webhook، مزامنة النماذج |
| `models/meta_form.py` + `meta_form_mapping.py` | نماذج الليدز وربط الحقول |
| `models/meta_lead_queue.py` | طابور الأحداث + الـ cron + Recovery Polling |
| `models/meta_message.py` | تسجيل رسائل Messenger الواردة |
| `models/meta_lead_log.py` | سجل تدقيق (immutable-style) |
| `controllers/main.py` | OAuth connect/callback + webhook `/meta_crm/webhook` |
| `controllers/compliance.py` | صفحة حذف بيانات المستخدم `/meta_crm/data_deletion` |
| `data/ir_cron_data.xml` | تعريف الـ crons (noupdate) |
| `data/ir_cron_update.xml` | فرض تحديث فترات الـ cron على القواعد القائمة |

## بيئة الإنتاج

- Odoo 19 Community، قاعدة البيانات `inverse_elite`، خدمة `odoo19.service`.
- مسار الموديول على السيرفر:
  `/opt/odoo/custom-addons/boq-budget/crm_meta_lead_ads`.

## حالة المشروع والمهمة الحالية

- الحالة الكاملة: `docs/PROJECT_STATUS.md`
- المهمة الجارية: `docs/CURRENT_TASK.md`
