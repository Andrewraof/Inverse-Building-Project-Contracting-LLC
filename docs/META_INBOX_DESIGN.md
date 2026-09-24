# وثيقة التصميم الرسمية — Meta Inbox Conversations

الحالة: **معتمدة** (بانتظار التنفيذ)
التاريخ: 2026-09-24
النطاق: موديول `crm_meta_lead_ads` — Odoo 19 Community

## 1) الهدف

تحويل رسائل Facebook Messenger من قائمة رسائل منفصلة إلى محادثات
منظمة داخل Odoo: محادثة واحدة لكل عميل (PSID) لكل صفحة Meta، مع
إرسال واستقبال، ربط بـ CRM Lead، وإشعارات لمستخدمي Odoo.

**التصور المعتمد**: Meta Inbox مستقل داخل CRM + إشعارات Odoo +
ربط بالـ Lead + إرسال واستقبال Messenger، باستخدام Odoo Community
فقط، مع تأجيل الواجهة الحية (OWL/JS) وInstagram وWhatsApp.

## 2) القرارات المعتمدة (بعد المراجعة)

1. **`conversation_id` على `meta.message` غير إلزامي في هذه المرحلة**،
   لأن الرسائل القديمة لا تحتوي عليه. يُفرض الإلزام لاحقًا (على
   مستوى المنطق أولًا، ولا يُرفع لقيد DB إلا بعد التأكد من اكتمال
   الترحيل في الإنتاج).
2. **ترحيل تلقائي أثناء ترقية الموديول**: الرسائل القديمة تُجمَّع في
   محادثات حسب (الصفحة، PSID) عبر ملف migration
   (`migrations/<version>/post-migration.py` مع رفع رقم الإصدار)،
   **دون حذف أي رسالة**.
3. **الإشعارات**: تذهب للمسؤول المعيّن `assigned_user_id`، وإن لم
   يوجد تذهب إلى **مستخدم افتراضي يُحدَّد من الإعدادات**
   (حقل جديد في `res.config.settings`). لا إرسال لكل المديرين.
   نشاط واحد مفتوح كحد أقصى لكل محادثة (لا سبام).
4. **نافذة المراسلة (24 ساعة)**: منع استباقي للرد بعد انتهاء النافذة
   مع رسالة توضيحية، مع بقاء **Meta هي المرجع النهائي** (أي خطأ من
   Meta يُعرض منقّى كما هو).
5. **حقل `channel` بقيمة `messenger`** يُضاف الآن على المحادثة
   استعدادًا لـ Instagram لاحقًا.
6. **المرفقات في النسخة الأولى**: رابط + بيانات وصفية فقط (النوع،
   الاسم إن وجد)، بلا تنزيل تلقائي ولا جلب سيرفري — حماية من SSRF
   ومن الروابط الخارجية.
7. **Mark Read**: زر صريح + تصفير تلقائي لغير المقروء عند الرد.
   يكفي للنسخة الأولى.
8. **الواجهة**: Odoo views قياسية (List/Form عملية) — ليست شبيهة
   بـ WhatsApp. التصميم الشبيه بتطبيقات المحادثة (OWL/JavaScript)
   **مرحلة ثانية مؤجلة**.
9. **التكلفة**: لا Odoo Enterprise، لا موديولات مدفوعة، لا SaaS
   خارجي. Messenger Send API لا يفرض حاليًا رسمًا لكل رسالة، لكن
   يلزم نشر التطبيق Live + App Review لـ `pages_messaging`،
   وسياسات Meta قابلة للتغيير (تُراقب).

## 3) خارج النطاق (في هذه المرحلة)

- WhatsApp (قد يترتب عليه رسوم استخدام).
- Instagram Messaging (مُهيّأ له عبر `channel` فقط).
- واجهة محادثة حية بـ OWL/JavaScript أو تحديث لحظي للشاشة.
- إنشاء قنوات Discuss لكل عميل (Discuss يُستخدم للإشعارات فقط).

## 4) نموذج البيانات

### `meta.conversation` (جديد)

| الحقل | النوع | ملاحظات |
|---|---|---|
| `company_id` | Many2one إلزامي | كباقي الموديلات |
| `page_id` | Many2one `meta.page` إلزامي | ondelete cascade |
| `psid` | Char إلزامي، index | Page-Scoped User ID |
| `channel` | Selection | `messenger` (default) — جاهز لـ `instagram` |
| `sender_name` | Char | من الملف الشخصي عند الإمكان |
| `partner_id` | Many2one `res.partner` | اختياري |
| `lead_id` | Many2one `crm.lead` | اختياري، ربط متبادل |
| `assigned_user_id` | Many2one `res.users` | المسؤول |
| `state` | Selection | `new/open/pending/closed`، default `new` |
| `last_message_at` | Datetime، index | للترتيب |
| `last_message_preview` | Char | مقصوص ~100 حرف |
| `unread_count` | Integer | يصفَّر بـ Mark Read أو الرد |
| `message_ids` | One2many | الرسائل |

- قيد: `UNIQUE(page_id, psid)` — محادثة واحدة لكل عميل لكل صفحة.
- `_order = 'last_message_at desc'`.
- `_inherit = ['mail.thread']` لتتبع التغييرات وملخصات Chatter.

### `meta.message` (تطوير)

| الحقل | النوع | ملاحظات |
|---|---|---|
| `conversation_id` | Many2one `meta.conversation` | **غير إلزامي مؤقتًا** (انظر القرار 1) |
| `direction` | Selection | `inbound/outbound`، default `inbound` |
| `send_state` | Selection | للصادرة: `sent/failed`؛ الواردة بدون |
| `failure_reason` | Char | منقّى بـ `_sanitize_error` |
| `attachments_json` | Json، groups=system | نوع + رابط + اسم فقط |

القيد الحالي `UNIQUE(meta_message_id, company_id)` يبقى — رسائل
الصادر تأخذ `message_id` من ردّ Meta.

### `crm.lead` (تطوير)

- `meta_conversation_id` (Many2one، readonly، copy=False) + زر ذكي
  يفتح المحادثة، والعكس من شاشة المحادثة.

### `res.config.settings` (تطوير)

- `meta_inbox_default_user_id` (Many2one `res.users`) — مستلم
  الإشعارات عند غياب مسؤول معيّن (يُخزَّن config parameter).

## 5) تدفق الرسائل

### واردة (Webhook)

1. `_handle_messaging_event` (موجود) → لكل صفحة مطابقة.
2. **savepoint مستقل لكل محادثة** — فشل محادثة لا يوقف الباقي.
3. Upsert المحادثة على `(page_id, psid)` مع `active_test=False`؛
   محادثة `closed` تُعاد إلى `open` عند رسالة جديدة.
4. إنشاء الرسالة: idempotent بـ `meta_message_id`، تخطي echo
   (موجود)، نص أو وصف مرفق `[image]` إلخ.
5. تحديث المحادثة: `last_message_at`، `last_message_preview`،
   `unread_count += 1`.
6. إشعار: `mail.activity` للمسؤول المعيّن، وإلا للمستخدم الافتراضي
   من الإعدادات، وإلا بلا إشعار. نشاط واحد مفتوح لكل محادثة.
7. كل الأخطاء تمر بـ `_sanitize_error` — لا Tokens ولا App Secret
   في السجلات أو الـ chatter أو `results`.

### صادرة (Reply)

1. زر Reply من فورم المحادثة.
2. تحققات: الحساب `connected`، توكن الصفحة موجود، الحالة ليست
   `closed`، **آخر رسالة واردة ضمن 24 ساعة** وإلا منع استباقي
   برسالة توضيحية (Meta تبقى المرجع النهائي لو رفضت).
3. `POST /me/messages` (مessaging_type=`RESPONSE`) بتوكن الصفحة
   عبر `_request` الحالية.
4. **نجاح فقط**: حفظ الرسالة outbound بـ `message_id` المرجع،
   تحديث المحادثة، تصفير `unread_count`.
5. **فشل**: حفظ الرسالة `send_state=failed` بسبب منقّى، وعرض خطأ
   مفهوم. لا تُحفظ كمرسلة إلا بردّ ناجح صريح.
6. لا التفاف على سياسات Meta بأي شكل.

## 6) ترحيل البيانات القديمة

- رفع إصدار الموديول وإضافة `migrations/<new_version>/post-migration.py`:
  - تجميع كل `meta.message` التي `conversation_id` فيها فارغ حسب
    `(company_id, page_id, psid)`.
  - إنشاء `meta.conversation` لكل مجموعة (الاسم من أحدث
    `sender_name`، `last_message_at` من أحدث رسالة، الحالة `open`).
  - ربط الرسائل بمحادثاتها. **لا حذف ولا تعديل لمحتوى الرسائل.**
  - idempotent: آمن عند التشغيل المتكرر.

## 7) الأمان

- صلاحيات: User (قراءة + رد) وAdministrator (إدارة كاملة) —
  نفس مجموعتي الموديول.
- Record rules لكل شركة على `meta.conversation` (و`meta.message`
  مغطاة أصلًا).
- Page Tokens بصلاحية الإدارة فقط (موجود).
- التحقق HMAC للـ webhook (موجود).
- XSS: نصوص الرسائل تُعرض كـ Text مهرّبة من Odoo؛ المرفقات روابط
  فقط بلا HTML مخصص.
- SSRF: لا جلب سيرفري لأي رابط مرفق إطلاقًا.
- لا تخزين/تسجيل Tokens خارج الحقول المحمية (موجود + هذه الوثيقة
  تعمّمه على المحادثات).

## 8) الواجهة (النسخة الأولى)

- قائمة **Inbox** تحت Meta Lead Ads: List مرتبة بآخر رسالة، أعمدة:
  الاسم، آخر رسالة، وقتها، غير المقروء، المسؤول، الحالة، الصفحة.
- فورم المحادثة بثلاث مناطق عبر مجموعات قياسية: بيانات العميل
  والـ Lead والأزرار (Assign، Mark Read، Close، Create Lead،
  Link Lead) أعلى/جانب، وقائمة الرسائل (o2m readonly بترتيب زمني)
  + حقل ردّ في المنتصف.
- بحث وفلاتر: الصفحة، المسؤول، الحالة، غير المقروء، القناة.
- تعمل بالعربية والإنجليزية (RTL من Odoo نفسه).
- **لا JavaScript مخصص** في هذه المرحلة.

## 9) تكامل CRM

- Create Lead من المحادثة: الاسم من `sender_name`، المصدر
  `utm.source` جديد "Meta Messenger"، وربط `meta_conversation_id`.
- منع التكرار: إن وُجد `lead_id` على المحادثة يُعرض رابطه بدل إنشاء
  جديد (الإنشاء الثاني فقط بفصل الربط يدويًا أولًا = طلب صريح).
- ملخص في Chatter عند الإنشاء/الربط (عدد الرسائل وآخر نشاط) —
  بلا نسخ نصوص كاملة ولا أي بيانات حساسة.

## 10) الاختبارات المخططة

إنشاء/تحديث محادثة، idempotency الرسائل، صفحتان لنفس PSID بلا
خلط، inbound/outbound، unread count، إنشاء وربط Lead ومنع التكرار،
فشل API وتوكن منتهٍ (mock)، منع الرد خارج النافذة، حماية
multi-company، عدم تسريب الأسرار (يمتد لاختبار التسريب الحالي)،
توقيع الـ webhook، مرفقات ونص فارغ، والترحيل (إنشاء رسائل قديمة
ثم تشغيل منطق الترحيل).

## 11) مراحل التنفيذ

1. الموديلات + الأمان + إعدادات المستخدم الافتراضي + اختباراتها.
2. تكامل الـ webhook (upsert المحادثة + unread + عزل الفشل).
3. الإرسال الصادر + نافذة 24 ساعة (mock كامل).
4. الواجهة والأزرار والفلاتر.
5. تكامل CRM + UTM + Chatter.
6. ملف الترحيل + اختباره.
7. توثيق: تحديث `PROJECT_STATUS.md` وREADME.

## 12) صلاحيات Meta

لا صلاحية جديدة: `pages_messaging`، `pages_manage_metadata`،
`pages_show_list`، `pages_read_engagement` (كلها في التطبيق).
التشغيل الحقيقي يتطلب نشر التطبيق **Live** و**Advanced Access** لـ
`pages_messaging` (مجاني، يخضع لمراجعة Meta).

## 13) التكلفة

لا Odoo Enterprise، لا موديولات مدفوعة، لا SaaS خارجي، لا رسوم
لكل رسالة حاليًا. تكلفة السيرفر وإعلانات Meta الحالية خارج حساب
هذا التطوير. سياسات Meta قابلة للتغيير وتُراقب قبل كل ترقية.

## 14) مخاطر متبقية

- روابط مرفقات Meta CDN تنتهي صلاحيتها مع الوقت (مقبول: رابط فقط).
- في وضع التطوير قد لا يتوفر اسم العميل لغير أصحاب الأدوار → يظهر
  PSID مؤقتًا حتى النشر Live.
- عدم وجود تحديث لحظي للشاشة في النسخة الأولى (مرحلة OWL المؤجلة).
