# CURRENT_TASK — Batch 1A.1: Quiet Recovery Polling (Hotfix)

الحالة: منفّذة محلياً بالكامل — بانتظار مراجعة المستخدم ثم النشر.
اختبارات Odoo الفعلية لم تُشغَّل (لا توجد بيئة Odoo محلية، ويُمنع
تشغيل `--test-enable` على قاعدة `inverse_elite` الإنتاجية).

## الخلفية والتشخيص (مثبت بالأرقام)

- Recovery Polling كان يعمل كل **ساعة** على الإنتاج (وليس 5 دقائق) لأن
  سجل الـcron محمي بـ noupdate ولم تطبَّق عليه تحديثات ملفات البيانات.
- تجربة حية read-only على نموذج واحد (103 ليدز): طلب `since=الآن-ساعة`
  أعاد 100 ليد أقدمها 2026-08-02 → **Meta تتجاهل `since`** على leads
  edge. نفس الطلب بصيغة
  `filtering=[{"field":"time_created","operator":"GREATER_THAN",...}]`
  أعاد 0 نتيجة بلا خطأ → **filtering مقبولة وتعمل**، فاعتُمدت.
- النتيجة: ~239 INSERT فاشلة/ساعة (~5,700 يوميًا) تُسجَّل ERROR
  رغم أنها معالَجة بـsavepoint (استثناء متوقع كمسار طبيعي).

## نطاق Batch 1A.1 (ما تم)

1. **إدخال دفعي هادئ** `_enqueue_poll_leads`: بحث ORM واحد مقيد
   بـ`company_id` عن الـIDs الموجودة، إنشاء المفقود فقط، deduplicate
   داخل الدورة؛ القيد الفريد وsavepoint/IntegrityError يبقيان حماية
   سباق نادرة مع الـwebhook فقط. لا SQL خام.
2. **Pagination آمنة** `_poll_form_leads`: `paging.cursors.after`
   كباراميتر على نفس الـendpoint (لا `paging.next` كـURL → لا SSRF)،
   سقف 10 صفحات، اكتشاف cursor متكرر، تحذير واحد لكل حالة.
3. **تصفية زمنية معتمدة حيًّا**: `filtering/time_created GREATER_THAN`
   عبر `json.dumps` مع هامش تداخل 5 دقائق.
4. **watermark آمن**: `last_sync_date = sync_started_at` (قبل أول طلب)
   بعد اكتمال كل الصفحات فقط؛ لا تقدّم عند فشل أي صفحة؛ عزل فشل كل
   نموذج عن الباقي.
5. **إصلاح فترة الـcron**: migration `19.0.3.1.0` idempotent عبر ORM
   يفرض 5 دقائق + active، ويسجل القيم القديمة/الجديدة فقط.
   `ir_cron_update.xml` أُبقي مع تصحيح توثيقه (غير معتمَد عليه وحده).
6. **ملخص تشغيلي**: INFO واحد لكل نموذج بأعداد فقط
   (fetched/pages/unique/existing/created/race/duration).

## الملفات المعدلة

- `crm_meta_lead_ads/models/meta_lead_queue.py`
- `crm_meta_lead_ads/tests/test_meta_polling.py` (جديد، 15 اختبارًا)
- `crm_meta_lead_ads/tests/__init__.py`
- `crm_meta_lead_ads/migrations/19.0.3.1.0/post-migration.py` (جديد)
- `crm_meta_lead_ads/__manifest__.py` (19.0.3.1.0)
- `crm_meta_lead_ads/data/ir_cron_update.xml` (توثيق التعليق فقط)
- `crm_meta_lead_ads/README.md`, `docs/PROJECT_STATUS.md`, `docs/CURRENT_TASK.md`

لا تغيير على Controllers أو Webhook أو Schema أو قواعد الأمان.

## التحقق بعد النشر (إلزامي قبل اعتبار المشكلة محلولة)

- قيمة الـcron الفعلية على الإنتاج = 5 دقائق، ودورتان متتاليتان بفارق
  ~5 دقائق في السجل.
- دورة polling تسطر INFO ملخصًا واحدًا لكل نموذج و**صفر** ERROR من نوع
  `meta_lead_queue_unique_queued_lead` في المسار الطبيعي.
- سطر ترحيل `19.0.3.1.0` في السجل بقيمة التغيير من ساعة إلى 5 دقائق.

## شروط دائمة

- لا Tokens أو App Secrets أو بيانات عملاء في الكود أو السجلات.
- Odoo 19 Community فقط؛ لا Enterprise ولا موديولات مدفوعة ولا SaaS خارجي.
- لا نشر دون طلب صريح من المستخدم.
- الدفعة 1B لم تبدأ.
