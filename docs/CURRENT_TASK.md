# CURRENT_TASK — Meta Inbox Conversations (Messenger داخل Odoo CRM)

الحالة: منفّذة محليًا بالكامل (Tasks 1–6) — Tasks 1–3 منشورة على الإنتاج،
وTasks 4–6 بانتظار المراجعة ثم النشر. اختبارات Odoo الفعلية لم تُشغَّل بعد
(لا توجد بيئة Odoo محلية) وتبقى شرط الاعتماد النهائي.

## المرجع

- التصميم المعتمد: `docs/META_INBOX_DESIGN.md`
- خطة التنفيذ: `docs/superpowers/plans/2026-09-24-meta-inbox-conversations.md`

## ما تم

1. موديل `meta.conversation` (لكل صفحة + PSID) مع `channel=messenger`،
   حالات new/open/pending/closed، unread count، وقيد `UNIQUE(page_id, psid)`.
2. ربط `meta.message` بالمحادثة + direction + send_state + مرفقات كـ
   metadata فقط (روابط، دون تنزيل سيرفري).
3. Webhook وارد: upsert للمحادثة، idempotency بمعرف الرسالة، نشاط واحد
   للمسؤول أو المستخدم الافتراضي من الإعدادات، عزل الفشل داخل savepoint،
   وتنقية الأخطاء من التوكنات.
4. الرد من Odoo عبر Messenger Send API مع حارس نافذة الـ24 ساعة؛ الفشل
   يُسجّل كرسالة `failed` بسبب منقّى.
5. ترحيل `19.0.2.0.0` يجمّع الرسائل القديمة في محادثات دون حذف أو تعديل
   المحتوى، ويعيد استخدام المحادثات الموجودة (بما فيها المؤرشفة).
6. واجهة Odoo قياسية: قائمة Inbox + فورم (رسائل، رد، Assign، Mark Read،
   Close/Reopen، Create Lead / Open Lead) + فلاتر وبحث.
7. تكامل CRM: إنشاء Lead بمصدر UTM «Meta Messenger»، منع التكرار، وربط
   متبادل مع ظهور المحادثة في فورم الليد.
8. إصلاح نشر حرج: إعادة تسمية One2many المحادثة إلى `meta_message_ids`
   لأن `message_ids` كان يصطدم مع `mail.thread` ويسقط تحديث الموديول.

## المتبقي قبل الاعتماد النهائي

- تشغيل اختبارات Odoo الفعلية (`--test-enable -u crm_meta_lead_ads`) على
  قاعدة اختبار.
- مراجعة المستخدم ثم نشر Tasks 4–6.

## شروط دائمة

- لا Tokens أو App Secrets في الكود أو السجلات أو رسائل الخطأ.
- Odoo 19 Community فقط؛ لا Enterprise ولا موديولات مدفوعة ولا SaaS خارجي.
- المرفقات تُعرض كروابط فقط.
- لا نشر دون طلب صريح من المستخدم.
