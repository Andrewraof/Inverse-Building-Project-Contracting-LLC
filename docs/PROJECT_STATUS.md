# PROJECT_STATUS — حالة مشروع crm_meta_lead_ads

آخر تحديث: 2026-09-24

## نظرة عامة

المشروع موديول Odoo 19 Community باسم `crm_meta_lead_ads` لربط
**Meta Lead Ads** و**Messenger** مع Odoo CRM لشركة
**Inverse Building Projects Contracting LLC**.

## الميزات المدعومة

- Meta OAuth (تبادل الكود بتوكن طويل الأمد).
- مزامنة الصفحات والنماذج (Lead Gen Forms) مع ربط ديناميكي للحقول.
- Webhook لأحداث `leadgen` و`messages` و`messaging_postbacks`
  على `/meta_crm/webhook` مع تحقق HMAC-SHA256.
- Ingestion Queue مع إعادة محاولة، تُعالج كل دقيقة (cron).
- Recovery Polling كل 5 دقائق لسحب أي ليدز فاتت الـ webhook.
- إنشاء CRM Leads مع مصادر UTM (Facebook / Instagram).
- تسجيل رسائل Messenger الواردة (اسم المرسل + الرسالة الافتتاحية).
- صفحة حذف بيانات المستخدم `/meta_crm/data_deletion` (متطلب Meta).
- دعم multi-company مع record rules، وتنبيه نشاط عند انتهاء التوكن.
- CI/CD: من GitHub Actions إلى السيرفر تلقائيًا.

## الإصلاحات المنفذة

- Upsert للصفحات حسب `meta_page_id` بدل إنشاء سجل جديد دائمًا
  (حل خطأ `This Meta Page is already configured for this company`).
- البحث يشمل الصفحات المؤرشفة باستخدام `active_test=False`
  وإعادة تفعيلها وتحديثها عند إعادة الربط.
- تحديث الحساب والتوكن والصلاحيات للصفحة الموجودة بدل إظهار خطأ.
- عدم إيقاف مزامنة الصفحات بسبب فشل صفحة واحدة (savepoint لكل صفحة).
- تعديل جلب الليد لاستخدام `adset_id` و`campaign_id`
  (Graph API ألغى `page_id`/`adgroup_id` من بيانات الليد).
- إضافة موديل `meta.message` ومعالجة أحداث `entry.messaging`
  مع تخطي رسائل echo.
- الاشتراك في `leadgen,messages,messaging_postbacks` من زر
  Subscribe Webhook (كان `leadgen` فقط ويمسح اشتراك الرسايل).
- تقليل Recovery Polling من ساعة إلى 5 دقائق، مع
  `data/ir_cron_update.xml` لفرض التحديث على القواعد القائمة
  (لأن `ir_cron_data.xml` بخاصية noupdate).
- إضافة اختبارات وAuto Deploy عبر GitHub Actions.
- تقوية `_upsert_from_meta`: رفض التوكن الفارغ أو المساوي لـ
  `meta_page_id`، وعدم مسح التوكن المخزن عند رد فارغ من Meta،
  ورسالة خطأ واضحة للصفحة الجديدة بدون توكن.
- تنقية رسائل أخطاء Graph API من أي توكن قبل عرضها للمستخدم
  أو تسجيلها.
- `action_disconnect` يمسح الآن Page Access Tokens لكل صفحات
  الحساب (بما فيها المؤرشفة) بالإضافة لتوكن المستخدم، وOAuth
  الجديد يعيد إنشاءها عبر الـ upsert.

## بيئة الإنتاج

- Odoo 19 Community.
- قاعدة البيانات: `inverse_elite`.
- الخدمة: `odoo19.service`.
- مسار الموديول على السيرفر:
  `/opt/odoo/custom-addons/boq-budget/crm_meta_lead_ads`.
- المستودع: `Inverse-Building-Project-Contracting-LLC` (فرع `main`).

## ملاحظات تشغيلية

- تطبيق Meta كان في وضع التطوير؛ في هذا الوضع توصّل webhooks
  لأصحاب الأدوار في التطبيق فقط. الـ Recovery Polling يغطي الليدز
  كحل احتياطي. النشر إلى Live + Advanced Access لـ
  `leads_retrieval` و`pages_messaging` هو الحل الجذري.
- الصفحة المعتمدة: **InverseGroup** (عليها نماذج الليدز الفعلية).
- لا تُخزَّن أي Tokens أو App Secrets في هذا المستودع أو التوثيق.
