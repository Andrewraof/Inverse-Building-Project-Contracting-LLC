# CURRENT_TASK — Batch 1B: Conservative Dedup (تطبيع + كشف تكرار محافظ)

الحالة: منفّذة محلياً بالكامل — بانتظار مراجعة المستخدم ثم النشر.
اختبارات Odoo الفعلية لم تُشغَّل (لا توجد بيئة Odoo محلية، ويُمنع
تشغيل `--test-enable` على قاعدة `inverse_elite` الإنتاجية).

## الهدف

منع إنشاء Leads مكررة لنفس العميل عبر البريد/الهاتف بصورة محافظة:
لا دمج ولا حذف تلقائي، لا فقدان لأي Meta Lead ID، ولا استبدال بيانات
موجودة.

## ما تم (v19.0.3.2.0)

1. **تطبيع** `models/meta_dedup.py`:
   - البريد: trim + lowercase + تحقق بنيوي محافظ؛ الفارغ/غير الصالح
     ليس مفتاح مطابقة. الأصل يُحفظ كما وصل.
   - الهاتف: إزالة مسافات/أقواس/شرطات/نقاط، `00`→`+`، الصيغ الإماراتية
     (`+971`, `00971`, `971` بـ12 رقمًا) → `+971...`، والمحلي `0...`
     → `+971` فقط عند دولة شركة AE مؤكدة؛ لا تخمين للأرقام الغامضة
     (تُقارن كأرقام منقّاة فقط).
2. **عمودا مطابقة** `meta_norm_email` / `meta_norm_phone` على
   `crm.lead` (مفهرسان، readonly) يُصانان عبر create/write.
3. **المطابقة** داخل الشركة فقط: meta_lead_id → بريد → هاتف؛ تعارض
   القناتين أو تعدد المرشحين = ambiguous؛ الاسم وحده لا يطابق؛
   المؤرشف لا يُطابَق ولا يُنشَّط.
4. **عند المطابقة**: ملء الفارغ فقط (اتصال + إسناد 1A + source)،
   دون مس user/team/stage/won/lost/active؛ ملاحظة Chatter آمنة.
5. **`meta.lead.identity`**: صف مستقل لكل Meta Lead ID
   (UNIQUE(meta_lead_id, company_id)) مع access + record rule.
6. **النتائج**: `match_result` بالقيم الست + حالة `ambiguous` جديدة
   (خارج الـcron، فلتر خاص، Process Now يدوي بعد المراجعة)، وسبب
   التعارض بمعرفات فقط بلا بيانات اتصال.
7. **تأمين الأخطاء**: logger بلا exc_info وبرسالة منقّاة بكل الأسرار
   (page/user token + app secret)؛ نفس التنقية لـerror_message
   وmeta.lead.log.
8. **الترحيل** `19.0.3.2.0` idempotent على دفعات 500: تطبيع ليدز
   Meta فقط + backfill للـidentity (match_type=backfill)، أعداد فقط.

## إصلاحات جولة المراجعة (2026-09-24)

1. **Migration تنتهي دائمًا**: `_backfill_norm_fields` أصبحت
   id-pagination ثابتة (`id > last_id`) تمر على كل Meta Lead مرة واحدة
   وتكتب القيم المتغيرة فقط — بريد/هاتف غير صالح (تطبيعه False) لم يعد
   يبقي السجل في domain الحلقة إلى الأبد.
2. **حماية سباق حقيقية**: `pg_advisory_xact_lock` بمفاتيح SHA-256 ثابتة
   (company + channel + normalized value، ليست Python hash) مرتبة
   تصاعديًا ضد الـdeadlock؛ يُعاد البحث بعد القفل قبل الإنشاء. اختبار
   التتابع أُعيدت تسميته صراحةً، وأُضيف اختبار تزامن حقيقي في
   `TestMetaDedupConcurrency`: fixture عبر cursor مستقل (بلا commit
   على cursor الاختبار)، خيطان بمعاملتين متداخلتين فعليًا و
   `threading.Event` بـtimeouts — A يمسك القفل داخل معاملة مفتوحة
   بينما B يبدأ ويُثبت انتظاره، ثم يُحرَّر A فيطابق B الليد نفسه.
3. **لا PII في سجل التدقيق**: `meta.lead.log.payload_json` أصبح يحمل
   المعرفات والبيانات التقنية فقط (AUDIT_PAYLOAD_KEYS)؛ الـpayload
   الكامل يبقى في الحقول المحمية (fetched_payload / meta_raw_payload).
4. **عزل الشركات**: بحث `meta_lead_id` في `_resolve_crm_lead` أصبح
   مقيدًا بالشركة؛ اصطدام القيد العالمي بسجل شركة أخرى = ambiguous
   للمراجعة بلا ربط؛ `_ensure_identity` يتحقق أن identity المنافسة
   تشير لنفس Lead والشركة وإلا يعيد رفع الخطأ (لا ابتلاع).

## الملفات

- جديدة: `models/meta_dedup.py`, `models/meta_lead_identity.py`,
  `migrations/19.0.3.2.0/post-migration.py`, `tests/test_meta_dedup.py`
- معدلة: `models/crm_lead.py`, `models/meta_lead_queue.py`,
  `models/__init__.py`, `tests/__init__.py`, `security/ir.model.access.csv`,
  `security/meta_security.xml`, `views/meta_lead_queue_views.xml`,
  `__manifest__.py`, `docs/PROJECT_STATUS.md`

لا تغيير على Controllers أو Webhook أو Messenger Inbox أو توزيع
المندوبين أو التقارير.

## نتائج الفحوصات المحلية

- `py_compile` ✅ — XML validation ✅ — `git diff --check` ✅
- `deploy/validate_addon.py` ✅ (19.0.3.2.0) — اختبارات الريبو 14/14 ✅
- دوال التطبيع: 20/20 حالة فعلية ✅
- اختبارات Odoo الفعلية: **لم تُشغَّل** (لا بيئة محلية).

## التحقق بعد النشر (قبل اعتبارها ناجحة حيًا)

- سطر ترحيل `19.0.3.2.0` بالأعداد في سجل Odoo.
- أول ليد مكرر حقيقي يظهر `match_result` صحيحة + صف identity + ملاحظة
  Chatter، دون Lead ثانٍ.
- لا أخطاء Registry/XML/AccessError، ولا PII/توكنات في السجل.

## شروط دائمة

- لا Tokens أو App Secrets أو بيانات عملاء في الكود أو السجلات.
- Odoo 19 Community فقط؛ لا Enterprise ولا موديولات مدفوعة.
- لا نشر دون طلب صريح من المستخدم.
- الدفعة التالية (2: ربط المحادثات بـLead/Partner) لم تبدأ.
