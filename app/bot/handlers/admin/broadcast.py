"""Broadcast (پیام همگانی) composed inside the bot.

The operator opens the feature, sends or forwards the message to broadcast,
picks an audience, previews it and sends.  The composed message is stored on the
campaign (``source_chat_id`` / ``source_message_id``) and delivered with
``copyMessage`` — the only way premium emoji, formatting, media and the
operator's inline keyboard survive mass delivery.

Callbacks are all :class:`~app.bot.callbacks.AdminCB`, owner/admin only:

======================================  =======================================
``action="broadcast"``                  open the composer
``action="bc_audience"``                pick an audience — ``page`` is the
                                        index packed by ``audience_index``
``action="bc_back"``                    back to the audience list
``action="bc_test"``                    deliver one copy to the operator
``action="bc_send"``                    start the campaign and watch progress
``action="bc_cancel"``                  stop a running campaign
``action="bc_abort"``                   drop the draft and leave the flow
======================================  =======================================

``target_id`` always carries the campaign id, so a stale button either finds its
campaign or says it expired — it can never act on a different one.
"""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCB, NavCB
from app.bot.filters import IsAdmin
from app.bot.keyboards import KB, KeyboardBuilder
from app.bot.states import AdminStates
from app.bot.utils import answer_callback, show
from app.core.errors import AppError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import Broadcast, BroadcastStatus, Staff
from app.db.session import session_scope
from app.services.broadcast import (
    AUDIENCE_KEYS,
    FALLBACK_AUDIENCE,
    MAX_BUTTONS,
    BroadcastButton,
    audience_index,
    audience_is_known,
    audience_key,
    audience_label,
    broadcasts,
    buttons_payload,
)
from app.services.notifications import notifier

log = get_logger(__name__)
router = Router(name="admin_broadcast")

#: Where a campaign stops being worth a live status message.
TERMINAL_STATUSES = (BroadcastStatus.DONE, BroadcastStatus.CANCELED, BroadcastStatus.FAILED)

#: How often the status message is refreshed while the campaign runs.
WATCH_INTERVAL_SECONDS = 3.0

#: A watcher gives up eventually; the campaign itself is not affected.
WATCH_LIMIT_SECONDS = 6 * 60 * 60

#: Every callback this module owns.  The last handler matches them all so a
#: press from the wrong role is answered instead of silently ignored.
BROADCAST_ACTIONS = frozenset({"broadcast", "bc_audience", "bc_back", "bc_test", "bc_send", "bc_cancel", "bc_abort"})

#: One watcher per campaign, so re-opening the status screen cannot double-edit.
_watchers: dict[int, asyncio.Task[None]] = {}

# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------
COMPOSE_PROMPT = (
    "<b>📣 پیام همگانی</b>\n\n"
    "پیامی که می\u200cخواهید برای کاربران بفرستید را همین\u200cجا بنویسید یا برای ربات بفرستید.\n"
    "متن، عکس، ویدیو، فایل، گیف و پیام\u200cهای فورواردشده — همراه با ایموجی پرمیوم، "
    "قالب\u200cبندی و دکمه\u200cهایشان — عیناً برای مخاطبان کپی می\u200cشوند.\n\n"
    "<i>برای انصراف /cancel را بزنید.</i>"
)

NOT_SENDABLE = (
    "این پیام قابل ارسال نیست. متن، عکس، ویدیو، فایل، گیف یا یک پیام فورواردشده بفرستید.\n\n"
    "برای انصراف /cancel را بزنید."
)

CANCELED_TEXT = "<b>📣 پیام همگانی</b>\n\nلغو شد؛ چیزی برای کاربران فرستاده نشد."

EXPIRED_ALERT = "این دکمه دیگر معتبر نیست؛ از «پیام همگانی» یک پیام تازه بسازید."

EXPIRED_SCREEN = (
    "<b>📣 پیام همگانی</b>\n\n"
    "این پیش\u200cنویس دیگر پیدا نشد؛ شاید لغو شده باشد. با دکمه\u200cی زیر یک پیام تازه بسازید."
)

AUDIENCE_FALLBACK_ALERT = "این دکمه قدیمی است و گروه مخاطبش شناخته نشد؛ پیام برای همه کاربران آماده شد."


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
async def _restart_keyboard(session: AsyncSession) -> KB:
    """The way out of every dead end: start over or go back to the menu."""
    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("admin.broadcast", callback=AdminCB(action="broadcast").pack())
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


async def _show_audience(event, session: AsyncSession, broadcast: Broadcast, *, kind: str) -> None:
    """Step 2 — one button per audience, each labelled with its real count."""
    kb = KeyboardBuilder(session=session, columns=1)
    for key in AUDIENCE_KEYS:
        count = await broadcasts.preview_count(session, key)
        await kb.add(
            text=f"{audience_label(key)} — {fa_digits(count)} نفر",
            callback=AdminCB(action="bc_audience", target_id=broadcast.id, page=audience_index(key)).pack(),
        )
    kb.row()
    await kb.add("menu.cancel", text="انصراف", callback=AdminCB(action="bc_abort", target_id=broadcast.id).pack())

    body = (
        "<b>📣 پیام همگانی</b>\n\n"
        f"<b>نوع پیام:</b> {kind}\n"
        f"<b>دکمه\u200cهای شیشه\u200cای:</b> {_buttons_summary(broadcast)}\n\n"
        "این پیام برای کدام گروه از کاربران فرستاده شود؟"
    )
    await show(event, body, keyboard=kb.build())


async def _show_preview(event, session: AsyncSession, broadcast: Broadcast, *, kind: str) -> None:
    """Step 3 — what will be sent, to how many, with which buttons."""
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add(text="ارسال آزمایشی به من", callback=AdminCB(action="bc_test", target_id=broadcast.id).pack())
    await kb.add(text="ارسال", style="success", callback=AdminCB(action="bc_send", target_id=broadcast.id).pack())
    kb.row()
    await kb.add(text="تغییر گروه مخاطبان", callback=AdminCB(action="bc_back", target_id=broadcast.id).pack())
    await kb.add("menu.cancel", text="لغو", callback=AdminCB(action="bc_abort", target_id=broadcast.id).pack())

    body = (
        "<b>📣 پیش\u200cنمایش ارسال</b>\n\n"
        f"<b>نوع پیام:</b> {kind}\n"
        f"<b>گروه مخاطبان:</b> {audience_label(broadcast.audience)}\n"
        f"<b>گیرندگان:</b> {fa_digits(broadcast.total)} نفر\n"
        f"<b>دکمه\u200cهای شیشه\u200cای:</b> {_buttons_summary(broadcast)}"
        f"{_placeholder_warning(broadcast)}\n\n"
        "با «ارسال» همین پیام برای همین گروه فرستاده می\u200cشود و «ارسال آزمایشی» فقط یک نسخه به خودتان می\u200cدهد."
    )
    await show(event, body, keyboard=kb.build())


async def _render_progress(session: AsyncSession, broadcast: Broadcast, chat_id: int, message_id: int) -> None:
    """Live status: how far the campaign got, with a way to stop it."""
    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("menu.cancel", text="لغو ارسال", callback=AdminCB(action="bc_cancel", target_id=broadcast.id).pack())
    body = (
        "<b>📣 ارسال در جریان است…</b>\n\n"
        f"<b>گروه مخاطبان:</b> {audience_label(broadcast.audience)}\n"
        f"<b>پیشرفت:</b> {fa_digits(broadcast.sent)} از {fa_digits(broadcast.total)} نفر\n"
        f"<b>ناموفق:</b> {fa_digits(broadcast.failed)}\n\n"
        "می\u200cتوانید این صفحه را ببندید؛ ارسال در پس\u200cزمینه ادامه پیدا می\u200cکند و وضعیت در پنل وب هم دیده می\u200cشود."
    )
    await notifier.edit(chat_id, message_id, body, keyboard=kb.build())


async def _render_finished(session: AsyncSession, broadcast: Broadcast, chat_id: int, message_id: int) -> None:
    """Final summary, including the recipients Telegram refused."""
    if broadcast.status is BroadcastStatus.DONE:
        headline = "<b>📣 ارسال همگانی پایان یافت</b>"
        tail = (
            "پیام برای همه\u200cی گیرندگان این گروه فرستاده شد."
            if not broadcast.failed
            else "به بخشی از گیرندگان نرسید؛ این کاربران ربات را بلاک کرده\u200cاند یا حسابشان در دسترس نیست."
        )
    elif broadcast.status is BroadcastStatus.CANCELED:
        headline = "<b>📣 ارسال همگانی لغو شد</b>"
        tail = (
            "پیام\u200cهای ارسال\u200cشده باقی می\u200cمانند. با «ارسال دوباره» در پنل وب، "
            "پیام از ابتدا برای همه فرستاده می\u200cشود."
        )
    elif broadcast.status is BroadcastStatus.FAILED:
        headline = "<b>📣 ارسال همگانی ناتمام ماند</b>"
        tail = "ارسال با خطا متوقف شد. گزارش کامل در پنل وب، بخش «ارسال همگانی» ثبت شده است."
    else:
        headline = "<b>📣 ارسال همگانی</b>"
        tail = f"وضعیت: {broadcast.status.value}"

    body = (
        f"{headline}\n\n"
        f"<b>گروه مخاطبان:</b> {audience_label(broadcast.audience)}\n"
        f"<b>ارسال\u200cشده:</b> {fa_digits(broadcast.sent)} از {fa_digits(broadcast.total)} نفر\n"
        f"<b>ناموفق:</b> {fa_digits(broadcast.failed)}\n\n"
        f"{tail}"
    )
    await notifier.edit(chat_id, message_id, body, keyboard=await _restart_keyboard(session))


def _buttons_summary(broadcast: Broadcast) -> str:
    count = len(broadcast.buttons or [])
    return f"{fa_digits(count)} دکمه" if count else "ندارد"


def _placeholder_warning(broadcast: Broadcast) -> str:
    """Say out loud that a copied message is *not* re-rendered.

    ``{name}`` and ``{e:key}`` are panel-side placeholders; a copied message
    travels byte for byte, so a literal placeholder would reach thousands of
    customers.  The operator is told before pressing send.
    """
    text = broadcast.text or ""
    if not broadcast.source_message_id or ("{name}" not in text and "{e:" not in text):
        return ""
    marks = [mark for mark, needle in (("«{name}»", "{name}"), ("«{e:...}»", "{e:")) if needle in text]
    return "\n\n⚠️ این پیام عیناً کپی می\u200cشود، پس " + " و ".join(marks) + " در متن آن پر نمی\u200cشود."


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------
async def _load(session: AsyncSession, callback: CallbackQuery, broadcast_id: int) -> Broadcast | None:
    """The campaign behind a button, or a friendly «expired» screen."""
    broadcast = await session.get(Broadcast, broadcast_id) if broadcast_id else None
    if broadcast is not None:
        return broadcast
    await answer_callback(callback, EXPIRED_ALERT, alert=True)
    await show(callback, EXPIRED_SCREEN, keyboard=await _restart_keyboard(session))
    return None


async def _drop_draft(session: AsyncSession, state: FSMContext) -> None:
    """Close the draft a previous attempt left behind, so the panel stays tidy."""
    data = await state.get_data()
    previous = int(data.get("broadcast_id") or 0)
    if not previous:
        return
    row = await session.get(Broadcast, previous)
    if row is not None and row.status == BroadcastStatus.DRAFT:
        row.status = BroadcastStatus.CANCELED
        row.finished_at = now_utc()


def _is_cancel(message: Message) -> bool:
    return (message.text or "").strip() in {"/cancel", "لغو", "انصراف"}


def _content_kind(message: Message) -> str:
    """Persian label for what the operator sent (``""`` when it cannot be sent)."""
    if message.photo:
        return "عکس"
    if message.video:
        return "ویدیو"
    if message.animation:
        return "گیف"
    if message.document:
        return "فایل"
    if message.audio:
        return "صوت"
    if message.voice:
        return "پیام صوتی"
    if message.video_note:
        return "پیام ویدیویی"
    if message.sticker:
        return "استیکر"
    if message.location:
        return "موقعیت مکانی"
    if message.contact:
        return "مخاطب"
    if message.poll:
        # Telegram refuses to copy a quiz without its answer key.
        return "" if message.poll.type == "quiz" else "نظرسنجی"
    if message.text or message.caption:
        return "متن"
    return ""


def _keyboard_payload(message: Message) -> tuple[list[dict[str, str]], int]:
    """Capture the operator's inline keyboard, if the message carried one.

    Telegram drops the source keyboard on a copied message, so the buttons are
    stored on the campaign and re-attached at delivery.  Only URL buttons can
    travel; ``callback_data`` buttons belonged to whoever sent the message.
    Returns ``(payload, dropped)``.
    """
    markup = message.reply_markup
    if not isinstance(markup, InlineKeyboardMarkup):
        return [], 0

    rows = [BroadcastButton(text=button.text, url=button.url or "") for row in markup.inline_keyboard for button in row]
    usable = [row for row in rows if row.text.strip() and row.url.startswith(("http://", "https://"))]
    return buttons_payload(usable[:MAX_BUTTONS]), len(rows) - len(usable[:MAX_BUTTONS])


# ---------------------------------------------------------------------------
# Step 1 — compose
# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "broadcast"), IsAdmin())
async def start_broadcast(callback: CallbackQuery, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    await answer_callback(callback)
    await _drop_draft(session, state)
    await state.clear()
    await state.set_state(AdminStates.entering_broadcast)

    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("menu.cancel", text="انصراف", callback=AdminCB(action="menu").pack())
    await show(callback, COMPOSE_PROMPT, keyboard=kb.build())


@router.message(AdminStates.entering_broadcast, IsAdmin())
async def compose_broadcast(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    """The operator sent (or forwarded) the message to broadcast."""
    if _is_cancel(message):
        await _drop_draft(session, state)
        await state.clear()
        await show(message, CANCELED_TEXT, keyboard=await _restart_keyboard(session))
        return

    kind = _content_kind(message)
    if not kind:
        kb = KeyboardBuilder(session=session, columns=1)
        await kb.add("menu.cancel", text="انصراف", callback=AdminCB(action="menu").pack())
        await show(message, NOT_SENDABLE, keyboard=kb.build())
        return

    await _drop_draft(session, state)
    buttons, dropped = _keyboard_payload(message)
    body = (message.html_text or "").strip() or f"📎 {kind}"
    try:
        broadcast = await broadcasts.create(
            session,
            staff,
            text=body,
            audience=FALLBACK_AUDIENCE,
            buttons=buttons or None,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            start_immediately=False,
        )
    except AppError as exc:
        kb = KeyboardBuilder(session=session, columns=1)
        await kb.add("menu.cancel", text="انصراف", callback=AdminCB(action="menu").pack())
        await show(message, f"⚠️ {exc.message}", keyboard=kb.build())
        return

    if dropped:
        log.info("Broadcast %s: dropped %d buttons Telegram cannot copy", broadcast.id, dropped)
    await state.update_data(broadcast_id=broadcast.id, kind=kind, dropped_buttons=dropped)
    await state.set_state(AdminStates.broadcast_audience)
    await _show_audience(message, session, broadcast, kind=kind)


@router.message(AdminStates.broadcast_audience, IsAdmin())
@router.message(AdminStates.confirming_broadcast, IsAdmin())
async def nudge_to_buttons(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """A message arriving while a screen waits for a button press."""
    data = await state.get_data()
    broadcast = await session.get(Broadcast, int(data.get("broadcast_id") or 0) or 0)
    if broadcast is None:
        await state.clear()
        await show(message, EXPIRED_SCREEN, keyboard=await _restart_keyboard(session))
        return

    kind = str(data.get("kind") or "پیام")
    if await state.get_state() == AdminStates.confirming_broadcast.state:
        await _show_preview(message, session, broadcast, kind=kind)
    else:
        await _show_audience(message, session, broadcast, kind=kind)


# ---------------------------------------------------------------------------
# Step 2 — audience
# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "bc_audience"), IsAdmin())
async def choose_audience(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    broadcast = await _load(session, callback, callback_data.target_id)
    if broadcast is None:
        return

    known = audience_is_known(callback_data.page)
    data = await state.get_data()
    broadcast.audience = audience_key(callback_data.page)
    broadcast.total = await broadcasts.preview_count(session, broadcast.audience)
    await state.set_state(AdminStates.confirming_broadcast)

    # An unknown index means the button outlived this build: say so and go on
    # with «all» rather than blaming the operator for a silent wrong audience.
    await answer_callback(callback, "" if known else AUDIENCE_FALLBACK_ALERT, alert=not known)
    await _show_preview(callback, session, broadcast, kind=str(data.get("kind") or "پیام"))


@router.callback_query(AdminCB.filter(F.action == "bc_back"), IsAdmin())
async def back_to_audience(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    broadcast = await _load(session, callback, callback_data.target_id)
    if broadcast is None:
        return
    data = await state.get_data()
    await state.set_state(AdminStates.broadcast_audience)
    await answer_callback(callback)
    await _show_audience(callback, session, broadcast, kind=str(data.get("kind") or "پیام"))


# ---------------------------------------------------------------------------
# Step 3 — preview, test send, send, cancel
# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "bc_test"), IsAdmin())
async def test_broadcast(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, staff: Staff) -> None:
    broadcast = await _load(session, callback, callback_data.target_id)
    if broadcast is None:
        return
    if staff.telegram_id is None:
        await answer_callback(callback, "شناسه\u200cی تلگرام شما ثبت نشده است؛ ارسال آزمایشی ممکن نیست.", alert=True)
        return
    if not notifier.bound:
        await answer_callback(
            callback, "ربات تلگرام فعال نیست، بنابراین ارسال ممکن نیست. BOT_TOKEN را بررسی کنید.", alert=True
        )
        return

    delivered = await broadcasts.deliver_one(session, broadcast, staff.telegram_id, name=staff.name or "")
    await answer_callback(
        callback,
        "نسخه\u200cی آزمایشی برای شما فرستاده شد."
        if delivered
        else "ارسال آزمایشی انجام نشد. مطمئن شوید ربات می\u200cتواند برای شما پیام بفرستد و دوباره تلاش کنید.",
        alert=not delivered,
    )


@router.callback_query(AdminCB.filter(F.action == "bc_send"), IsAdmin())
async def send_broadcast(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    broadcast = await _load(session, callback, callback_data.target_id)
    if broadcast is None:
        return

    await state.clear()
    try:
        started = await broadcasts.start(broadcast.id)
    except AppError as exc:
        # A missing bot or a dead task must explain itself here; letting it
        # escape would leave a DRAFT row and an unanswered spinner behind.
        await answer_callback(callback, exc.message, alert=True)
        return

    if not started:
        await answer_callback(callback, "این ارسال همین حالا در حال اجراست.", alert=True)
        return

    chat_id, message_id = _message_ref(callback)
    await answer_callback(callback, "ارسال آغاز شد…")
    await session.refresh(broadcast)
    if chat_id is not None and message_id is not None:
        await _render_progress(session, broadcast, chat_id, message_id)
        _start_watcher(broadcast.id, chat_id, message_id)


@router.callback_query(AdminCB.filter(F.action == "bc_cancel"), IsAdmin())
async def cancel_broadcast(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    broadcast = await _load(session, callback, callback_data.target_id)
    if broadcast is None:
        return

    if broadcast.status not in TERMINAL_STATUSES:
        await broadcasts.cancel(broadcast.id)
        await session.refresh(broadcast)
    await answer_callback(callback, "ارسال لغو شد.")

    chat_id, message_id = _message_ref(callback)
    if chat_id is not None and message_id is not None:
        await _render_finished(session, broadcast, chat_id, message_id)


@router.callback_query(AdminCB.filter(F.action == "bc_abort"), IsAdmin())
async def abort_broadcast(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    broadcast = await _load(session, callback, callback_data.target_id)
    if broadcast is None:
        return

    if broadcast.status == BroadcastStatus.DRAFT:
        broadcast.status = BroadcastStatus.CANCELED
        broadcast.finished_at = now_utc()
    await state.clear()
    await answer_callback(callback, "لغو شد.")
    await show(callback, CANCELED_TEXT, keyboard=await _restart_keyboard(session))


@router.callback_query(AdminCB.filter(F.action.in_(BROADCAST_ACTIONS)))
async def broadcast_role_guard(callback: CallbackQuery) -> None:
    """Registered last: a support operator pressing one of these gets an answer.

    The admin menu is visible to every operator, so a press that ``IsAdmin``
    rejected would otherwise spin forever with no explanation.
    """
    await answer_callback(callback, "ارسال همگانی فقط برای مالک و مدیر است.", alert=True)


# ---------------------------------------------------------------------------
# Live progress
# ---------------------------------------------------------------------------
def _message_ref(callback: CallbackQuery) -> tuple[int | None, int | None]:
    """Chat + message id of the screen the button was pressed on."""
    message = callback.message
    if message is None:  # pragma: no cover - inaccessible message
        return None, None
    return message.chat.id, message.message_id


def _start_watcher(broadcast_id: int, chat_id: int, message_id: int) -> None:
    running = _watchers.get(broadcast_id)
    if running is not None and not running.done():
        return
    task = asyncio.create_task(
        _watch_progress(broadcast_id, chat_id, message_id), name=f"broadcast-watch-{broadcast_id}"
    )
    _watchers[broadcast_id] = task
    task.add_done_callback(lambda _task: _watchers.pop(broadcast_id, None))


async def _watch_progress(broadcast_id: int, chat_id: int, message_id: int) -> None:
    """Edit the status message until the campaign reaches a terminal state."""
    elapsed = 0.0
    try:
        while elapsed < WATCH_LIMIT_SECONDS:
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            elapsed += WATCH_INTERVAL_SECONDS
            async with session_scope() as session:
                broadcast = await session.get(Broadcast, broadcast_id)
                if broadcast is None:
                    return
                if broadcast.status in TERMINAL_STATUSES:
                    await _render_finished(session, broadcast, chat_id, message_id)
                    return
                await _render_progress(session, broadcast, chat_id, message_id)
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception as exc:  # pragma: no cover - a watcher never breaks the bot
        log.warning("Broadcast progress watcher stopped: %s", exc)


__all__ = ["router"]
