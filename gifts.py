# gifts.py — слушает твой аккаунт (@jonnkq): подарки со звёздами и (в бета-режиме) обычные сообщения
import asyncio
import logging
import math

from aiogram import Bot
from telethon import events, types

import accounts
import config
import database
import payments

log = logging.getLogger("gifts")
GIFT_ACTIONS = ("MessageActionStarGift", "MessageActionStarGiftUnique")


async def handle_gift(bot: Bot, sender_id: int, msg_id: int, stars: int | None):
    if not database.record_gift(sender_id, msg_id, stars):
        return  # этот подарок уже обработан

    order = database.get_pending_order(sender_id)
    if order is None:
        log.info("[ПОДАРОК] от %s, но активного заказа нет", sender_id)
        await payments.notify_admins(
            bot, f"🎁 Подарок ({stars if stars else '?'} ⭐) от <code>{sender_id}</code>, но активного заказа "
                 "у этого пользователя нет. Разберитесь вручную.")
        return
    if stars is None:
        await payments.notify_admins(
            bot, f"🎁 От <code>{sender_id}</code> пришёл подарок без известной стоимости (коллекционный). "
                 f"Заказ #{order['id']} — подтвердите вручную кнопкой в заявке.")
        return

    total = database.add_paid_stars(order["id"], stars)
    need = order["price_stars"]
    # «закрываем глаза» на комиссию: достаточно (100 - GIFT_TOLERANCE_PERCENT)% от цены
    threshold = need * (1 - config.GIFT_TOLERANCE_PERCENT / 100)
    log.info("[ПОДАРОК] заказ #%s: получено %s ⭐, цена %s ⭐, достаточно от %.1f ⭐",
             order["id"], total, need, threshold)
    if total + 1e-9 >= threshold:
        await payments.complete_order(bot, order["id"], via="подарок")
    else:
        try:
            await bot.send_message(
                sender_id, f"🎁 Получено {total} ⭐ из {need} ⭐. Отправьте ещё подарок(и) на "
                           f"{math.ceil(threshold - total)} ⭐ — и аккаунт выдастся автоматически.")
        except Exception:
            pass


async def _watch(bot: Bot):
    client = accounts.make_client(config.OWNER_SESSION)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Сессия владельца не авторизована. Запусти: python login_owner.py")
        await client.disconnect()
        return False

    # --- подарки (служебные сообщения) ---
    @client.on(events.Raw(types.UpdateNewMessage))
    async def on_service(update):
        msg = update.message
        if not isinstance(msg, types.MessageService) or msg.out:
            return
        name = type(msg.action).__name__
        if name not in GIFT_ACTIONS or not isinstance(msg.peer_id, types.PeerUser):
            return
        action = msg.action
        if getattr(action, "refunded", False) or getattr(action, "upgrade_separate", False):
            return
        stars = getattr(getattr(action, "gift", None), "stars", None)
        log.info("[ПОДАРОК] получен от %s: %s ⭐", msg.peer_id.user_id, stars)
        try:
            await handle_gift(bot, msg.peer_id.user_id, msg.id, stars)
        except Exception:
            log.exception("Ошибка обработки подарка")

    # --- обычные сообщения: видны в консоли; в бета-режиме считаются оплатой ---
    @client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if not event.is_private:
            return
        user_id = event.sender_id
        sender = await event.get_sender()
        uname = f"@{sender.username}" if getattr(sender, "username", None) else "без username"
        log.info("[ВХОДЯЩЕЕ] от %s (%s): %s", user_id, uname, (event.raw_text or "<не текст>")[:200])

        if not config.BETA_MODE:
            return
        if config.BETA_USER_IDS and user_id not in config.BETA_USER_IDS:
            log.info("[БЕТА] %s не в списке BETA_USER_IDS — игнор", user_id)
            return
        order = database.get_pending_order(user_id)
        if order is None:
            log.info("[БЕТА] у %s нет активного заказа в боте — ничего не выдаю", user_id)
            return
        log.info("[БЕТА] оплата засчитана: заказ #%s, пользователь %s", order["id"], user_id)
        try:
            await payments.complete_order(bot, order["id"], via="бета-тест")
        except Exception:
            log.exception("Ошибка выдачи в бета-режиме")

    try:
        await client.catch_up()  # подхватить подарки, пришедшие, пока бот был выключен
    except Exception:
        log.exception("catch_up не удался")
    log.info("🎁 Слушатель аккаунта запущен%s", " (БЕТА-РЕЖИМ ВКЛЮЧЁН)" if config.BETA_MODE else "")
    await client.run_until_disconnected()
    return True


async def run_gift_watcher(bot: Bot):
    while True:
        try:
            if await _watch(bot) is False:
                return
        except Exception:
            log.exception("Слушатель упал, перезапуск через 10 сек")
        await asyncio.sleep(10)
