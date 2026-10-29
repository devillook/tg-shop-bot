# main.py — Telegram-магазин аккаунтов (оплата подарками в звёздах, рулетка, лог активности)
import asyncio
import html
import logging
import os
import random
import time
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telethon import errors, functions

import accounts
import config
import database
import geo
import code_watch
import gifts
import payments
import texts
import ui
from ui import btn, keyboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%H:%M:%S")
for noisy in ("aiogram", "telethon", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("main")

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
bot_username: str = ""  # заполняется при старте, используется для реферальных ссылок

PAGE_SIZE = 8
ROULETTE_COOLDOWN = 86400  # 24 часа
_last_code_request: dict[int, float] = {}
_bg_tasks: set[asyncio.Task] = set()
_pending_logins: dict[int, dict] = {}  # admin_id -> {client, session_name, phone, sent, cloud_password}


async def _abort_pending_login(admin_id: int):
    """Отключает клиент и удаляет недоделанный файл сессии — используется при отмене/ошибке/таймауте."""
    data = _pending_logins.pop(admin_id, None)
    if not data:
        return
    try:
        await data["client"].disconnect()
    except Exception:
        pass
    path = os.path.join(config.SESSIONS_DIR, data["session_name"] + ".session")
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


class ReviewState(StatesGroup):
    text = State()


class AdminAddState(StatesGroup):
    phone = State()
    price = State()
    premium = State()
    year = State()


class AdminLoginState(StatesGroup):
    phone = State()
    code = State()
    password = State()
    price = State()
    year = State()


class PromoState(StatesGroup):
    code = State()


class SupportState(StatesGroup):
    text = State()


# ---------- вспомогательное ----------
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def who(user: types.User) -> str:
    return f"@{user.username}" if user.username else user.full_name


def fmt_ts(ts) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


async def log_action(user: types.User, action_desc: str):
    """Короткий лог активности пользователя для админов."""
    text = (
        f"⚡️ <b>АКТИВНОСТЬ</b>  ·  {html.escape(who(user))} (<code>{user.id}</code>)\n"
        f"📌 {html.escape(action_desc)}"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


def stats_line() -> str:
    sold = config.STATS_BASE_SOLD + database.count_sold()
    users = config.STATS_BASE_USERS + database.count_users()
    return f"📊 Куплено: <b>{sold}</b>  ·  Пользовались ботом: <b>{users}</b>"


def item_style(p):
    """Цвет кнопки аккаунта: премиум — зелёная, старше 5 лет — синяя, остальные — серые."""
    if p["premium"]:
        return "success"
    years = geo.age_years(p["reg_year"])
    return "primary" if years is not None and years >= 5 else None


def item_label(p) -> str:
    parts = [f"{geo.country_of(p['phone'])[0]} #{p['id']}"]
    if p["premium"]:
        parts.append("💎")
    age = geo.age_short(p["reg_year"])
    if age:
        parts.append(age)
    parts.append(f"{p['price_stars']} ⭐")
    return "   ·   ".join(parts)


def timer_line(created_at: int) -> str:
    total = config.PAYMENT_MINUTES * 60
    left = max(0, created_at + total - int(time.time()))
    return f"⏳ Осталось: <b>{left // 60:02d}:{left % 60:02d}</b>\n{ui.bar(round(left / total * 10), 10)}"


def welcome_text(user: types.User) -> str:
    return texts.welcome(html.escape(user.first_name or "друг"))


def menu_kb(admin: bool = False):
    rows = [
        btn("🛍   К А Т А Л О Г   🛍", "catalog", style="primary"),
        btn("🎰   РУЛЕТКА СЧАСТЬЯ   ✨", "roulette_home", style="success"),
        btn("👤   Мой кабинет", "cabinet", style="primary"),
        btn("📦   Мои покупки", "my_orders", style="primary"),
        btn("❓   Частые вопросы", "faq", style="primary"),
        btn("💬   Поддержка", "support_start", style="success"),
        btn("📄   Соглашение", "agreement", style="danger"),
    ]
    if admin:
        rows.append(btn("🛠   Админ-панель", "admin_panel", style="danger"))
    return keyboard(*rows)


def back_kb(data: str = "back_home", label: str = "◀️   Назад"):
    return keyboard(btn(label, data))


# ---------- обязательная подписка на канал ----------
_sub_cache: dict[int, tuple[bool, float]] = {}
SUB_CACHE_SECONDS = 60


async def is_subscribed(user_id: int) -> bool:
    if not config.SUB_CHANNEL:
        return True
    cached = _sub_cache.get(user_id)
    if cached and time.time() - cached[1] < SUB_CACHE_SECONDS:
        return cached[0]
    try:
        member = await bot.get_chat_member(f"@{config.SUB_CHANNEL}", user_id)
        ok = member.status in ("member", "administrator", "creator")
    except Exception:
        log.warning("Не удалось проверить подписку на @%s — бот добавлен в канал админом?", config.SUB_CHANNEL)
        ok = False
    _sub_cache[user_id] = (ok, time.time())
    return ok


def sub_gate_text() -> str:
    return (
        "🔒 <b>Подпишитесь на канал магазина</b>\n"
        f"{ui.LINE}\n\n"
        "Чтобы пользоваться ботом, подпишитесь на основной канал магазина, а затем нажмите «Проверить подписку»."
    )


def sub_gate_kb() -> types.InlineKeyboardMarkup:
    return keyboard(
        btn("📢   Подписаться на канал", url=f"https://t.me/{config.SUB_CHANNEL}", style="primary"),
        btn("✅   Проверить подписку", "check_sub", style="success"),
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub(cb: types.CallbackQuery):
    _sub_cache.pop(cb.from_user.id, None)
    if await is_subscribed(cb.from_user.id):
        await cb.answer("Подписка подтверждена! 🎉", show_alert=True)
        await ui.safe_edit(cb.message, welcome_text(cb.from_user), menu_kb(is_admin(cb.from_user.id)))
    else:
        await cb.answer("Пока не вижу подписку. Подпишитесь и попробуйте снова.", show_alert=True)


@dp.message.outer_middleware()
async def sub_gate_message_mw(handler, event: types.Message, data):
    # /start пропускаем всегда: там считывается реферальная ссылка и сам же /start покажет
    # экран подписки, если нужно — так ссылка с ref_ не теряется.
    if event.text and event.text.startswith("/start"):
        return await handler(event, data)
    if event.from_user and not is_admin(event.from_user.id) and not await is_subscribed(event.from_user.id):
        await event.answer(sub_gate_text(), reply_markup=sub_gate_kb())
        return
    return await handler(event, data)


@dp.callback_query.outer_middleware()
async def sub_gate_callback_mw(handler, event: types.CallbackQuery, data):
    if event.data == "check_sub" or is_admin(event.from_user.id):
        return await handler(event, data)
    if not await is_subscribed(event.from_user.id):
        await event.answer("Сначала подпишитесь на канал 👇", show_alert=True)
        await ui.safe_edit(event.message, sub_gate_text(), sub_gate_kb())
        return
    return await handler(event, data)


# ---------- старт / стоп / меню ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()
    is_new = database.get_user_row(message.from_user.id) is None
    database.add_user(message.from_user.id, message.from_user.username)
    await log_action(message.from_user, "Запустил бота (/start)")

    # ---- реферальная программа: /start ref_<id>, засчитывается только первому запуску нового пользователя ----
    if is_new and command.args and command.args.startswith("ref_"):
        ref_part = command.args[4:]
        if ref_part.isdigit():
            referrer_id = int(ref_part)
            if referrer_id != message.from_user.id and database.record_referral(message.from_user.id, referrer_id):
                new_balance = database.add_balance(referrer_id, config.REFERRAL_BONUS_TOKENS)
                try:
                    await bot.send_message(
                        referrer_id, f"🎉 <b>Новый реферал!</b> {html.escape(who(message.from_user))} запустил бота "
                                     f"по вашей ссылке.\n💰 Начислено: <b>+{config.REFERRAL_BONUS_TOKENS} токенов</b> "
                                     f"(баланс: {new_balance}).")
                except Exception:
                    pass
                await log_action(message.from_user, f"Пришёл по реферальной ссылке от {referrer_id}")

    await bot.send_chat_action(message.chat.id, "typing")
    if not is_admin(message.from_user.id) and not await is_subscribed(message.from_user.id):
        await message.answer(sub_gate_text(), reply_markup=sub_gate_kb())
        return
    await ui.send(bot, message.chat.id, welcome_text(message.from_user), menu_kb(is_admin(message.from_user.id)),
                  effect=ui.EFFECT_FIRE)


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message, state: FSMContext):
    """Сбрасывает незавершённые заказы пользователя (даже во время оплаты) и возвращает к первому сообщению."""
    await state.clear()
    cancelled = database.cancel_user_pending(message.from_user.id)
    log.info("[STOP] пользователь %s: сброшено заказов — %s", message.from_user.id, len(cancelled))
    msg = await message.answer("🛑 <b>Сбрасываю заказы…</b>\n\n▰▱▱")
    await ui.animate(msg, ["🛑 <b>Сбрасываю заказы…</b>\n\n▰▰▱", "🛑 <b>Сбрасываю заказы…</b>\n\n▰▰▰"], 0.35)
    await ui.safe_edit(msg, welcome_text(message.from_user), menu_kb(is_admin(message.from_user.id)))


@dp.callback_query(F.data == "back_home")
async def back_home(cb: types.CallbackQuery):
    await log_action(cb.from_user, "Вернулся в главное меню")
    await ui.safe_edit(cb.message, welcome_text(cb.from_user), menu_kb(is_admin(cb.from_user.id)))
    await cb.answer()


@dp.callback_query(F.data == "faq")
async def show_faq(cb: types.CallbackQuery):
    await ui.safe_edit(cb.message, texts.FAQ, back_kb())
    await cb.answer()


@dp.callback_query(F.data == "agreement")
async def show_agreement(cb: types.CallbackQuery):
    await ui.safe_edit(cb.message, texts.AGREEMENT, back_kb())
    await cb.answer()


# ---------- поддержка ----------
@dp.callback_query(F.data == "support_start")
async def support_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(SupportState.text)
    await ui.safe_edit(
        cb.message,
        "💬 <b>Поддержка</b>\n"
        f"{ui.LINE}\n\n"
        "Нам искренне жаль, что у вас произошла проблема. Пожалуйста, опишите её как можно подробнее "
        "и отправьте фото-доказательство <b>одним сообщением</b>.\n\n"
        "Наша команда рассмотрит дело как можно быстрее и вынесет вердикт.",
        keyboard(btn("❌   Отмена", "support_cancel")))
    await cb.answer()


@dp.callback_query(F.data == "support_cancel")
async def support_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, welcome_text(cb.from_user), menu_kb(is_admin(cb.from_user.id)))
    await cb.answer()


@dp.message(SupportState.text, F.text | F.photo)
async def support_submit(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return  # дать пройти командам (например /stop), а не проглотить их как текст обращения
    await state.clear()
    user = message.from_user
    header = (f"🆘 <b>ОБРАЩЕНИЕ В ПОДДЕРЖКУ</b>\n{ui.LINE}\n"
             f"От: {html.escape(who(user))} (<code>{user.id}</code>)\n"
             f"Ответить: <code>/support {user.id} текст</code>\n{ui.LINE}")
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, header)
            await bot.copy_message(admin_id, from_chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            log.exception("Не удалось переслать обращение в поддержку админу %s", admin_id)
    await log_action(user, "Отправил обращение в поддержку")
    await message.answer("✅ <b>Обращение отправлено!</b>\n\nМы ответим вам в ближайшее время прямо здесь, в боте.",
                         reply_markup=back_kb("back_home", "◀️   В меню"))


@dp.callback_query(F.data == "my_orders")
async def my_orders(cb: types.CallbackQuery):
    orders = database.get_user_orders(cb.from_user.id)
    if not orders:
        text = f"📦 <b>МОИ ПОКУПКИ</b>\n{ui.LINE}\n\nУ вас пока нет покупок. Самое время заглянуть в каталог 😉"
        await ui.safe_edit(cb.message, text, keyboard(btn("🛍   В каталог", "catalog", style="primary"),
                                                       btn("◀️   Назад", "back_home")))
    else:
        lines = [f"📦 <b>МОИ ПОКУПКИ</b>\n{ui.LINE}\n"]
        for o in orders:
            state = "✅ завершён" if o["status"] == "done" else "🟢 выдан"
            lines.append(f"▫️ Заказ <b>#{o['id']}</b> — <code>{html.escape(o['phone'])}</code>\n"
                         f"    {state} · {fmt_ts(o['paid_at'])}")
        await ui.safe_edit(cb.message, "\n".join(lines), back_kb())
    await cb.answer()


# ---------- каталог ----------
@dp.callback_query(F.data == "catalog")
async def catalog_home(cb: types.CallbackQuery):
    await log_action(cb.from_user, "Открыл каталог")
    await cb.answer()
    await ui.animate(cb.message, ui.loading("Загружаю каталог", 4), 0.28)
    rows = database.get_available_products()
    head = f"{stats_line()}\n\n{ui.LINE}\n🛍 <b>КАТАЛОГ АККАУНТОВ</b>\n{ui.LINE}\n\n"
    if not rows:
        await ui.safe_edit(cb.message, head + "😔 Сейчас нет доступных аккаунтов. Загляните позже!",
                           back_kb("back_home", "◀️   В меню"))
        return
    buttons = [btn(f"🌍   ВСЕ АККАУНТЫ   ·   {len(rows)}", "list_all_0", style=geo.FILTER_STYLES["all"])]
    for key, label in geo.FILTERS:
        n = len(geo.apply_filter(rows, key))
        buttons.append(btn(f"{label}   ·   {n}", f"list_{key}_0", style=geo.FILTER_STYLES.get(key)))
    buttons.append(btn("◀️   В меню", "back_home"))
    await ui.safe_edit(cb.message, head + "Выберите фильтр 👇", keyboard(*buttons))


@dp.callback_query(F.data.startswith("list_"))
async def show_list(cb: types.CallbackQuery):
    _, key, page = cb.data.split("_")
    page = int(page)
    rows = geo.apply_filter(database.get_available_products(), key)
    title = f"<b>{geo.title_of(key)}</b>"
    if not rows:
        await ui.safe_edit(cb.message, f"{title}\n{ui.LINE}\n\n😔 В этой категории пока пусто.",
                           back_kb("catalog", "◀️   К фильтрам"))
        await cb.answer()
        return

    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    buttons = [btn(item_label(p), f"item_{p['id']}_{key}", style=item_style(p))
               for p in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"list_{key}_{page - 1}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"list_{key}_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append(btn("◀️   К фильтрам", "catalog"))
    note = "\n<i>Старые аккаунты — выше.</i>" if key == "long" else ""
    legend = "\n<i>🟢 Premium   🔵 от 5 лет</i>"
    await ui.safe_edit(cb.message, f"{stats_line()}\n\n{title}\n{ui.LINE}\nНайдено: <b>{len(rows)}</b>{note}{legend}"
                                   f"\n\nВыберите аккаунт 👇", keyboard(*buttons))
    await cb.answer()


async def show_unsynced_item(msg: types.Message, product, key: str):
    text = (
        "😅 <b>Упс!..</b>\n\n"
        "Похоже аккаунт ещё не успели донести до базы данных\n\n"
        "<b>НЕ беда!</b> Аккаунт всё ещё можно купить — напиши @" + config.ADMIN_USERNAME + " с заготовленным "
        "текстом (нажми на кнопку, шаблон появится сам). Администрация быстро рассмотрит твой запрос "
        "о покупке и выдаст номер вручную.\n\n"
        "<i>Всё согласовано в политике конфиденциальности.</i>"
    )
    sender = msg.chat
    template = (f"Я {sender.first_name or ''} ({'@' + sender.username if getattr(sender, 'username', None) else 'без username'}, "
               f"id {sender.id}) хочу купить аккаунт {product['phone']} за {product['price_stars']} ⭐. "
               "При покупке сказали написать сюда, помогите, пожалуйста!")
    url = f"https://t.me/{config.ADMIN_USERNAME}?text={quote(template)}"
    await ui.safe_edit(msg, text, keyboard(
        btn("✍️   Написать администрации", url=url, style="primary"),
        btn("◀️   Вернуться в каталог", f"list_{key}_0", style="danger"),
    ))


@dp.callback_query(F.data.startswith("item_"))
async def show_item(cb: types.CallbackQuery):
    _, pid, key = cb.data.split("_")
    product = database.get_product(int(pid))
    if not database.is_buyable(product):
        await cb.answer("Этот аккаунт уже забронирован или куплен!", show_alert=True)
        return

    if not product["synced"]:
        await cb.answer()
        await show_unsynced_item(cb.message, product, key)
        return

    flag, country = geo.country_of(product["phone"])
    text = (
        f"{flag} <b>АККАУНТ #{product['id']}</b>\n"
        f"{ui.LINE}\n"
        f"🌍 Страна: <b>{country}</b>\n"
        f"📞 Номер: <code>{html.escape(product['phone'])}</code>\n"
        f"🆔 ID: <code>{html.escape(product['account_id'])}</code>\n"
        f"🛡 Спам-блок: <b>{html.escape(product['spam_block'])}</b>\n"
        f"💎 Premium: <b>{'есть' if product['premium'] else 'нет'}</b>\n"
        f"📅 Возраст: <b>{geo.age_text(product['reg_year'])}</b>\n"
        f"{ui.LINE}\n"
        f"⭐ <b>Цена: {product['price_stars']} звёзд</b>  =  <b>{product['price_stars']} токенов</b>"
    )
    await ui.safe_edit(cb.message, text, keyboard(
        btn("⭐   Оплатить звёздами", f"buy_stars_{product['id']}", style="success"),
        btn("🪙   Оплатить токенами", f"buy_tokens_{product['id']}", style="primary"),
        btn("◀️   К списку", f"list_{key}_0"),
    ))
    await cb.answer()


# ---------- заказ ----------
async def countdown(msg: types.Message, order_id: int, base_text: str, markup):
    """Живой таймер на сообщении с оплатой: обновляется раз в 15 секунд."""
    while True:
        await asyncio.sleep(15)
        order = database.get_order(order_id)
        if order is None or order["status"] != "new":
            if order and order["status"] == "paid":
                await ui.safe_edit(msg, "✅ <b>Оплата получена!</b>\n\nДанные аккаунта отправлены сообщением ниже 👇",
                                   back_kb("back_home", "◀️   В меню"))
            return
        if order["created_at"] + config.PAYMENT_MINUTES * 60 - int(time.time()) <= 0:
            await ui.safe_edit(msg, "⌛ <b>Время оплаты истекло.</b>\n\nАккаунт снова в каталоге.",
                               keyboard(btn("🛍   В каталог", "catalog", style="primary")))
            return
        await ui.safe_edit(msg, f"{base_text}\n\n{timer_line(order['created_at'])}", markup)


@dp.callback_query(F.data.startswith("buy_tokens_"))
async def buy_tokens(cb: types.CallbackQuery):
    product = database.get_product(int(cb.data.split("_")[2]))
    if not database.is_buyable(product) or not product["synced"]:
        await cb.answer("Этот аккаунт уже забронирован или куплен!", show_alert=True)
        return
    user = cb.from_user
    balance = database.get_balance(user.id)
    if balance < product["price_stars"]:
        await cb.answer(f"Недостаточно токенов: у вас {balance}, нужно {product['price_stars']}.", show_alert=True)
        return
    database.cancel_user_pending(user.id)
    if not database.reserve_product(product["id"], user.id):
        await cb.answer("Не удалось забронировать аккаунт.", show_alert=True)
        return
    if not database.spend_balance(user.id, product["price_stars"]):
        database.release_product(product["id"], user.id)
        await cb.answer("Недостаточно токенов.", show_alert=True)
        return
    order_id = database.create_order(product["id"], user.id, product["price_stars"], method="tokens")
    await cb.answer()
    await log_action(user, f"Купил за токены: заказ #{order_id} ({product['phone']}, {product['price_stars']} 🪙)")
    log.info("[ЗАКАЗ] #%s оплачен токенами: пользователь %s → аккаунт %s", order_id, user.id, product["phone"])
    await ui.animate(cb.message, ui.loading("Списываю токены и выдаю аккаунт", 3), 0.35)
    await payments.complete_order(bot, order_id, via="токены")


@dp.callback_query(F.data.startswith("buy_stars_"))
async def buy_stars(cb: types.CallbackQuery):
    product = database.get_product(int(cb.data.split("_")[2]))
    if not database.is_buyable(product) or not product["synced"]:
        await cb.answer("Этот аккаунт уже забронирован или куплен!", show_alert=True)
        return
    user = cb.from_user
    database.cancel_user_pending(user.id)
    if not database.reserve_product(product["id"], user.id):
        await cb.answer("Не удалось забронировать аккаунт.", show_alert=True)
        return
    order_id = database.create_order(product["id"], user.id, product["price_stars"], method="gift")
    await cb.answer()
    await log_action(user, f"Оформил заказ #{order_id} ({product['phone']}, {product['price_stars']} ⭐)")
    log.info("[ЗАКАЗ] #%s создан: пользователь %s (%s) → аккаунт %s, %s ⭐. Жду оплату...",
             order_id, user.id, who(user), product["phone"], product["price_stars"])
    await ui.animate(cb.message, ui.loading("Бронирую аккаунт", 3), 0.35)

    flag, _ = geo.country_of(product["phone"])
    base_text = (
        f"💳 <b>ОПЛАТА ПОДАРКАМИ</b>  ·  заказ #{order_id}\n"
        f"{ui.LINE}\n"
        f"{flag} Аккаунт: <code>{html.escape(product['phone'])}</code>\n"
        f"⭐ К оплате: <b>{product['price_stars']} звёзд</b> (подарками на эту сумму)\n"
        f"{ui.LINE}\n\n"
        f"1️⃣ Откройте чат с @{config.ADMIN_USERNAME}\n"
        f"2️⃣ Отправьте подарок(и) на сумму не меньше <b>{product['price_stars']} ⭐</b> "
        f"<b>с этого же аккаунта</b> ({html.escape(who(user))}) — по нему бот вас узнаёт\n"
        "3️⃣ Бот сам увидит подарок и сразу выдаст данные"
    )
    if config.BETA_MODE:
        base_text += f"\n\n🧪 <b>Бета-тест:</b> достаточно написать любое сообщение в чат @{config.ADMIN_USERNAME}."
    markup = keyboard(
        btn("💬   Открыть чат для оплаты", url=f"https://t.me/{config.ADMIN_USERNAME}", style="primary"),
        btn("❌   Отменить заказ", f"cancel_order_{order_id}", style="danger"),
    )
    order = database.get_order(order_id)
    await ui.safe_edit(cb.message, f"{base_text}\n\n{timer_line(order['created_at'])}", markup)

    task = asyncio.create_task(countdown(cb.message, order_id, base_text, markup))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    admin_kb = keyboard([
        btn("✅ Подтвердить вручную", f"adm_ok_{order_id}", style="success"),
        btn("❌ Отклонить", f"adm_no_{order_id}", style="danger"),
    ])
    await payments.notify_admins(
        bot, f"🆕 Заказ #{order_id}: {html.escape(who(user))} (<code>{user.id}</code>) → "
             f"{html.escape(product['phone'])}, {product['price_stars']} ⭐", admin_kb)


@dp.callback_query(F.data.startswith("cancel_order_"))
async def cancel_order(cb: types.CallbackQuery):
    order = database.get_order(int(cb.data.split("_")[2]))
    if not order or order["user_id"] != cb.from_user.id or order["status"] != "new":
        await cb.answer("Заказ уже обработан.", show_alert=True)
        return
    database.set_order_status(order["id"], "cancelled")
    database.release_product(order["product_id"], order["user_id"])
    log.info("[ЗАКАЗ] #%s отменён покупателем", order["id"])
    await ui.safe_edit(cb.message, "❌ <b>Заказ отменён.</b>\n\nАккаунт вернулся в каталог.",
                       keyboard(btn("🛍   В каталог", "catalog", style="primary"), btn("◀️   В меню", "back_home")))
    await cb.answer()


# ---------- ручное подтверждение админом (запасной вариант) ----------
@dp.callback_query(F.data.startswith("adm_ok_"))
async def admin_confirm(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    ok = await payments.complete_order(bot, int(cb.data.split("_")[2]), via="вручную")
    await cb.answer("Выдано" if ok else "Заказ уже обработан или аккаунт занят", show_alert=not ok)
    if ok:
        await ui.safe_edit(cb.message, (cb.message.html_text or "") + "\n\n✅ Подтверждено вручную")


@dp.callback_query(F.data.startswith("adm_no_"))
async def admin_reject(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    order = database.get_order(int(cb.data.split("_")[2]))
    if not order or order["status"] != "new":
        await cb.answer("Заказ уже обработан", show_alert=True)
        return
    database.set_order_status(order["id"], "rejected")
    database.release_product(order["product_id"], order["user_id"])
    try:
        await bot.send_message(order["user_id"], f"❌ Заказ #{order['id']} отклонён. Вопросы — в поддержку.")
    except Exception:
        pass
    await ui.safe_edit(cb.message, (cb.message.html_text or "") + "\n\n❌ Отклонено")
    await cb.answer()


# ---------- после выдачи: код входа и завершение ----------
def own_paid_order(cb: types.CallbackQuery):
    order = database.get_order(int(cb.data.rsplit("_", 1)[1]))
    if not order or order["user_id"] != cb.from_user.id or order["status"] != "paid":
        return None
    if time.time() > (order["paid_at"] or 0) + config.ACCESS_HOURS * 3600:
        return None  # время на вход вышло, фоновая задача сейчас закроет заказ
    return order


@dp.callback_query(F.data.startswith("get_code_"))
async def get_code(cb: types.CallbackQuery):
    order = own_paid_order(cb)
    if not order:
        await cb.answer("Заказ не найден или уже завершён.", show_alert=True)
        return
    if time.time() - _last_code_request.get(order["id"], 0) < 10:
        await cb.answer("Подождите несколько секунд.", show_alert=True)
        return
    _last_code_request[order["id"]] = time.time()
    await cb.answer("🔎 Ищу код…")
    await bot.send_chat_action(cb.message.chat.id, "typing")

    product = database.get_product(order["product_id"])
    try:
        await code_watch.stop_watching(product["session_name"])  # не держать сессию с двух сторон разом
        code, reason = await accounts.fetch_login_code(product["session_name"])
    except Exception:
        log.exception("Ошибка получения кода")
        await cb.message.answer("⚠️ Не удалось получить код. Попробуйте ещё раз или напишите в поддержку.")
        return
    if code:
        log.info("[КОД] заказ #%s: код входа отправлен покупателю", order["id"])
        await cb.message.answer(f"🔑 <b>Код входа:</b> <code>{code}</code>\n\nВведите его в Telegram 👆")
    else:
        await cb.message.answer("🔎 Свежий код пока не найден.\nЗапросите код входа в Telegram и нажмите кнопку ещё раз.")


@dp.callback_query(F.data.startswith("finish_"))
async def finish(cb: types.CallbackQuery, state: FSMContext):
    order = own_paid_order(cb)
    if not order:
        await cb.answer("Заказ не найден или уже завершён.", show_alert=True)
        return
    await cb.answer()
    product = database.get_product(order["product_id"])
    await code_watch.stop_watching(product["session_name"])  # не держать сессию с двух сторон разом
    ok = await accounts.logout_session(product["session_name"])
    database.set_order_status(order["id"], "done")
    log.info("[ЗАКАЗ] #%s завершён, сессия продавца %s", order["id"], "закрыта" if ok else "НЕ закрыта")
    if not ok:
        await payments.notify_admins(bot, f"⚠️ Заказ #{order['id']}: не удалось закрыть сессию продавца "
                                          f"для {html.escape(product['phone'])}. Закройте вручную.")
    await state.set_state(ReviewState.text)
    await state.update_data(order_id=order["id"])
    await ui.send(
        bot, cb.message.chat.id,
        "🎉 <b>Спасибо за покупку!</b>\n\nНапишите, пожалуйста, отзыв одним сообщением — нам это очень важно 🖤",
        keyboard(btn("Пропустить", "skip_review")), effect=ui.EFFECT_HEART)


@dp.callback_query(F.data == "skip_review")
async def skip_review(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, "Спасибо! Будем рады видеть вас снова 🙌", back_kb("back_home", "◀️   В меню"))
    await cb.answer()


@dp.message(ReviewState.text, F.text, ~F.text.startswith("/"))
async def got_review(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    database.add_review(order_id, message.from_user.id, message.text)
    await payments.notify_admins(
        bot, f"⭐ <b>Новый отзыв</b> (заказ #{order_id}) от {html.escape(who(message.from_user))}:\n\n"
             f"{html.escape(message.text)}")
    await state.clear()
    await ui.send(bot, message.chat.id, "💙 <b>Спасибо за отзыв!</b>", menu_kb(is_admin(message.from_user.id)),
                  effect=ui.EFFECT_LIKE)


# ---------- мой кабинет ----------
def referral_link(user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}" if bot_username else ""


def cabinet_text(user: types.User) -> str:
    row = database.get_user_row(user.id)
    stats = database.get_user_stats(user.id)
    joined = fmt_ts(row["created_at"]) if row else "—"
    link = referral_link(user.id)
    ref_block = (f"\n{ui.LINE}\n🔗 <b>Реферальная ссылка</b> (+{config.REFERRAL_BONUS_TOKENS} токенов за друга, "
                f"когда он запустит бота):\n<code>{html.escape(link)}</code>\n"
                f"👥 Приглашено друзей: <b>{database.get_referral_count(user.id)}</b>") if link else ""
    return (
        f"👤 <b>МОЙ КАБИНЕТ</b>\n"
        f"{ui.LINE}\n"
        f"Имя: <b>{html.escape(user.full_name)}</b>\n"
        f"ID: <code>{user.id}</code>\n"
        f"В боте с: <b>{joined}</b>\n"
        f"{ui.LINE}\n"
        f"🛒 Покупок: <b>{stats['orders']}</b>\n"
        f"⭐ Потрачено: <b>{stats['spent_stars']}</b> звёзд\n"
        f"{ui.LINE}\n"
        f"💰 <b>Мой баланс: {database.get_balance(user.id)} токенов</b>"
        f"{ref_block}"
    )


def cabinet_kb():
    return keyboard(
        btn("📦   История покупок", "my_orders", style="primary"),
        btn("🎟   Активировать промокод", "promo_start", style="success"),
        btn("◀️   В меню", "back_home"),
    )


@dp.callback_query(F.data == "cabinet")
async def cabinet(cb: types.CallbackQuery):
    await log_action(cb.from_user, "Открыл личный кабинет")
    await ui.safe_edit(cb.message, cabinet_text(cb.from_user), cabinet_kb())
    await cb.answer()


@dp.callback_query(F.data == "promo_start")
async def promo_start(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.code)
    await ui.safe_edit(cb.message, "🎟 <b>Активация промокода</b>\n\nВведите код одним сообщением:",
                       keyboard(btn("❌ Отмена", "promo_cancel")))
    await cb.answer()


@dp.callback_query(F.data == "promo_cancel")
async def promo_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, cabinet_text(cb.from_user), cabinet_kb())
    await cb.answer()


@dp.message(PromoState.code, F.text, ~F.text.startswith("/"))
async def promo_redeem(message: types.Message, state: FSMContext):
    await state.clear()
    code = message.text.strip().upper()
    ok, data = database.redeem_promo(code, message.from_user.id)
    if ok:
        await log_action(message.from_user, f"Активировал промокод {code} (+{data['amount']} токенов)")
        text = f"✅ <b>Промокод активирован!</b>\n\nНачислено: <b>{data['amount']} токенов</b>\nБаланс: <b>{data['balance']} токенов</b>"
    else:
        reason = {"not_found": "такого промокода не существует", "exhausted": "лимит активаций исчерпан",
                  "already_used": "вы уже активировали этот код"}.get(data, "не удалось активировать")
        text = f"❌ <b>Не получилось:</b> {reason}."
    await message.answer(text, reply_markup=cabinet_kb())


# ---------- рулетка счастья ----------
@dp.callback_query(F.data == "roulette_home")
async def roulette_home(cb: types.CallbackQuery):
    await log_action(cb.from_user, "Открыл рулетку счастья")
    last_spin = database.get_last_roulette_time(cb.from_user.id)
    diff = time.time() - last_spin
    daily_ready = diff >= ROULETTE_COOLDOWN

    time_left_str = ""
    if not daily_ready:
        rem = int(ROULETTE_COOLDOWN - diff)
        time_left_str = f"\n⏳ Ежедневная попытка будет доступна через: <b>{rem // 3600}ч. {(rem % 3600) // 60}мин.</b>"

    text = (
        "🎰✨ <b>КАЗИНО УДАЧИ</b> ✨🎰\n"
        f"{ui.LINE}\n"
        "Испытай удачу и выиграй ценные призы!\n\n"
        "🎁 <b>Шансы выигрыша:</b>\n"
        "🕶 Анонимный номер — <b>1%</b>\n"
        "📱 Рабочий номер — <b>7%</b>\n"
        "💰 Пополнение счёта (5–15 токенов) — <b>5%</b>\n"
        "💨 Пусто — <i>повезёт в следующий раз!</i>\n"
        f"{ui.LINE}{time_left_str}"
    )
    rows = [btn("🎁  Крутить ежедневную (бесплатно)", "spin_daily", style="success")] if daily_ready else \
           [btn("🔒  Ежедневная уже использована", "noop", style="danger")]
    rows += [btn("⭐  Крутить за 15 звёзд", "spin_paid", style="primary"), btn("◀️   В меню", "back_home")]
    await ui.safe_edit(cb.message, text, keyboard(*rows))
    await cb.answer()


@dp.callback_query(F.data == "noop")
async def noop_cb(cb: types.CallbackQuery):
    await cb.answer("⏳ Время бесплатной прокрутки ещё не настало!", show_alert=True)


SPIN_PRICE_TOKENS = 15


@dp.callback_query(F.data == "spin_paid")
async def spin_paid_menu(cb: types.CallbackQuery):
    await ui.safe_edit(
        cb.message,
        f"⭐ <b>Платная прокрутка</b>\n{ui.LINE}\n\n"
        f"Баланс: <b>{database.get_balance(cb.from_user.id)} токенов</b> 🪙\n\n"
        f"Можно крутить мгновенно за {SPIN_PRICE_TOKENS} токенов, либо подарком в звёздах "
        "(подтверждается вручную).",
        keyboard(
            btn(f"🪙   Крутить за {SPIN_PRICE_TOKENS} токенов", "spin_tokens", style="success"),
            btn("⭐   Подарком (вручную)", "spin_gift_info", style="primary"),
            btn("◀️   Назад в рулетку", "roulette_home"),
        ))
    await cb.answer()


@dp.callback_query(F.data == "spin_gift_info")
async def spin_gift_info(cb: types.CallbackQuery):
    await log_action(cb.from_user, f"Запросил платную прокрутку подарком ({SPIN_PRICE_TOKENS} ⭐)")
    await cb.message.answer(
        f"⭐ <b>Оплата прокрутки подарком</b>\n\n"
        f"Отправьте подарок на сумму <b>{SPIN_PRICE_TOKENS} ⭐</b> в чат @{config.ADMIN_USERNAME} и напишите нам "
        "для начисления прокрутки — это подтверждается вручную.",
        reply_markup=keyboard(btn("💬   Открыть чат", url=f"https://t.me/{config.ADMIN_USERNAME}", style="primary"),
                              btn("◀️ Назад в рулетку", "roulette_home")))
    await cb.answer()


@dp.callback_query(F.data == "spin_tokens")
async def spin_tokens(cb: types.CallbackQuery):
    user = cb.from_user
    if not database.spend_balance(user.id, SPIN_PRICE_TOKENS):
        await cb.answer(f"Недостаточно токенов: нужно {SPIN_PRICE_TOKENS}, у вас "
                        f"{database.get_balance(user.id)}.", show_alert=True)
        return
    await log_action(user, f"Крутит платную рулетку за {SPIN_PRICE_TOKENS} токенов")
    await cb.answer()
    await do_spin(cb.message, user)


@dp.callback_query(F.data == "spin_daily")
async def spin_daily(cb: types.CallbackQuery):
    user = cb.from_user
    last_spin = database.get_last_roulette_time(user.id)
    if time.time() - last_spin < ROULETTE_COOLDOWN:
        await cb.answer("Бесплатная попытка недоступна!", show_alert=True)
        return
    database.set_last_roulette_time(user.id)
    await log_action(user, "Запустил бесплатную ежедневную рулетку")
    await cb.answer()
    await do_spin(cb.message, user)


async def do_spin(message: types.Message, user: types.User):
    reel = ["🍋", "🍒", "💎", "7️⃣", "📱", "🎁", "🕶", "⭐"]
    header = "🎰✨ <b>К А З И Н О   У Д А Ч И</b> ✨🎰"
    spins = [
        (0.22, "Разгоняем барабан"), (0.22, "Разгоняем барабан"), (0.28, "Крутим"),
        (0.28, "Крутим"), (0.35, "Крутим"), (0.45, "Замедляемся"), (0.55, "Почти остановились"),
    ]
    for delay, label in spins:
        row = " | ".join(random.choice(reel) for _ in range(3))
        await ui.safe_edit(message, f"{header}\n{ui.LINE}\n\n🎲 <i>[ {row} ]</i>\n\n⏳ {label}...")
        await asyncio.sleep(delay)
    await ui.safe_edit(message, f"{header}\n{ui.LINE}\n\n🎲 <i>[ ❓ | ❓ | ❓ ]</i>\n\n💫 Открываем результат...")
    await asyncio.sleep(0.5)

    # Честные шансы: 1% / 7% / 5% / остальное — ровно те, что показаны выше
    roll = random.random() * 100
    if roll <= 1:
        result_text = (
            "🎉✨ <b>ДЖЕКПОТ! НЕВЕРОЯТНО!</b> ✨🎉\n"
            f"{ui.LINE}\n\n🕶🕶🕶\n\n"
            "Вы выиграли <b>Анонимный номер</b> (шанс 1%)!\n"
            f"Администратор @{config.ADMIN_USERNAME} уже получил уведомление и выдаст вам приз!"
        )
        await log_action(user, "👑 ВЫИГРАЛ АНОНИМНЫЙ НОМЕР В РУЛЕТКЕ (1%)!")
        await payments.notify_admins(bot, f"👑 <b>ПОБЕДА В РУЛЕТКЕ!</b> {who(user)} выбил АНОНИМНЫЙ НОМЕР!")
    elif roll <= 8:
        result_text = (
            "🎁✨ <b>ПОЗДРАВЛЯЕМ!</b> ✨🎁\n"
            f"{ui.LINE}\n\n📱 Удача на вашей стороне!\n\n"
            "Вы выиграли <b>Рабочий номер</b> (шанс 7%)!\n"
            f"Напишите @{config.ADMIN_USERNAME} для получения награды."
        )
        await log_action(user, "🏆 Выиграл рабочий номер в рулетке (7%)")
        await payments.notify_admins(bot, f"🏆 {who(user)} выиграл рабочий номер в рулетке!")
    elif roll <= 13:
        amount = random.randint(5, 15)
        new_balance = database.add_balance(user.id, amount)
        result_text = (
            "💰✨ <b>ПОПОЛНЕНИЕ СЧЁТА!</b> ✨💰\n"
            f"{ui.LINE}\n\n"
            f"Вам начислено <b>{amount} токенов</b> (шанс 5%)!\n"
            f"Текущий баланс: <b>{new_balance} токенов</b> 🪙."
        )
        await log_action(user, f"💰 Выиграл пополнение счёта в рулетке: +{amount} токенов (5%)")
    else:
        result_text = (
            "💨 <b>Эх, в этот раз пусто!</b>\n"
            f"{ui.LINE}\n\n"
            "Фортуна была не на вашей стороне, но можно попробовать снова или заглянуть в каталог."
        )
        await log_action(user, "Ничего не выиграл в рулетке (пусто)")

    await asyncio.sleep(0.4)
    await ui.safe_edit(message, result_text, keyboard(
        btn("🔄 Ещё попытка", "roulette_home"), btn("🛍 В каталог", "catalog", style="primary"),
        btn("◀️ В меню", "back_home")))


# ---------- админ-панель: добавление аккаунтов прямо из бота ----------
def admin_panel_text() -> str:
    return (
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n"
        f"{ui.LINE}\n"
        "📲 <b>Войти в аккаунт</b> — настоящий вход по номеру и коду (как через консоль), прямо в этом чате. "
        "После входа аккаунт считается «синхронизированным»: код, который придёт на него при входе покупателя, "
        "бот сам поймает и отправит покупателю автоматически, плюс копию пришлёт тебе сюда.\n\n"
        "➕ <b>Добавить вручную</b> — просто карточка в каталоге без входа (для аккаунтов, в которые ты сам "
        "зайти не можешь). При покупке бот попросит покупателя написать тебе напрямую.\n\n"
        f"{ui.LINE}\n"
        "<b>Команды:</b>\n"
        "<code>/stock</code> — склад\n"
        "<code>/setprice ID ЗВЁЗДЫ</code> — изменить цену\n"
        "<code>/remove ID</code> — снять с продажи\n"
        "<code>/getcode ID</code> — вручную вытащить код для синхронизированного аккаунта (переслать через /sms)\n"
        "<code>/addbalance ID СУММА</code> — начислить/списать токены пользователю\n"
        "<code>/newpromo КОД СУММА [ЛИМИТ]</code> — создать промокод (лимит по умолчанию 1)\n"
        "<code>/sms ID ТЕКСТ</code> — личное сообщение пользователю\n"
        "<code>/support ID ТЕКСТ</code> — ответ на обращение в поддержку"
    )


def admin_panel_kb():
    return keyboard(
        btn("📲   Войти в аккаунт (реальный вход)", "admin_login_start", style="primary"),
        btn("➕   Добавить вручную (без входа)", "admin_add_start", style="success"),
        btn("◀️   В меню", "back_home"),
    )


@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await ui.safe_edit(cb.message, admin_panel_text(), admin_panel_kb())
    await cb.answer()


# ---------- реальный вход в аккаунт прямо из бота ----------
@dp.callback_query(F.data == "admin_login_start")
async def admin_login_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    admin_id = cb.from_user.id
    if admin_id in _pending_logins:
        await _abort_pending_login(admin_id)
    session_name = accounts.next_free_session_name()
    client = accounts.make_client(session_name)
    await client.connect()
    _pending_logins[admin_id] = {"client": client, "session_name": session_name}
    await state.set_state(AdminLoginState.phone)
    await ui.safe_edit(
        cb.message,
        "📲 <b>Вход в новый аккаунт</b>\n"
        f"{ui.LINE}\n\n"
        "Введите номер телефона, например <code>+49123456789</code>:",
        keyboard(btn("❌ Отмена", "admin_login_cancel")))
    await cb.answer()


@dp.callback_query(F.data == "admin_login_cancel")
async def admin_login_cancel(cb: types.CallbackQuery, state: FSMContext):
    await _abort_pending_login(cb.from_user.id)
    await state.clear()
    await ui.safe_edit(cb.message, admin_panel_text(), admin_panel_kb())
    await cb.answer()


@dp.message(AdminLoginState.phone, F.text, ~F.text.startswith("/"))
async def admin_login_phone(message: types.Message, state: FSMContext):
    admin_id = message.from_user.id
    if not is_admin(admin_id) or admin_id not in _pending_logins:
        return
    client = _pending_logins[admin_id]["client"]
    phone = message.text.strip().replace(" ", "")
    try:
        sent = await client.send_code_request(phone)
    except errors.PhoneNumberInvalidError:
        await message.answer("⚠️ Неверный номер. Формат: <code>+49123456789</code>. Введите ещё раз:")
        return
    except errors.PhoneNumberBannedError:
        await _abort_pending_login(admin_id)
        await state.clear()
        await message.answer("⛔ Этот номер заблокирован Telegram — войти нельзя.", reply_markup=admin_panel_kb())
        return
    except (errors.PhoneNumberFloodError, errors.FloodWaitError) as e:
        wait = getattr(e, "seconds", None)
        await _abort_pending_login(admin_id)
        await state.clear()
        hint = f" (~{wait // 60} мин.)" if wait else ""
        await message.answer(f"⛔ Telegram просит подождать{hint} и повторить позже.", reply_markup=admin_panel_kb())
        return
    except errors.ApiIdInvalidError:
        await _abort_pending_login(admin_id)
        await state.clear()
        await message.answer("⛔ API_ID / API_HASH неверные — проверь .env.", reply_markup=admin_panel_kb())
        return
    _pending_logins[admin_id].update(phone=phone, sent=sent)
    await state.set_state(AdminLoginState.code)
    await message.answer(
        f"📨 Код отправлен: {accounts.describe_code_delivery(sent)}\n\n"
        f"ℹ️ Если не придёт — нажмите «Отправить другим способом» (следующий способ: "
        f"{accounts.next_code_hint(sent)}).\n\nВведите код:",
        reply_markup=keyboard(btn("🔁 Отправить другим способом", "admin_login_resend"),
                              btn("❌ Отмена", "admin_login_cancel")))


@dp.callback_query(F.data == "admin_login_resend", AdminLoginState.code)
async def admin_login_resend(cb: types.CallbackQuery):
    admin_id = cb.from_user.id
    if not is_admin(admin_id) or admin_id not in _pending_logins:
        await cb.answer("Нет активного входа", show_alert=True)
        return
    data = _pending_logins[admin_id]
    client, phone, sent = data["client"], data["phone"], data["sent"]
    try:
        sent = await client(functions.auth.ResendCodeRequest(phone_number=phone, phone_code_hash=sent.phone_code_hash))
        data["sent"] = sent
        await cb.message.answer(f"📨 Код отправлен: {accounts.describe_code_delivery(sent)}\n\n"
                                f"Следующий способ, если снова не придёт: {accounts.next_code_hint(sent)}")
    except Exception as e:
        await cb.message.answer(f"⚠️ Не удалось отправить повторно: {e}")
    await cb.answer()


@dp.message(AdminLoginState.code, F.text, ~F.text.startswith("/"))
async def admin_login_code(message: types.Message, state: FSMContext):
    admin_id = message.from_user.id
    if not is_admin(admin_id) or admin_id not in _pending_logins:
        return
    data = _pending_logins[admin_id]
    client, phone, sent = data["client"], data["phone"], data["sent"]
    code = message.text.strip()
    try:
        await message.delete()  # код — чувствительные данные, чистим переписку
    except Exception:
        pass
    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        await state.set_state(AdminLoginState.password)
        await message.answer("🔐 На аккаунте включён облачный пароль. Введите его "
                             "(сообщение будет сразу удалено из переписки):",
                             reply_markup=keyboard(btn("❌ Отмена", "admin_login_cancel")))
        return
    except errors.PhoneCodeInvalidError:
        await message.answer("⚠️ Неверный код, попробуйте ещё раз:")
        return
    except errors.PhoneCodeExpiredError:
        await message.answer("⚠️ Код устарел — нажмите «Отправить другим способом» выше.")
        return
    data["cloud_password"] = ""
    await _admin_login_ask_price(message, state)


@dp.message(AdminLoginState.password, F.text, ~F.text.startswith("/"))
async def admin_login_password(message: types.Message, state: FSMContext):
    admin_id = message.from_user.id
    if not is_admin(admin_id) or admin_id not in _pending_logins:
        return
    data = _pending_logins[admin_id]
    password = message.text.strip()
    try:
        await message.delete()  # пароль — чувствительные данные, чистим переписку
    except Exception:
        pass
    try:
        await data["client"].sign_in(password=password)
    except errors.PasswordHashInvalidError:
        await message.answer("⚠️ Пароль неверный, попробуйте ещё раз (сообщение будет удалено):")
        return
    data["cloud_password"] = password
    await _admin_login_ask_price(message, state)


async def _admin_login_ask_price(message: types.Message, state: FSMContext):
    await state.set_state(AdminLoginState.price)
    await message.answer("✅ <b>Вход выполнен!</b>\n\n⭐ Теперь введите цену в звёздах (= столько же токенов):")


@dp.message(AdminLoginState.price, F.text, ~F.text.startswith("/"))
async def admin_login_price(message: types.Message, state: FSMContext):
    admin_id = message.from_user.id
    if not is_admin(admin_id) or admin_id not in _pending_logins:
        return
    if not message.text.strip().isdigit():
        await message.answer("⚠️ Нужно целое число. Введите цену ещё раз:")
        return
    _pending_logins[admin_id]["price"] = int(message.text.strip())
    await state.set_state(AdminLoginState.year)
    await message.answer("📅 Укажите год регистрации аккаунта (например <code>2021</code>) "
                         "или отправьте «-», чтобы оценить автоматически:")


@dp.message(AdminLoginState.year, F.text, ~F.text.startswith("/"))
async def admin_login_year(message: types.Message, state: FSMContext):
    admin_id = message.from_user.id
    if not is_admin(admin_id) or admin_id not in _pending_logins:
        return
    data = _pending_logins.pop(admin_id)
    await state.clear()
    client = data["client"]

    txt = message.text.strip()
    me = await client.get_me()
    year = geo.estimate_reg_year(me.id)
    if txt != "-":
        if txt.isdigit() and 2013 <= int(txt) <= 2026:
            year = int(txt)

    await message.answer("🔎 Проверяю спам-блок через @SpamBot...")
    spam = await accounts.check_spam(client)
    phone = ("+" + me.phone) if me.phone else data["phone"]
    premium = 1 if getattr(me, "premium", False) else 0
    await client.disconnect()  # сессия остаётся сохранённой на диске для автовыдачи и входа покупателя

    pid = database.add_product(phone, me.id, spam, data["price"], data["session_name"],
                               data["cloud_password"], premium, year, synced=1)
    flag, country = geo.country_of(phone)
    await message.answer(
        f"🎉 <b>Готово!</b> Карточка #{pid} добавлена в каталог как <b>синхронизированная</b>.\n\n"
        f"{flag} {html.escape(phone)} ({country})\n"
        f"🛡 Спам-блок: {spam}   ·   💎 {'да' if premium else 'нет'}   ·   📅 {year}\n"
        f"⭐ {data['price']} звёзд / 🪙 {data['price']} токенов\n\n"
        "Когда аккаунт купят и покупатель начнёт вход — код автоматически придёт и ему, и тебе сюда.",
        reply_markup=keyboard(btn("📲 Войти ещё в один", "admin_login_start", style="primary"),
                              btn("◀️ В меню", "back_home")))


@dp.callback_query(F.data == "admin_add_start")
async def admin_add_start(cb: types.CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminAddState.phone)
    await ui.safe_edit(cb.message, "➕ <b>Добавление аккаунта (вручную)</b>\n\nВведите номер телефона, "
                                   "например <code>+49123456789</code>:", keyboard(btn("❌ Отмена", "admin_add_cancel")))
    await cb.answer()


@dp.callback_query(F.data == "admin_add_cancel")
async def admin_add_cancel(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, admin_panel_text(), admin_panel_kb())
    await cb.answer()


@dp.message(AdminAddState.phone, F.text, ~F.text.startswith("/"))
async def admin_add_phone(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    phone = message.text.strip()
    if not phone.startswith("+") or not phone[1:].replace(" ", "").isdigit():
        await message.answer("⚠️ Формат номера: <code>+49123456789</code>. Введите ещё раз:")
        return
    await state.update_data(phone=phone)
    await state.set_state(AdminAddState.price)
    await message.answer("⭐ Введите цену в звёздах (целое число):")


@dp.message(AdminAddState.price, F.text, ~F.text.startswith("/"))
async def admin_add_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not message.text.strip().isdigit():
        await message.answer("⚠️ Нужно целое число. Введите цену ещё раз:")
        return
    await state.update_data(price=int(message.text.strip()))
    await state.set_state(AdminAddState.premium)
    await message.answer("💎 Есть ли на аккаунте Telegram Premium?",
                         reply_markup=keyboard([btn("💎 Да", "admin_prem_yes", style="success"),
                                                btn("— Нет", "admin_prem_no")]))


@dp.callback_query(AdminAddState.premium, F.data.in_({"admin_prem_yes", "admin_prem_no"}))
async def admin_add_premium(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(premium=1 if cb.data == "admin_prem_yes" else 0)
    await state.set_state(AdminAddState.year)
    await ui.safe_edit(cb.message, "📅 Укажите год регистрации аккаунта (например <code>2021</code>) "
                                   "или отправьте «-», если не знаете:")
    await cb.answer()


@dp.message(AdminAddState.year, F.text, ~F.text.startswith("/"))
async def admin_add_year(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    txt = message.text.strip()
    year = None
    if txt != "-":
        if not (txt.isdigit() and 2013 <= int(txt) <= 2026):
            await message.answer("⚠️ Введите год цифрами (2013–2026) или «-»:")
            return
        year = int(txt)
    data = await state.get_data()
    await state.clear()
    pid = database.add_manual_product(data["phone"], data["price"], data["premium"], year)
    flag, country = geo.country_of(data["phone"])
    await message.answer(
        f"🎉 <b>Готово!</b> Карточка #{pid} добавлена в каталог.\n\n"
        f"{flag} {html.escape(data['phone'])} ({country})\n"
        f"⭐ {data['price']} звёзд   ·   💎 {'да' if data['premium'] else 'нет'}   ·   "
        f"📅 {year or 'не указан'}\n\n"
        "Эта карточка помечена как «не синхронизирована»: когда покупатель попытается её купить, "
        "бот направит его написать тебе напрямую.",
        reply_markup=keyboard(btn("➕ Добавить ещё", "admin_add_start", style="success"),
                              btn("◀️ В меню", "back_home")))


# ---------- команды админа ----------
@dp.message(Command("stock"))
async def cmd_stock(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    rows = database.list_products()
    if not rows:
        await message.answer("Склад пуст. Добавьте аккаунты: python add_account.py")
        return
    lines = [f"#{p['id']} | {geo.country_of(p['phone'])[0]} {html.escape(p['phone'])} | спам: "
             f"{html.escape(p['spam_block'])} | {'💎' if p['premium'] else '—'} | {geo.age_short(p['reg_year']) or '?'} | "
             f"{p['price_stars']} ⭐ | {p['status']}" for p in rows]
    await message.answer("<b>Склад:</b>\n" + "\n".join(lines))


@dp.message(Command("setprice"))
async def cmd_setprice(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Формат: /setprice ID ЗВЁЗДЫ\nНапример: /setprice 3 100")
        return
    ok = database.set_price(int(parts[1]), int(parts[2]))
    await message.answer("✅ Цена обновлена" if ok else "Не нашёл такой товар (или он уже продан)")


@dp.message(Command("remove"))
async def cmd_remove(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /remove ID")
        return
    ok = database.remove_product(int(parts[1]))
    await message.answer("✅ Снят с продажи" if ok else "Не получилось (нет такого или уже продан)")


@dp.message(Command("getcode"))
async def cmd_getcode(message: types.Message):
    """Вручную вытащить текущий код для синхронизированного аккаунта (на всякий случай — помимо автовыдачи)."""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /getcode ID")
        return
    product = database.get_product(int(parts[1]))
    if not product or not product["synced"] or not product["session_name"]:
        await message.answer("Это не синхронизированный аккаунт — входа в него нет.")
        return
    await code_watch.stop_watching(product["session_name"])  # не держать сессию с двух сторон разом
    code, reason = await accounts.fetch_login_code(product["session_name"])
    if code:
        order = database.get_paid_order_for_product(product["id"])
        hint = f"\n\nПерешлите покупателю: <code>/sms {order['user_id']} Код входа: {code}</code>" if order else ""
        await message.answer(f"🔑 Код для <code>{html.escape(product['phone'])}</code>: <code>{code}</code>{hint}")
    else:
        await message.answer("Свежего кода пока нет.")


@dp.message(Command("addbalance"))
async def cmd_addbalance(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not (parts[2].lstrip("-").isdigit()):
        await message.answer("Формат: /addbalance ID СУММА\nПример: /addbalance 123456789 10")
        return
    target_id, amount = int(parts[1]), int(parts[2])
    new_balance = database.add_balance(target_id, amount)
    await message.answer(f"✅ Баланс <code>{target_id}</code>: {new_balance} токенов")
    try:
        sign = "+" if amount >= 0 else ""
        await bot.send_message(target_id, f"💰 Администрация начислила вам {sign}{amount} токенов. "
                                          f"Текущий баланс: <b>{new_balance}</b>.")
    except Exception:
        pass


@dp.message(Command("newpromo"))
async def cmd_newpromo(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) not in (3, 4) or not parts[2].isdigit() or (len(parts) == 4 and not parts[3].isdigit()):
        await message.answer("Формат: /newpromo КОД СУММА [ЛИМИТ]\nПример: /newpromo WELCOME10 10 50")
        return
    code = parts[1].strip().upper()
    amount = int(parts[2])
    max_uses = int(parts[3]) if len(parts) == 4 else 1
    database.create_promo(code, amount, max_uses)
    await message.answer(f"✅ Промокод <code>{code}</code> создан: +{amount} токенов, лимит активаций {max_uses}")


@dp.message(Command("support"))
async def cmd_support(message: types.Message):
    """Ответить на обращение в поддержку: /support ID текст"""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("⚠️ Формат: <code>/support ID_ПОЛЬЗОВАТЕЛЯ ТЕКСТ_ОТВЕТА</code>")
        return
    target_id, text_to_send = int(parts[1]), parts[2]
    try:
        await bot.send_message(
            target_id, f"🆘 <b>Ответ поддержки:</b>\n\n{html.escape(text_to_send)}",
            reply_markup=back_kb("back_home", "◀️   В меню"))
        await message.answer(f"✅ Ответ отправлен пользователю <code>{target_id}</code>")
        log.info("[ПОДДЕРЖКА] %s → %s: %s", message.from_user.id, target_id, text_to_send)
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.message(Command("sms"))
async def cmd_sms(message: types.Message):
    """Отправить личное сообщение любому пользователю бота: /sms ID текст"""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("⚠️ Формат: <code>/sms ID_ПОЛЬЗОВАТЕЛЯ ТЕКСТ_СООБЩЕНИЯ</code>")
        return
    target_id, text_to_send = int(parts[1]), parts[2]
    try:
        await bot.send_message(target_id, f"💬 <b>Сообщение от администрации:</b>\n\n{html.escape(text_to_send)}")
        await message.answer(f"✅ Отправлено пользователю <code>{target_id}</code>")
        log.info("[SMS АДМИНА] %s → %s: %s", message.from_user.id, target_id, text_to_send)
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


# ---------- фон и запуск ----------
async def background_loop():
    """Раз в минуту: закрывает неоплаченные заказы и заказы, у которых вышло время на вход."""
    while True:
        try:
            for o in database.expire_orders():
                log.info("[ЗАКАЗ] #%s просрочен", o["id"])
                try:
                    await bot.send_message(
                        o["user_id"], f"⌛ Время оплаты заказа #{o['id']} истекло. Если вы уже оплатили — "
                                      f"напишите в поддержку @{config.ADMIN_USERNAME}.")
                except Exception:
                    pass
            for o in database.get_expired_access_orders():
                product = database.get_product(o["product_id"])
                await code_watch.stop_watching(product["session_name"])  # не держать сессию с двух сторон разом
                ok = await accounts.logout_session(product["session_name"])
                database.set_order_status(o["id"], "done")
                log.info("[ЗАКАЗ] #%s: время на вход вышло, сессия продавца %s",
                         o["id"], "закрыта" if ok else "НЕ закрыта")
                try:
                    await bot.send_message(
                        o["user_id"], f"⌛ Время на вход в аккаунт по заказу #{o['id']} "
                                      f"({config.ACCESS_HOURS:g} ч.) истекло, доступ закрыт.")
                except Exception:
                    pass
                if not ok:
                    await payments.notify_admins(
                        bot, f"⚠️ Заказ #{o['id']}: время на вход вышло, но сессию продавца для "
                             f"{html.escape(product['phone'])} закрыть не удалось. Закройте вручную.")
        except Exception:
            log.exception("Ошибка фоновой задачи")
        await asyncio.sleep(60)


async def main():
    global bot_username
    database.init_db()
    me = await bot.get_me()
    bot_username = me.username or ""
    if config.SUB_CHANNEL:
        try:
            await bot.get_chat_member(f"@{config.SUB_CHANNEL}", me.id)
        except Exception:
            log.warning("⚠️ Бот не похож на участника канала @%s — добавь его туда админом, "
                       "иначе проверка подписки не будет работать!", config.SUB_CHANNEL)
    tasks = [
        asyncio.create_task(background_loop()),
        asyncio.create_task(gifts.run_gift_watcher(bot)),
        asyncio.create_task(code_watch.run_code_watch_manager(bot)),
    ]
    log.info("🚀 Бот запущен%s", " | БЕТА-РЕЖИМ: любое сообщение на аккаунт = оплата" if config.BETA_MODE else "")
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
