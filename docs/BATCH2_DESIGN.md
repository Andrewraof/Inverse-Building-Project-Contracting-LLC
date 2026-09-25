# BATCH2_DESIGN — Complete Meta CRM Operations Suite (v19.0.4.0.0)

الحالة: منفّذ على فرع مراجعة، واجتاز اختبارات Odoo 19 على قاعدة مؤقتة.
لم يُنشر على الإنتاج عند تحديث هذه الوثيقة.

## 1. المعمارية

```
Meta Graph API                    Odoo 19 Community
─────────────────                 ──────────────────────────────
OAuth ───────────────► meta.account (tokens محمية، Diagnostics)
/me/accounts ────────► meta.page   (upsert بـ meta_page_id)
/{page}/leadgen_forms ► meta.form  (upsert آمن للمؤرشف + pagination)
leadgen webhook ─────► meta.lead.queue ─► process_one ─► crm.lead
/{form}/leads (poll) ┘      ▲            (dedup 1B + identity)
messages webhook ────► meta.conversation / meta.message
/{page}/conversations ─────► (مزامنة تاريخية عبر Sync Run)
                                │
                 meta.sync.run ─┤  مهمة خلفية قابلة للاستئناف
                 meta.sync.run.line  (تقرير أعداد لكل خطوة)
                                │
                 meta.routing.rule ─► تعيين team/user/priority/tags
                                       على lead أو conversation
```

## 2. Sync Run Engine (`meta.sync.run`)

- الزر Sync All Meta Data على الحساب ينشئ Run (state=draft) ثم
  `action_start()` → running. لا ينتظر HTTP request اكتمال العملية.
- cron `ir_cron_meta_sync_runs` (كل دقيقة) ينفذ `_process_tick()` بحد
  زمني 45 ثانية لكل tick؛ التقدم محفوظ في `work_state` (JSON):
  الخطوات المنجزة + cursors الـpagination، فأي فشل/إعادة تشغيل يستأنف
  من آخر نقطة.
- أنواع: `all` / `leads` / `messages` / `forms`، ولكل نوع قائمة خطوات
  (`RUN_TYPE_STEPS`). الـwizard للسحب التاريخي ينشئ run نوع leads
  بنطاق تاريخي وحدود صفحات/سجلات.
- كل خطوة تسجّل `meta.sync.run.line`: fetched/created/updated/
  skipped/failed + رسالة منقّاة بلا Tokens/PII.
- الصلاحيات: `STEP_PERMISSIONS` تربط كل خطوة بصلاحية Meta المطلوبة؛
  الخطوة الناقصة صلاحيتها تُتخطى بتحذير وتنتهي المهمة
  `completed_warnings` بدل `failed`.
- قيد: لا مهمتان (draft/running) لنفس الحساب في نفس الوقت.
- داخل خطوة leads يُعاد استخدام `_enqueue_poll_leads` من Batch 1A.1
  (نفس التصفية الزمنية والـpagination الآمنة) مع ربط السجلات بالرن
  عبر `sync_run_id`، ثم `_process_own_queue` يعالجها على دفعات داخل
  نفس الـtick مع حد زمني.
- خطوة conversations/messages: upsert بالـMeta conversation/message
  ID، idempotent مع الـwebhook، المرفقات metadata فقط (لا تحميل —
  حماية SSRF)، حفظ direction والتوقيت واسم المرسل المتاح.

## 3. Diagnostics (`meta.account`)

- `action_run_diagnostics` يجلب `/me/permissions` عبر Graph API ويخزن
  `granted_permissions` / `missing_permissions` / `permissions_checked_at`
  مع آخر خطأ منقّى. CPL موثق أنه يتطلب `ads_read` ولا يفشل النظام
  بدونه.

## 4. Routing (`meta.routing.rule`)

- شروط AND: page, form, campaign/adset/ad (id أو name)، platform,
  city، service (إجابة سؤال)، keyword + keyword_field.
- نتيجة: sales team, salesperson, priority, tags, lead_type,
  activity type + due delay.
- أول قاعدة مطابقة بالترتيب (`sequence`) تفوز؛ اسمها يُسجَّل على
  الـlead (`meta_routing_rule_id`).
- لا تستبدل تعيينًا يدويًا إلا إذا `override_manual=True`.
- `action_preview_matches` read-only يعرض نتيجة القاعدة على سجل دون
  تعديل. Record rule للشركة.

## 5. Queue Actions

- Retry Selected / Retry All Failed: إعادة failed/ambiguous إلى
  pending مع سطر تدقيق؛ done لا يُمس (لا تكرار lead).
- Reset to Pending: Manager فقط (فحص مجموعة في الكود).
- Link Selected Lead: حل ambiguous يدويًا بربط سجل CRM موجود —
  ينشئ identity من نوع `manual` ولا ينشئ lead ثانيًا.
- `processing_seconds` محسوبة (received_at → processed_at) للتقارير.

## 6. الرسائل التاريخية والرد

- Upsert على (meta_message_id) مع savepoint؛ تكرار webhook/polling
  لا يزيد unread_count (advisory lock على الزيادة).
- الرد الناجح ينقل المحادثة إلى `pending` (بانتظار رد العميل)؛
  رسالة واردة جديدة تعيدها `open` (وتفتح المغلقة/المؤرشفة).
- `first_response_seconds` تُحسب عند أول رد صادر ناجح من
  `last_inbound_at` (أو أول رسالة واردة)، وتُعبَّأ بأثر رجعي عبر
  migration 19.0.4.0.0.
- حارس نافذة 24 ساعة قبل الإرسال؛ Meta هي المرجع النهائي للقبول.

## 7. التقارير (Odoo Community فقط)

- Queue pivot/graph: state × match_result، measures للـattempts
  وزمن المعالجة.
- Meta Leads pivot/graph على crm.lead: page/campaign × organic/paid.
- Conversations pivot: assigned_user × state، measures unread و
  avg(first_response_seconds).
- Funnel pivot: type/stage × active على سجلات Meta.
- CPL: غير متاح دون `ads_read` — يظهر التنبيه في Diagnostics فقط.

## 8. الأمان

- Record rules شركة لكل موديل جديد (sync.run/line, routing.rule).
- ACL منفصلة User (قراءة/تشغيل) / Manager (كاملة)؛ الـwizard Manager.
- لا Tokens/PII في logs أو audit payload أو رسائل الأخطاء (تنقية
  مركزية من Batch 1B).
- لا حذف/دمج تلقائي للـLeads؛ لا إعادة تنشيط مؤرشف دون قرار موثق.
- Idempotency: قيود فريدة + advisory locks (نفس نمط 1B).

## 9. Migration / Rollback

- `19.0.4.0.0/post-migration.py`: backfill `last_inbound_at` و
  `first_response_seconds` على دفعات، idempotent، يسجل أعدادًا فقط.
- كل الأعمدة الجديدة nullable؛ لا حذف بيانات.
- Rollback = revert للـcommit ثم إعادة النشر؛ الأعمدة الجديدة تبقى
  يتيمة غير مستخدمة ولا تكسر الإصدار السابق.

## 10. الصلاحيات المطلوبة من Meta

`leads_retrieval`, `pages_show_list`, `pages_read_engagement`,
`pages_manage_metadata`, `pages_messaging`. CPL يتطلب `ads_read`
(Ads Insights) — غير متوفر حتى يُمنح Advanced Access؛ باقي النظام
يعمل بدونه.

## 11. حدود معروفة

- وضع التطبيق (Live/Development) لا يمكن قراءته عبر API — يوثَّق
  يدويًا ولا يعرضه النظام كحقيقة آلية.
- CPL/spend/impressions غير متاحة قبل `ads_read`.
- اختبارات Odoo الفعلية لم تُشغَّل محليًا (لا بيئة Odoo/Docker) —
  مطلوب تشغيلها على قاعدة disposable قبل الاعتماد النهائي.
