# CURRENT_TASK — Batch 2: Complete Meta CRM Operations Suite

الحالة: Batch 2 مصحّحة محلياً بعد مراجعة Codex؛ بانتظار اختبار Odoo 19
على قاعدة اختبار منفصلة قبل اعتماد النشر. لا commit ولا push ولا deploy.
اختبارات Odoo الفعلية لم تُشغَّل (لا توجد بيئة Odoo محلية، ويُمنع
تشغيل `--test-enable` على قاعدة `inverse_elite` الإنتاجية).

## إصلاحات مراجعة 2026-09-25

- إضافة استيراد `api` المفقود في `meta_form.py`، وهو خطأ تحميل Registry
  لا يكشفه `py_compile`.
- إزالة `decoration-*` من جذر قائمة Sync Runs لتبسيطها. مخطط Odoo 19
  الرسمي يقبل هذه الخصائص، لذا لم تكن وحدها سبب فشل مؤكد.
- إبقاء Sync Run في حالة running حتى تنتهي كل سجلات Queue التابعة له؛
  المعالجة على دفعات 20، مع إعادة احتساب نتائج التشغيل من الحالات النهائية
  حتى لا تتكرر العدادات عند الاستئناف أو معالجة الـcron العام.
- إعادة احتساب آخر رسالة وآخر رسالة واردة وأول رد من الرسائل الفعلية
  بعد السحب التاريخي وفي ترحيل النسخة والرد الحي. أول رد هو أول outbound
  ناجح بعد أول inbound؛ الردود الفاشلة لا تغيّر معاينة المحادثة.
- منع Routing Rule من الإشارة إلى صفحة أو نموذج أو فريق أو مستخدم خارج
  شركتها. أُضيفت اختبارات رجوع لهذه الحالات ولأكثر من 20 Lead.
- الفحوصات الثابتة واختبارات الريبو لا تغني عن تشغيل اختبارات Odoo
  والتحقق من Views ضد RNG على بيئة Odoo 19 قبل النشر.

الإصدار: `19.0.4.0.0`. التصميم التفصيلي: `docs/BATCH2_DESIGN.md`.

## ما بُني على الموجود (لم يُكسر)

كل سلوك Batch 1A/1A.1/1B/1B.1 وMeta Inbox قائم كما هو: منع التكرار
المحافظ، التطبيع، مزامنة النماذج الآمنة للمؤرشف، Recovery Polling،
الـAttribution، عزل الشركات، وتنقية الأسرار.

## المزايا الجديدة

1. **`meta.sync.run` + `meta.sync.run.line`**: زر Sync All Meta Data
   يشغّل مهمة خلفية قابلة للاستئناف (work_state JSON + cursors)، خطوات
   connection/pages/forms/leads/conversations/messages، حالات
   draft/running/completed/completed_warnings/failed/cancelled، منع
   مهمتين نشطتين لنفس الحساب، cron tick بحد زمني 45 ثانية، تقرير أعداد
   فقط بلا Tokens/PII. الخطوة ذات الصلاحية المفقودة تُتخطى بتحذير ولا
   تُسقط المهمة.
2. **Diagnostics** على حساب Meta: الصلاحيات الممنوحة/المفقودة، آخر فحص،
   آخر خطأ منقّى، وتوثيق أن CPL يتطلب `ads_read`.
3. **`meta.routing.rule`**: شروط (page/form/campaign/adset/ad/platform/
   city/service/keyword) ونتيجة (team/user/priority/tags/lead_type/
   activity)، أول قاعدة بالترتيب تفوز، `override_manual=False`
   افتراضيًا فلا يُستبدل التعيين اليدوي، Preview read-only، قاعدة شركة.
4. **Wizard سحب تاريخي** `meta.lead.backfill.wizard`: نطاق تاريخي وحدود
   أمان، ينشئ run من نوع leads.
5. **Queue Actions**: Retry Selected / Retry All Failed / Reset to
   Pending (Manager فقط) / Link Selected Lead (ربط ambiguous بسجل
   موجود مع identity من نوع manual)، `processing_seconds` محسوبة،
   كتم إشعارات النجاح عبر `meta_queue_notify`.
6. **Inbox**: مزامنة رسائل تاريخية عبر Graph API (upsert بالـMeta
   message id، attachments metadata فقط بلا تحميل — SSRF آمن)،
   `last_inbound_at` + `first_response_seconds`، حالة `pending` بعد
   الرد الناجح، فلتر Needs Response > 24h، زر Convert to Opportunity،
   routing hook للمحادثات.
7. **كشف الحقول غير المربوطة** على `meta.form` مع بانر تحذير.
8. **تقارير Community فقط**: pivot/graph للـQueue وليدز Meta
   (page/campaign/organic)، المحادثات (unread + متوسط أول رد لكل
   موظف)، وFunnel (lead → opportunity → won/lost).
9. **إعدادات**: `meta_sync_notify` و`meta_queue_notify`.

## Migration 19.0.4.0.0

Backfill لـ`last_inbound_at` و`first_response_seconds` على المحادثات
القائمة — batched وidempotent وأعداد فقط، بلا حذف أو إعادة كتابة
بيانات المستخدم. Rollback = revert للـcommit؛ الأعمدة الجديدة nullable.

## الملفات

- موديلات جديدة: `meta_sync_run.py`, `meta_routing_rule.py`,
  `meta_lead_backfill_wizard.py`, `migrations/19.0.4.0.0/`
- Views جديدة: sync_run, routing_rule, lead_identity, backfill_wizard,
  report_views
- اختبارات جديدة: `test_meta_sync_run.py` (8)، `test_meta_routing.py`
  (9)، `test_meta_messages_sync.py` (7)، `test_meta_queue_actions.py`
  (9)، `test_meta_reports.py` (7)
- معدلة: بقية models/views/security/data/manifest/tests init

## نتائج الفحوصات المحلية

- `py_compile` ✅ (كل models/tests/controllers/migration)
- XML parse ✅ (19 ملفًا)
- `git diff --check` ✅
- `deploy/validate_addon.py` ✅ (19.0.4.0.0)
- اختبارات الريبو 14/14 ✅
- فحص أسرار على الـdiff: صفر ✅
- اختبارات Odoo الفعلية: **لم تُشغَّل** (لا بيئة محلية).

## خطوات التحديث على الإنتاج (بعد الموافقة)

1. commit + push إلى `main` → CI/CD ينشر ويشغّل `-u crm_meta_lead_ads`
   (migration 19.0.4.0.0 يعمل تلقائيًا).
2. تحقق read-only: الخدمة active، سطر migration بالأعداد، لا أخطاء
   Registry/XML/ACL/constraint، لا Tokens/PII في السجل.
3. اختبار حي مؤجل من Batch 1B على النموذج `893155173379183` (صفحة
   InverseGroup): Lead أول `created`، ثم مكرر بنفس البريد/الهاتف
   `matched_*`، مع identity rows وخصوصية audit log.
4. اختبار Sync All Meta Data على الحساب ثم Diagnostics.

## شروط دائمة

- لا Tokens أو App Secrets أو بيانات عملاء في الكود أو السجلات.
- Odoo 19 Community فقط؛ لا Enterprise ولا موديولات مدفوعة ولا
  scraping ولا تجاوز لسياسات Meta.
- لا نشر دون طلب صريح من المستخدم.
