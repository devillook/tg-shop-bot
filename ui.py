# ui.py — кнопки, анимации, эффекты сообщений
import asyncio
import logging

from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("ui")

LINE = "━━━━━━━━━━━━━━━━━━"

# Эффекты сообщений Telegram (работают в личных чатах; если не сработают — отправим без эффекта)
EFFECT_FIRE = "5104841245755180586"      # 🔥
EFFECT_LIKE = "5107584321108051014"      # 👍
EFFECT_HEART = "5044134455711629726"     # ❤️
EFFECT_CONFETTI = "5046509860389126442"  # 🎉


def btn(text: str, data: str | None = None, url: str | None = None, style: str | None = None):
    """style: 'primary' (синяя), 'success' (зелёная), 'danger' (красная). В старых клиентах игнорируется."""
    kwargs = {"text": text}
    if url:
        kwargs["url"] = url
    else:
        kwargs["callback_data"] = data
    if style:
        try:
            return InlineKeyboardButton(**kwargs, style=style)
        except Exception:
            pass  # старая версия aiogram — кнопка без цвета
    return InlineKeyboardButton(**kwargs)


def keyboard(*rows) -> InlineKeyboardMarkup:
    """Каждый аргумент — одна кнопка (отдельная строка) или список кнопок (одна строка)."""
    return InlineKeyboardMarkup(inline_keyboard=[list(r) if isinstance(r, (list, tuple)) else [r] for r in rows])


def bar(filled: int, total: int) -> str:
    return "▰" * filled + "▱" * (total - filled)


def loading(title: str, steps: int = 4) -> list[str]:
    return [f"⏳ <b>{title}</b>\n\n{bar(i, steps)}" for i in range(1, steps + 1)]


async def safe_edit(msg: types.Message, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await msg.edit_text(text, reply_markup=markup)
        return True
    except TelegramBadRequest:
        return False


async def animate(msg: types.Message, frames: list[str], delay: float = 0.3):
    for frame in frames:
        await safe_edit(msg, frame)
        await asyncio.sleep(delay)


async def send(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None,
               effect: str | None = None):
    """Отправка с эффектом сообщения; при любой ошибке эффекта — отправка без него."""
    if effect:
        try:
            return await bot.send_message(chat_id, text, reply_markup=markup, message_effect_id=effect)
        except Exception:
            log.debug("эффект не применился, отправляю без него")
    return await bot.send_message(chat_id, text, reply_markup=markup)
