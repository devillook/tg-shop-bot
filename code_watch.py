# code_watch.py — слушает каждый ПРОДАННЫЙ (оплаченный) аккаунт и автоматически пересылает
# пришедший код входа покупателю + копию тебе на основной аккаунт.
#
# Безопасность: код отправляется покупателю ТОЛЬКО если для этого аккаунта прямо сейчас
# существует ровно один активный оплаченный заказ (status='paid'), и получатель — именно
# user_id из этого заказа. Если заказа нет или аккаунт уже не продаётся — код никому не шлём,
# только уведомляем админа, чтобы разобраться вручную.
import asyncio
import html
import logging
import re

from aiogram import Bot
from telethon import events

import accounts
import config
import database
import payments

log = logging.getLogger("code_watch")

CODE_RE = re.compile(r"\b(\d{5})\b")
POLL_SECONDS = 20

# session_name -> {"task": asyncio.Task, "client": TelegramClient}. Общий на весь модуль,
# чтобы main.py мог попросить "отпустить" конкретный файл сессии перед тем, как сам
# откроет его (код входа / logout) — иначе два одновременных подключения к одному .session
# упираются в ту же блокировку SQLite, что уже ловили при входе через консоль.
_watchers: dict[str, dict] = {}


async def stop_watching(session_name: str):
    """Останавливает слежение за конкретным аккаунтом и ЖДЁТ, пока файл сессии освободится.
    Вызывай перед accounts.fetch_login_code / accounts.logout_session для этой же сессии."""
    entry = _watchers.pop(session_name, None)
    if not entry:
        return
    entry["task"].cancel()
    try:
        await entry["client"].disconnect()
    except Exception:
        pass


async def _watch_one(bot: Bot, session_name: str, client):
    await client.connect()
    if not await client.is_user_authorized():
        log.warning("[АВТОКОД] сессия %s не авторизована — слежение не запущено", session_name)
        await client.disconnect()
        return

    @client.on(events.NewMessage(chats=777000))
    async def on_code_message(event):
        m = CODE_RE.search(event.raw_text or "")
        if not m:
            return
        code = m.group(1)
        product = database.get_product_by_session(session_name)
        if not product:
            log.warning("[АВТОКОД] код для сессии %s, но товар не найден в базе", session_name)
            return
        order = database.get_paid_order_for_product(product["id"])
        if not order:
            log.info("[АВТОКОД] код для %s, но активного оплаченного заказа нет — только уведомляю админа",
                     product["phone"])
            await payments.notify_admins(
                bot, f"🔑 Пришёл код для <code>{html.escape(product['phone'])}</code>, но активного заказа "
                     f"на него нет: <code>{code}</code>")
            return
        text = f"🔑 <b>Код входа:</b> <code>{code}</code>\n\nВведите его в Telegram 👆"
        try:
            await bot.send_message(order["user_id"], text)
            log.info("[АВТОКОД] заказ #%s: код отправлен покупателю автоматически", order["id"])
        except Exception:
            log.exception("[АВТОКОД] не удалось отправить код покупателю")
        await payments.notify_admins(
            bot, f"🔑 Код для <code>{html.escape(product['phone'])}</code> (заказ #{order['id']}, покупатель "
                 f"<code>{order['user_id']}</code>) отправлен автоматически: <code>{code}</code>")

    try:
        await client.catch_up()
    except Exception:
        log.exception("[АВТОКОД] catch_up не удался для %s", session_name)
    log.info("[АВТОКОД] слежение запущено: %s", session_name)
    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_code_watch_manager(bot: Bot):
    """Держит по одному слушателю на каждый СЕЙЧАС проданный и ожидающий входа аккаунт."""
    while True:
        try:
            active = {o["session_name"] for o in database.get_active_paid_orders()}

            for session_name in list(_watchers):
                if session_name not in active:
                    await stop_watching(session_name)

            for session_name in active:
                entry = _watchers.get(session_name)
                if entry is None or entry["task"].done():
                    client = accounts.make_client(session_name)
                    task = asyncio.create_task(_watch_one_with_client(bot, session_name, client))
                    _watchers[session_name] = {"task": task, "client": client}
        except Exception:
            log.exception("[АВТОКОД] ошибка менеджера слежения")
        await asyncio.sleep(POLL_SECONDS)


async def _watch_one_with_client(bot: Bot, session_name: str, client):
    """Обёртка: использует уже созданный клиент (его же хранит _watchers для stop_watching)."""
    await _watch_one(bot, session_name, client)
