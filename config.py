# config.py — настройки из файла .env (лежит рядом с этим файлом)
import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")


def _ids(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x]


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = _ids("ADMIN_IDS")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@")

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
OWNER_SESSION = os.getenv("OWNER_SESSION", "owner").strip()

BETA_MODE = os.getenv("BETA_MODE", "0").strip() == "1"
BETA_USER_IDS = _ids("BETA_USER_IDS")

# Канал, подписка на который обязательна для пользования ботом. Без @, например "codcrytg".
# Бот ДОЛЖЕН быть добавлен в этот канал администратором — иначе проверка подписки не сможет работать.
SUB_CHANNEL = os.getenv("SUB_CHANNEL", "").strip().lstrip("@")

REFERRAL_BONUS_TOKENS = int(os.getenv("REFERRAL_BONUS_TOKENS", "5"))  # токенов за приглашённого друга

# DATA_DIR — папка с данными, которые обязаны пережить перезапуск (shop.db, sessions/).
# Локально это просто папка проекта. На хостинге с постоянным диском (Fly.io volume и т.п.)
# задай переменную окружения DATA_DIR=/путь/к/смонтированному/диску.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR = str(DATA_DIR / "sessions")
DB_PATH = str(DATA_DIR / "shop.db")
PAYMENT_MINUTES = int(os.getenv("PAYMENT_MINUTES", "5"))            # время на оплату (аккаунт в резерве)
ACCESS_HOURS = float(os.getenv("ACCESS_HOURS", "5"))                # время на вход в аккаунт после оплаты
GIFT_TOLERANCE_PERCENT = float(os.getenv("GIFT_TOLERANCE_PERCENT", "15"))  # допуск на комиссию подарка
# Статистика на главном экране = стартовое число + реальное количество
STATS_BASE_SOLD = int(os.getenv("STATS_BASE_SOLD", "15"))    # «куплено аккаунтов»
STATS_BASE_USERS = int(os.getenv("STATS_BASE_USERS", "24"))  # «пользовались ботом»
CODE_MAX_AGE_SECONDS = 600    # коды входа старше 10 минут не отправляем

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в файле .env")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS не задан в файле .env")
if not API_ID:
    raise RuntimeError("API_ID не задан в файле .env")
if not re.fullmatch(r"[0-9a-fA-F]{32}", API_HASH):
    raise RuntimeError("API_HASH должен состоять ровно из 32 символов: цифры и латинские буквы a-f. "
                       "Скопируйте его заново с my.telegram.org (мог попасть лишний символ или кириллическая буква).")
