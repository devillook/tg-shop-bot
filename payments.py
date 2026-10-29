# payments.py — оплата получена -> выдать аккаунт покупателю
import asyncio
import html
import logging

from aiogram import Bot

import config
import database
import ui
from ui import btn, keyboard

log = logging.getLogger("payments")


async def notify_admins(bot: Bot, text: str, markup=None):
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception:
            log.warning("Не удалось написать админу %s", admin_id)


def delivery_kb(order_id: int):
    return keyboard(
        btn("📩  Получить код входа", f"get_code_{order_id}", style="success"),
        btn("✅  Я вошёл — завершить", f"finish_{order_id}", style="primary"),
    )


async def complete_order(bot: Bot, order_id: int, via: str) -> bool:
    """Оплата подтверждена: закрепляем аккаунт и выдаём данные.
    Между проверкой статуса и записью «paid» нет await — двойной выдачи не будет."""
    order = database.get_order(order_id)
    if not order or order["status"] != "new":
        return False

    product = database.get_product(order["product_id"])
    if not database.lock_for_sale(order["product_id"], order["user_id"]):
        database.set_order_status(order_id, "failed")
        refund_note = ""
        if order["method"] == "tokens":
            # Токены — наши же, их мы можем и должны вернуть автоматически (в отличие от подарков,
            # которые покупатель уже отправил наружу и которые возвращает только админ вручную).
            new_balance = database.add_balance(order["user_id"], order["price_stars"])
            refund_note = f" Токены возвращены на баланс (сейчас: {new_balance})."
        try:
            await bot.send_message(
                order["user_id"],
                "⚠️ Оплата получена, но этот аккаунт уже достался другому покупателю."
                + refund_note +
                f" Если вопрос не решится сам — напишите в поддержку @{config.ADMIN_USERNAME}.")
        except Exception:
            pass
        await notify_admins(bot, f"⚠️ Заказ #{order_id}: оплата ({via}) есть, но аккаунт уже продан. "
                                 f"Покупатель: <code>{order['user_id']}</code>.{refund_note or ' Нужна замена/возврат вручную.'}")
        return False

    database.set_order_status(order_id, "paid")
    password = product["cloud_password"] or "нет"
    text = (
        "🎉 <b>ОПЛАТА ПОДТВЕРЖДЕНА!</b>\n"
        f"{ui.LINE}\n"
        f"🧾 Заказ <b>#{order_id}</b>\n\n"
        f"📞 Номер: <code>{html.escape(product['phone'])}</code>\n"
        f"🔐 Облачный пароль: <code>{html.escape(password)}</code>\n"
        f"{ui.LINE}\n\n"
        "<b>Как войти:</b>\n"
        "1️⃣ В Telegram нажмите «Добавить аккаунт» и введите номер.\n"
        "2️⃣ Когда Telegram запросит код — нажмите «Получить код входа» здесь.\n"
        "3️⃣ Введите облачный пароль, если он запросится.\n"
        "4️⃣ После успешного входа нажмите «Я вошёл — завершить».\n\n"
        f"⏳ На вход в аккаунт у вас <b>{config.ACCESS_HOURS:g} ч.</b> После этого доступ закроется."
    )
    try:
        await bot.send_chat_action(order["user_id"], "typing")
        await asyncio.sleep(0.8)
        await ui.send(bot, order["user_id"], text, delivery_kb(order_id), effect=ui.EFFECT_CONFETTI)
        log.info("[ВЫДАЧА] заказ #%s: аккаунт %s отправлен покупателю %s (%s)",
                 order_id, product["phone"], order["user_id"], via)
    except Exception:
        log.exception("Не удалось отправить данные покупателю")
        await notify_admins(bot, f"❗ Заказ #{order_id} оплачен ({via}), но бот не смог написать покупателю "
                                 f"<code>{order['user_id']}</code>. Свяжитесь с ним вручную.")
    await notify_admins(bot, f"💰 Оплата получена ({via}) — заказ #{order_id}, аккаунт {html.escape(product['phone'])}, "
                             f"покупатель <code>{order['user_id']}</code>.")
    return True
