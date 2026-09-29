# User-facing text: voice, wording and tone

Everything the customer reads — bot messages, buttons, errors, receipts, service
status, reminders — is product surface, not decoration. This document is the
reference for writing or rewording it; `AGENTS.md` §6 is the always-on summary.

Open this when you add or change **anything the customer sees**: a message, a
button label, an error, a warning, a notification, a status line.

The standard is not invented here. It is what the best entries of
`app/locales/fa.json` already do (`buy.success`, `error.generic`,
`error.not_found`, `service.expires_in`); the rest of this file makes that
standard explicit and repeatable.

---

## 1. Voice

- **Who is speaking:** the shop's support desk — competent, calm, on the
  customer's side. Not a government form, not a chat buddy.
- **Second person, plural, polite:** «شما». Never «کاربر گرامی», never talking
  about the customer in the third person.
- **Plain, not administrative.** Say the thing; drop «بدینوسیله»، «به استحضار
  می‌رساند»، «لطفاً منتظر بمانید».
- **Short sentences.** If a sentence needs two commas to survive, it is two
  sentences.
- **Confident.** State what happens next. «در حال ساخت سرویس…» beats «ممکن است
  سرویس شما ساخته شود».
- **Warm, never jokey in a failure.** A failed payment or a broken config is not
  the place for «اوه!».

Stiff or machine-translated → what to write instead:

| Do not write | Write |
|---|---|
| «کاربر گرامی، سفارش شما با موفقیت ثبت گردید.» | «سفارش شما ثبت شد ✅» |
| «لطفاً منتظر بمانید تا عملیات انجام شود.» | «چند لحظه صبر کنید…» |
| «خطای غیرمنتظره‌ای رخ داد. لطفاً دوباره تلاش کنید.» | «مشکلی پیش آمد. یک بار دیگر تلاش کنید؛ اگر باز هم تکرار شد به پشتیبانی پیام بدهید.» |
| «موجودی کیف پول شما کافی نمی‌باشد.» | «موجودی کیف پول کافی نیست. مبلغ لازم: ۲۵۰,۰۰۰ تومان» |
| «عملیات با خطا مواجه شد (502).» | «پنل موقتاً پاسخ نمی‌دهد. چند دقیقه بعد دوباره امتحان کنید.» |

## 2. The shape of a message

Three parts, in this order, and only the parts that carry information:

1. **What happened** — one line, bold when it is the point of the message
   (`<b>سرویس شما ساخته شد</b>`).
2. **What it means** — money, volume, expiry, limits. Skip it when it is obvious.
3. **The next step** — a button when there is an action, a sentence when there is
   not («از منوی اصلی دوباره شروع کنید.»).

`buy.success` is the reference implementation: what happened, then the facts as
a short `<b>label:</b> value` block, then what will arrive next. A block of
facts with no verb is a log line, not a message.

## 3. Buttons and calls to action

- The button **is** the next step: «خرید سرویس»، «تمدید سرویس»، «ارسال رسید».
- **1–3 words**, verb first, no punctuation, no trailing «…», no emoji inside the
  label — the icon comes from the catalog.
- One verb form everywhere: «دریافت کانفیگ», not «گرفتن کانفیگ» on one screen and
  «دانلود» on the next.
- Colour carries meaning and is set in `_BUTTON_RAW`
  (`app/services/appearance.py`): `success` = confirm/continue, `danger` =
  destructive («حذف دستگاه»، «تغییر کلید‌ها»), `primary` = navigation, `link` =
  external URL.
- Never repeat the message inside the button, and never two buttons that do the
  same thing with different words.

## 4. Errors and warnings

- **Cause + action.** Never «خطا رخ داد» on its own. `error.generic` and
  `error.not_found` show the shape: what happened, then what the customer can do.
- **Nothing technical.** No exception text, HTTP status, SQL, panel name, stack
  trace, file path or port. The customer cannot act on those, and they leak
  internals.
- **Never echo a secret.** A panel token, WireGuard config or subscription link
  must not appear in an error message (§1.2 of `AGENTS.md`).
- **Say whether money moved.** For any failure near a payment: «مبلغی از کیف پول
  کسر نشد» or the exact state. Silence here costs a support ticket.
- **⚠️ is for decisions**, not for information. A warning that carries no choice
  is decoration and trains the customer to ignore warnings.
- **Do not promise what the code does not do.** «موضوع به اطلاع تیم فنی رسیده
  است» in `error.generic` is true only because `notifier.report_error` runs first;
  if you remove the notification, that sentence becomes a lie. Check the
  behaviour before you write the promise.

## 5. Money, volume, dates, status

| Text | Helper | Renders as |
|---|---|---|
| Amount | `format_amount(rial)` | «۲۵۰,۰۰۰ تومان» |
| Volume | `format_gb(gb)` | «۳۰ گیگابایت» / «نامحدود» |
| Date | `jalali_date(dt)` | Jalali date, never ISO |
| Raw number | `fa_digits(n)` or the `|fa` filter | «۱۲» |

- **Toman to the customer, always.** Rial is storage only (`AGENTS.md` §1.1).
- Never format an amount by hand: the separator, the digits and the unit belong
  to `format_amount`.
- Say what runs out first and when: «تا پایان اعتبار ۱۲ روز باقی مانده است» beats
  a bare date.
- Status vocabulary stays fixed: «فعال»، «غیرفعال»، «در انتظار تأیید»، «منقضی»,
  «در حال ساخت».

## 6. Emoji

- **One leading emoji per message, or none.** Never mid-sentence, never two in a
  row, never an emoji-only message.
- They come from the catalog as `{e:key}` tokens — 56 keys in
  `app/services/appearance.py` — so the owner can replace them with premium
  emoji from the panel without a deploy. A hard-coded emoji in a handler is an
  automatic rejection (`AGENTS.md` §8).
- Keep meanings stable: ✅ done, ⚠️ caution, ❌ failed, ⏳ waiting, 🔥 featured.

## 7. Length and structure

- **One screen, one idea.** Past roughly ten lines, put the rest behind a button
  or paginate it (`paginate`).
- `<b>` for the one fact that matters, `<code>` only for values the customer may
  want to copy (order code, config, link).
- A blank line between blocks; no headings, no Markdown, no numbered lists unless
  the content is genuinely a list.
- Long-form is fine where the customer asked for it — `rules.text` and
  `guides.title` are allowed to be a screenful.

## 8. Persian mechanics

- **ZWNJ** joins prefixes and suffixes: می‌شود، نمی‌توانید، سرویس‌ها، به‌زودی،
  این‌جا. Written as the literal `\u200c`. A missing ZWNJ is a visible typo.
- **Persian letters only**: «ی» U+06CC and «ک» U+06A9 — never the Arabic «ي» /
  «ك» / «ة». The catalog is clean today (verified: 0 occurrences); keep it clean.
- **Persian punctuation**: «،» «؛» «؟» and «...» for quotes. No ASCII `,` or `?`
  inside a Persian sentence, and no space before «،».
- The **thousands separator** is the one ASCII character allowed inside an amount
  — `format_amount` owns it («۲۵۰,۰۰۰ تومان»). Never type it yourself.
- **Digits** are Persian wherever the customer reads a number.
- «!» only for a real celebration — the whole catalog uses it twice.

## 9. Where text lives

- `app/locales/fa.json` — the shipped catalog (147 keys), grouped by feature.
  The panel's «متن‌های ربات» page edits any key per shop, which is why new copy
  belongs here and not in a handler.
- `app/services/appearance.py` — button labels, colours and emoji ids (71
  buttons, 56 emoji).
- **Known gap, measured:** 258 Persian string literals still sit inside
  `app/bot/handlers/**` (211 distinct — «این سرویس پیدا نشد.» appears 13 times,
  «کاربر پیدا نشد.» 8 times). They cannot be reworded from the panel, their
  politeness drifts («لطفاً» appears in four of them and nowhere in the catalog),
  and some duplicate a catalog key that already exists. Migrating them is a
  product-copy project of its own — do not add new ones, and prefer the catalog
  key whenever you touch one of these screens.

## 10. Terminology

One concept, one word. The glossary is the reference; synonyms are drift.

| Concept | Use | Avoid |
|---|---|---|
| The thing the customer buys and uses | سرویس | اشتراک، اکانت (except «لینک اشتراک», which is the URL) |
| A sellable package (volume + duration) | پلن | بسته (that means *closed*, as in «تیکت بسته شد»), طرح |
| Balance | موجودی کیف پول | اعتبار (that means *validity*, as in «پایان اعتبار») |
| Extending a service | تمدید | رفرش، اکستند |
| Card-to-card payment proof | رسید | فاکتور (that is the pre-payment invoice) |
| Support thread | تیکت | درخواست |
| Subscription URL | لینک اشتراک | لینک ساب |
| WireGuard configuration | کانفیگ | فایل تنظیمات |
| A device slot | دستگاه | دیوایس |
| Data allowance | حجم | ترافیک |
| Gift code / discount code | کد هدیه / کد تخفیف | ووچر، کوپن |
| Customer support | پشتیبانی | ساپورت، ادمین |

## 11. Checklist before you commit copy

- [ ] Reads naturally out loud; no «کاربر گرامی», no «لطفاً منتظر بمانید».
- [ ] The customer can tell what happened **and** what to do next.
- [ ] Errors name a cause and an action, and leak nothing technical.
- [ ] Money in Toman via `format_amount`, volume via `format_gb`, date Jalali,
      digits Persian.
- [ ] At most one leading emoji; nothing emoji-only; no emoji mid-sentence.
- [ ] Button labels are 1–3 words, verb first, from `appearance.py`.
- [ ] ZWNJ, Persian letters and Persian punctuation are correct.
- [ ] Only the allowed HTML tags; user input escaped.
- [ ] New copy added to `app/locales/fa.json` — not to the handler.
- [ ] If the text promises something (a notification, a refund, a delivery), the
      code actually does it.

## 12. Next redesign (recorded, not done here)

Copy-quality work that is bigger than one change, and deliberately not part of
the documentation pass that wrote this file:

1. **Move the 258 inline handler strings into the catalog** so the owner can
   reword every screen, not two thirds of them.
2. **De-duplicate**: one key per message for the repeated ones («این سرویس پیدا
   نشد.» ×13, «کاربر پیدا نشد.» ×8, «این سفارش معتبر نیست.» ×3).
3. **Align the error fallbacks.** The static strings in
   `app/bot/handlers/errors.py` (message body, callback alert) differ in wording
   from `error.generic` for the same failure.
4. **Settle «سرویس» vs «اشتراک»** — 7 catalog keys mix them; the glossary above
   picks «سرویس» and reserves «اشتراک» for the subscription link.
5. **Persian digits in staff-facing messages.** Several admin handlers interpolate
   raw counts next to `format_amount` output, so one line can show «۱۲۳,۰۰۰ تومان»
   beside `12`.
6. **Politeness register.** «لطفاً» appears only in inline strings; decide whether
   the product uses it at all and apply it consistently.
