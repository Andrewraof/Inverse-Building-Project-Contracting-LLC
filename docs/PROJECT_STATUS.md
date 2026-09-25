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
- صفحة سياسة خصوصية عامة `/meta_crm/privacy` (متطلب نشر تطبيق Meta).
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

## Meta Inbox Conversations (2026-09-24)

- موديل `meta.conversation` لكل (صفحة، PSID) مع `channel=messenger`
  تهيئةً لـ Instagram لاحقًا، وحالات new/open/pending/closed، وقيد
  `UNIQUE(page_id, psid)`.
- رسائل `meta.message` مرتبطة بالمحادثة مع direction وsend_state،
  والمرفقات تُخزَّن كروابط/بيانات وصفية فقط دون تنزيل سيرفري.
- الـ webhook الوارد ينشئ/يحدّث المحادثة، idempotent بمعرف الرسالة،
  ويجدول نشاطًا واحدًا للمسؤول أو المستخدم الافتراضي من الإعدادات.
- الرد من Odoo عبر Messenger Send API مع حارس نافذة 24 ساعة؛ الفشل
  يُسجَّل كرسالة `failed` بسبب منقّى من التوكنات.
- ترحيل `19.0.2.0.0` جمّع الرسائل القديمة في محادثات دون حذف أو
  تعديل محتواها، ويعيد استخدام المحادثات الموجودة والمؤرشفة.
- واجهة Odoo قياسية (List/Form/Search) بأزرار Assign وMark Read و
  Close/Reopen وReply وCreate Lead/Open Lead، مع مصدر UTM
  «Meta Messenger» وربط متبادل مع الليد.
- إصلاح نشر حرج: One2many المحادثة أصبح `meta_message_ids` بعد أن
  تسبب اسم `message_ids` في إسقاط تحديث الموديول (تعارض مع
  `mail.thread.message_ids`).

## Lead Attribution & Multi-Company — Batch 1A (2026-09-24, v19.0.3.0.0)

- تثبيت إسناد الإعلانات على `crm.lead`: `meta_campaign_id` و
  `meta_adset_id` (بفهارس) و`meta_ad_id` (أصبح بفهرس)، إضافة إلى
  الأسماء `meta_campaign_name` و`meta_adset_name` و`meta_ad_name`
  التي تُجلب في نفس طلب leadgen دون صلاحيات إعلانية إضافية. كلها
  اختيارية: غيابها (ليد Organic أو إسناد ناقص من Meta) لا يمنع
  إنشاء الليد.
- الحقل القديم `meta_adgroup_id` أصبح Legacy ويُكتب دائمًا بنفس قيمة
  `meta_adset_id` حتى لا يتعارض الحقلان؛ ترحيل `19.0.3.0.0` idempotent
  ينسخ القيمة القديمة إلى الجديدة عند غيابها فقط ويسجل عدد الصفوف فقط.
  ملاحظة: `meta_campaign_id` والأسماء غير قابلة للاسترجاع بأثر رجعي
  لليدز القديمة لأنها لم تُخزَّن وقتها.
- سد ثغرة multi-company: record rule جديد لـ `meta.message`
  (كانت الرسائل مرئية عبر الشركات) بنفس نمط قواعد الموديول.
- قيد `UNIQUE(meta_lead_id)` لم يُمسّ؛ أي تعديل مستقبلي عليه يتطلب
  قرارًا منفصلًا مدعومًا ببيانات الإنتاج.
- اختبارات جديدة في `tests/test_meta_lead_attribution.py` (12 اختبارًا)
  تغطي الإسناد الكامل/الناقص، idempotency الترحيل، عزل الرسائل بين
  الشركات (مستخدم مقيد ومدير متعدد الشركات)، عدم تسريب التوكن،
  وسلامة القيد الفريد والـ mapping. لم تُشغَّل على بيئة Odoo فعلية بعد.

## Quiet Recovery Polling — Batch 1A.1 (2026-09-24, v19.0.3.1.0)

- تشخيص مثبت بالأرقام: Meta تتجاهل `since` على leads edge (تجربة حية:
  طلب `since=الآن-ساعة` أعاد 100 ليد أقدمها بتاريخ 2026-08-02)، فكانت
  كل دورة polling تعيد نفس 239 ID وتولّد ~239 INSERT فاشلة/ساعة
  (~5,700 يوميًا) تُسجَّل كـERROR رغم معالجتها بـsavepoint.
- إصلاح التصفية الزمنية: `filtering=[{"field":"time_created",
  "operator":"GREATER_THAN","value":...}]` بصيغة `json.dumps` مع هامش
  تداخل 5 دقائق — تجربة حية على نفس النموذج أعادت 0 نتيجة (مقبولة
  وتعمل)، فاعتُمدت في الكود.
- إدخال دفعي هادئ: بحث ORM واحد مقيد بالشركة عن الـIDs الموجودة ثم
  إنشاء المفقود فقط؛ القيد الفريد وsavepoint/IntegrityError يبقيان
  كحماية سباق نادرة مع الـwebhook وليسا مسارًا طبيعيًا، ولا SQL خام.
- Pagination آمنة عبر `paging.cursors.after` كباراميتر على نفس الـendpoint
  (لا `paging.next` كـURL كامل → لا SSRF)، بسقف 10 صفحات واكتشاف cursor
  متكرر، مع تحذير واحد عند أيٍّ منهما وdeduplicate داخل الدورة.
- `last_sync_date` يُحفظ بقيمة `sync_started_at` (قبل أول طلب) وليس وقت
  نهاية الدورة حتى لا يُفقد ليد يصل أثناء الـpagination؛ لا يتقدم عند
  فشل أي صفحة، وفشل نموذج لا يوقف بقية النماذج.
- ملخص INFO واحد لكل نموذج (fetched/pages/existing/created/race/duration)
  بأعداد فقط، بدل مئات أسطر الـERROR.
- ترحيل `19.0.3.1.0` idempotent يفرض فترة الـcron الحقيقية 5 دقائق عبر
  ORM (سجل ir_model_data كان noupdate فبقي الإنتاج على ساعة كاملة
  رغم `ir_cron_update.xml`).
- اختبارات جديدة في `tests/test_meta_polling.py` (15 اختبارًا) تغطي
  كل ما سبق بما فيه صفر SQL ERROR في المسار الطبيعي. لم تُشغَّل على
  بيئة Odoo فعلية بعد.

## Conservative Dedup — Batch 1B (2026-09-24, v19.0.3.2.0)

- تطبيع محافظ لبيانات الاتصال في `models/meta_dedup.py`: البريد
  (trim + lowercase + تحقق بنيوي، والفارغ/غير الصالح ليس مفتاح مطابقة
  أبدًا) والهاتف (إزالة الرموز، `00`→`+`، صيغ UAE الأربع إلى
  `+971...`، والمحلي `0...` فقط عندما تكون دولة الشركة AE بصورة
  مؤكدة، ولا تخمين دولة للأرقام الغامضة). القيمة الأصلية تبقى كما
  وصلت من Meta.
- عمودا مطابقة مفهرسان على `crm.lead` (`meta_norm_email` و
  `meta_norm_phone`) يُصانان تلقائيًا عبر create/write دون مس
  البيانات الأصلية.
- مطابقة داخل الشركة فقط بترتيب: `meta_lead_id` → بريد مطبّع → هاتف
  مطبّع؛ تعارض القناتين أو تعدد المرشحين = `ambiguous` للمراجعة
  اليدوية؛ الاسم وحده ليس مفتاحًا؛ المؤرشف لا يُطابَق ولا يُنشَّط.
- عند المطابقة: ملء الحقول الفارغة فقط (بما فيها إسناد Batch 1A)،
  دون مس salesperson أو team أو stage أو won/lost، مع ملاحظة Chatter
  آمنة عن وصول استفسار Meta جديد.
- موديل `meta.lead.identity` يحفظ كل Meta Lead ID في صف مستقل
  (`UNIQUE(meta_lead_id, company_id)`) مع record rule للشركة.
- نتيجة معالجة واضحة على الطابور: `match_result` (created /
  matched_email / matched_phone / duplicate_meta_id / ambiguous /
  failed) وحالة `ambiguous` جديدة لا يعيدها الـcron تلقائيًا، مع سبب
  التعارض بمعرفات السجلات فقط بلا بيانات اتصال، وإمكانية إعادة
  المعالجة يدويًا بعد المراجعة.
- تأمين مسار الخطأ: رسالة الـlogger أصبحت منقّاة بكل الأسرار المتاحة
  وبدون `exc_info` (نص الـtraceback قد يحمل توكنًا أو بيانات عميل)،
  و`error_message` وسجلات `meta.lead.log` تمر بنفس التنقية.
- ترحيل `19.0.3.2.0` idempotent على دفعات 500: يطبّع الليدز المرتبطة
  بـMeta فقط، وينشئ صفوف identity لليدز القديمة
  (`match_type=backfill`)، ويسجل أعدادًا فقط.
- اختبارات جديدة في `tests/test_meta_dedup.py` (18 اختبارًا)؛ دوال
  التطبيع أُثبتت محليًا بـ20 حالة فعلية. لم تُشغَّل اختبارات Odoo
  الفعلية بعد.
- إصلاحات جولة المراجعة: (1) الـmigration أصبحت id-pagination تنتهي
  دائمًا حتى مع بريد/هاتف غير صالح وتكتب المتغير فقط؛ (2) حماية سباق
  حقيقية بـ`pg_advisory_xact_lock` بمفاتيح SHA-256 ثابتة مرتبة مع
  إعادة بحث بعد القفل، واختبار تزامن حقيقي بمعاملتين في
  `TestMetaDedupConcurrency`؛ (3) `meta.lead.log.payload_json` بلا PII
  (معرفات وبيانات تقنية فقط، والـpayload الكامل في الحقول المحمية)؛
  (4) عزل كامل بين الشركات لبحث `meta_lead_id` وصفوف identity مع
  رفع أي تعارض بدل ابتلاعه.

## Batch 2 — Complete Meta CRM Operations Suite (2026-09-25, v19.0.4.1.0 on review branch)

- موديلا `meta.sync.run` و`meta.sync.run.line`: زر Sync All Meta Data
  يشغّل مهمة خلفية قابلة للاستئناف (work_state + cursors) عبر cron tick
  بحد زمني، تغطي connection/pages/forms/leads/conversations/messages،
  مع تقرير أعداد فقط وحالات نجاح جزئي، ومنع مهمتين نشطتين لنفس الحساب.
  الخطوة ذات الصلاحية المفقودة تُتخطى بتحذير (Completed with warnings).
- تبويب Diagnostics على حساب Meta: الصلاحيات الممنوحة/المفقودة وآخر
  فحص وخطأ منقّى، مع توثيق أن CPL يتطلب `ads_read` (غير متاح حاليًا).
- موديل `meta.routing.rule`: توزيع تلقائي بشروط (page/form/campaign/
  adset/ad/platform/city/service/keyword) ونتيجة (team/user/priority/
  tags/lead_type/activity)، أول قاعدة بالترتيب تفوز، ولا يستبدل
  التعيين اليدوي إلا بخيار صريح `override_manual`.
- Wizard سحب تاريخي `meta.lead.backfill.wizard` بنطاق تاريخي وحدود.
- Queue Actions: Retry Selected/All Failed، Reset to Pending (Manager)،
  Link Selected Lead للـambiguous (identity من نوع manual)، وكتم
  إشعارات النجاح عبر `meta_queue_notify` و`meta_sync_notify`.
- Inbox: مزامنة رسائل تاريخية (upsert بمعرف الرسالة، attachments
  metadata فقط بلا تحميل)، `last_inbound_at` و`first_response_seconds`
  مع migration 19.0.4.0.0 idempotent، حالة `pending` بعد الرد، فلتر
  Needs Response > 24h، وزر Convert to Opportunity.
- كشف الحقول غير المربوطة على `meta.form` (`unmapped_field_names`).
- تقارير Community: pivot/graph للـQueue وليدز Meta والمحادثات
  والـFunnel، وsmart buttons على crm.lead (identity + المحادثة).
- ربط محادثة Inbox بليد قائم عبر Wizard مع حراسة الصلاحيات والشركة
  والتزامن والربط المتبادل. مقياس رقمي للمحادثات المرتبطة بليد،
  و`first_response_at` لتمييز الرد الفوري من عدم الرد. ترحيل مستقل
  `19.0.4.1.0` يعيد تعبئة المؤشر والرد الأول للقواعد القديمة.
- اختبارات جديدة في ملفات sync_run, routing, messages_sync,
  queue_actions, reports. اختبارات Odoo 19 الفعلية اجتازت على قاعدة
  مؤقتة في GitHub Actions (تشغيل `36176476010`: **219 اختبارًا بلا
  فشل أو خطأ**، بما فيها اختبار ربط متزامن بمعاملتين).
  لم تُشغَّل اختبارات على قاعدة الإنتاج.

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
