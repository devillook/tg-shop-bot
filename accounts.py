# accounts.py — работа с продаваемыми аккаунтами через Telethon
import getpass
import os
import re
import time

from telethon import TelegramClient, errors, functions

import config

CODE_RE = re.compile(r"\b(\d{5})\b")


def make_client(session_name: str) -> TelegramClient:
    os.makedirs(config.SESSIONS_DIR, exist_ok=True)
    return TelegramClient(os.path.join(config.SESSIONS_DIR, session_name), config.API_ID, config.API_HASH)


async def check_spam(client: TelegramClient) -> str:
    """Спрашивает @SpamBot. Возвращает «нет», «есть» или «не проверен»."""
    try:
        async with client.conversation("SpamBot", timeout=30) as conv:
            await conv.send_message("/start")
            resp = await conv.get_response()
        text = (resp.raw_text or "").lower()
        if any(k in text for k in ("no limits", "free as a bird", "свободен от", "нет ограничений")):
            return "нет"
        return "есть"
    except Exception:
        return "не проверен"


async def fetch_login_code(session_name: str):
    """Ищет свежий код входа в служебном чате Telegram (777000). Возвращает (код, None) или (None, причина)."""
    client = make_client(session_name)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return None, "not_authorized"
        async for msg in client.iter_messages(777000, limit=5):
            if time.time() - msg.date.timestamp() > config.CODE_MAX_AGE_SECONDS:
                break
            m = CODE_RE.search(msg.raw_text or "")
            if m:
                return m.group(1), None
        return None, "no_code"
    finally:
        await client.disconnect()


async def logout_session(session_name: str) -> bool:
    """Закрывает НАШУ сессию, чтобы у продавца не осталось доступа к проданному аккаунту."""
    client = make_client(session_name)
    await client.connect()
    try:
        if await client.is_user_authorized():
            await client.log_out()
        return True
    except Exception:
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        path = os.path.join(config.SESSIONS_DIR, session_name + ".session")
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ---------- интерактивный вход в аккаунт ----------
def session_exists(name: str) -> bool:
    return os.path.exists(os.path.join(config.SESSIONS_DIR, name + ".session"))


def next_free_session_name(prefix: str = "acc") -> str:
    n = 1
    while session_exists(f"{prefix}{n}"):
        n += 1
    return f"{prefix}{n}"


def _describe(sent) -> str:
    kind = type(getattr(sent, "type", None)).__name__
    return {
        "SentCodeTypeApp": "в ПРИЛОЖЕНИЕ Telegram — откройте чат «Telegram» там, где этот аккаунт уже залогинен",
        "SentCodeTypeSms": "по SMS на этот номер",
        "SentCodeTypeCall": "голосовым звонком на этот номер",
        "SentCodeTypeFlashCall": "звонком-сбросом (код — последние цифры номера звонящего)",
        "SentCodeTypeMissedCall": "пропущенным звонком (код — последние цифры номера звонящего)",
        "SentCodeTypeEmailCode": "на e-mail, привязанный к аккаунту",
        "SentCodeTypeFirebaseSms": "через Firebase-SMS (сторонним программам такой код НЕ приходит)",
        "SentCodeTypeSmsWord": "SMS со словом (сторонним программам может не прийти)",
        "SentCodeTypeSmsPhrase": "SMS с фразой (сторонним программам может не прийти)",
    }.get(kind, kind or "неизвестным способом")


_UNUSABLE = ("SentCodeTypeFirebaseSms", "SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase")
_NEXT = {
    "CodeTypeSms": "SMS", "CodeTypeCall": "голосовой звонок", "CodeTypeFlashCall": "звонок-сброс",
    "CodeTypeMissedCall": "пропущенный звонок", "CodeTypeFragmentSms": "сообщение через Fragment",
}


def _next_hint(sent) -> str:
    nxt = getattr(sent, "next_type", None)
    if nxt is None:
        return "других способов Telegram не предлагает"
    return _NEXT.get(type(nxt).__name__, type(nxt).__name__)


def describe_code_delivery(sent) -> str:
    """Публичная обёртка над _describe — чем пришёл код (для бот-диалога, не только консоли)."""
    return _describe(sent)


def next_code_hint(sent) -> str:
    """Публичная обёртка над _next_hint."""
    return _next_hint(sent)


async def login_interactive(client: TelegramClient) -> None:
    """Вход по номеру с понятными сообщениями об ошибках и повторной отправкой кода (r)."""
    await client.connect()
    if await client.is_user_authorized():
        return

    while True:
        phone = input("Номер телефона (например +380XXXXXXXXX): ").strip().replace(" ", "")
        try:
            sent = await client.send_code_request(phone)
            break
        except errors.PhoneNumberInvalidError:
            print("⚠️ Неверный номер. Формат: +380XXXXXXXXX")
        except errors.PhoneNumberBannedError:
            raise SystemExit("⛔ Этот номер заблокирован Telegram — войти нельзя.")
        except errors.PhoneNumberFloodError:
            raise SystemExit("⛔ Слишком много запросов кода на этот номер. Подождите несколько часов и повторите.")
        except errors.FloodWaitError as e:
            raise SystemExit(f"⛔ Telegram просит подождать {e.seconds} сек. (~{e.seconds // 60} мин.) и повторить.")
        except errors.ApiIdInvalidError:
            raise SystemExit("⛔ API_ID / API_HASH неверные. Проверьте файл .env.")

    print(f"📨 Код отправлен: {_describe(sent)}")
    if type(getattr(sent, "type", None)).__name__ in _UNUSABLE:
        print("⚠️ Этот способ нам не подходит — автоматически запрашиваю другой...")
        try:
            sent = await client(functions.auth.ResendCodeRequest(
                phone_number=phone, phone_code_hash=sent.phone_code_hash))
            print(f"📨 Код отправлен: {_describe(sent)}")
        except Exception as e:
            print("⚠️ Не удалось запросить другой способ:", e)
    print(f"ℹ️ Если код не пришёл — введите r, следующий способ: {_next_hint(sent)}")
    while True:
        code = input("Введите код (r — отправить другим способом, q — выйти): ").strip()
        if code.lower() == "q":
            raise SystemExit("Отменено.")
        if code.lower() == "r":
            try:
                sent = await client(functions.auth.ResendCodeRequest(
                    phone_number=phone, phone_code_hash=sent.phone_code_hash))
                print(f"📨 Код отправлен: {_describe(sent)}")
                print(f"ℹ️ Следующий способ, если снова не придёт: {_next_hint(sent)}")
            except Exception as e:
                print("⚠️ Не удалось отправить повторно:", e)
            continue
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            return
        except errors.SessionPasswordNeededError:
            while True:
                pw = getpass.getpass("Облачный пароль (ввод скрыт): ")
                try:
                    await client.sign_in(password=pw)
                    return
                except errors.PasswordHashInvalidError:
                    print("⚠️ Пароль неверный, попробуйте ещё раз.")
        except errors.PhoneCodeInvalidError:
            print("⚠️ Неверный код, попробуйте ещё раз.")
        except errors.PhoneCodeExpiredError:
            print("⚠️ Код устарел. Введите r, чтобы запросить новый.")
