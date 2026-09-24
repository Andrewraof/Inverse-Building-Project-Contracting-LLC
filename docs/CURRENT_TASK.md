# CURRENT_TASK — Batch 1A: Lead Attribution + meta.message Multi-Company

الحالة: منفّذة محليًا بالكامل — بانتظار مراجعة المستخدم ثم النشر.
اختبارات Odoo الفعلية لم تُشغَّل (لا توجد بيئة Odoo محلية، ويُمنع
تشغيل `--test-enable` على قاعدة `inverse_elite` الإنتاجية) وتبقى
شرط الاعتماد النهائي على قاعدة اختبار منفصلة.

## المرجع

- خارطة الطريق المعتمدة: محاور التطوير الثلاثة (ربط المحادثات بالـCRM،
  تحسين Lead Ads، التقارير) مقسمة إلى دفعات 1A, 1B, 2, 3, 4, 5, 6
  ودفعة 7 مؤجلة (تكلفة الليد بعد توفر `ads_read` وموافقة منفصلة).

## نطاق الدفعة 1A (ما تم)

1. حقول إسناد جديدة على `crm.lead`: `meta_campaign_id` و`meta_adset_id`
   (بفهارس)، `meta_ad_id` أصبح بفهرس، والأسماء `meta_campaign_name` /
   `meta_adset_name` / `meta_ad_name` — تُجلب في نفس طلب leadgen
   الحالي (`_fetch_lead`) دون طلبات أو صلاحيات إضافية، وكلها اختيارية
   (ليد Organic أو إسناد ناقص لا يمنع إنشاء الليد).
2. `meta_adgroup_id` يبقى للتوافق ويُكتب دائمًا بنفس قيمة
   `meta_adset_id`؛ ترحيل `19.0.3.0.0` idempotent ينسخ القديم→الجديد
   عند غياب الجديد فقط ويسجل عدد الصفوف فقط (لا بيانات شخصية).
   `meta_campaign_id` والأسماء غير قابلة للاسترجاع لليدز القديمة
   (لم تُخزَّن وقتها) — موثق كفجوة بيانات معروفة.
3. Record rule جديد لـ `meta.message` يسد ثغرة رؤية الرسائل عبر
   الشركات.
4. فورم الليد يعرض الحقول الجديدة في تبويب Meta Lead Ads، مع وسم
   الحقل القديم «Ad Set ID (Legacy)».
5. قيد `UNIQUE(meta_lead_id)` لم يُمسّ (قرار مؤجل مدعوم بالبيانات).

## الملفات المعدلة

- `crm_meta_lead_ads/__manifest__.py` (الإصدار 19.0.3.0.0)
- `crm_meta_lead_ads/models/crm_lead.py`
- `crm_meta_lead_ads/models/meta_lead_queue.py`
- `crm_meta_lead_ads/security/meta_security.xml`
- `crm_meta_lead_ads/views/crm_lead_views.xml`
- `crm_meta_lead_ads/migrations/19.0.3.0.0/post-migration.py` (جديد)
- `crm_meta_lead_ads/tests/__init__.py`
- `crm_meta_lead_ads/tests/test_meta_lead_attribution.py` (جديد، 12 اختبارًا)
- `crm_meta_lead_ads/README.md`, `docs/PROJECT_STATUS.md`, `docs/CURRENT_TASK.md`

## Rollback

- إعادة نشر SHA السابق تعيد سلوك الكود السابق؛ الأعمدة الجديدة nullable
  وتبقى في قاعدة البيانات دون ضرر للكود القديم.
- الـ record rule الجديد لا يُحذف تلقائيًا عند الرجوع؛ إزالته تحتاج
  migration صريحًا (غير منفذ في هذه الدفعة عمدًا).
- لا حذف أعمدة ولا حذف قواعد في هذه الدفعة.

## المتبقي قبل الاعتماد النهائي

- مراجعة المستخدم للكود والـ diff.
- تشغيل اختبارات Odoo الفعلية على قاعدة اختبار منفصلة (لم تُنشأ بعد؛
  تتطلب موافقة منفصلة).
- النشر عبر push إلى `main` بعد أمر صريح فقط.
- بعد النشر: فتح سجل Queue حديث والتأكد من وجود `campaign_id`
  والأسماء في `fetched_payload` الفعلي، وفحص سطر الترحيل في السجل.

## شروط دائمة

- لا Tokens أو App Secrets في الكود أو السجلات أو رسائل الخطأ.
- Odoo 19 Community فقط؛ لا Enterprise ولا موديولات مدفوعة ولا SaaS خارجي.
- المرفقات تُعرض كروابط فقط.
- لا نشر دون طلب صريح من المستخدم.
