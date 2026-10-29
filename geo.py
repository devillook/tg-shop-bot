# geo.py — страна по номеру, фильтры каталога, возраст аккаунта
import datetime
import re

# префикс (цифры после «+») -> (флаг, название)
COUNTRIES = {
    "1": ("🇺🇸", "США / Канада"), "7": ("🇷🇺", "Россия"), "20": ("🇪🇬", "Египет"), "33": ("🇫🇷", "Франция"),
    "34": ("🇪🇸", "Испания"), "39": ("🇮🇹", "Италия"), "44": ("🇬🇧", "Великобритания"), "48": ("🇵🇱", "Польша"),
    "49": ("🇩🇪", "Германия"), "55": ("🇧🇷", "Бразилия"), "62": ("🇮🇩", "Индонезия"), "63": ("🇵🇭", "Филиппины"),
    "66": ("🇹🇭", "Таиланд"), "81": ("🇯🇵", "Япония"), "82": ("🇰🇷", "Южная Корея"), "84": ("🇻🇳", "Вьетнам"),
    "86": ("🇨🇳", "Китай"), "90": ("🇹🇷", "Турция"), "91": ("🇮🇳", "Индия"), "234": ("🇳🇬", "Нигерия"),
    "372": ("🇪🇪", "Эстония"), "374": ("🇦🇲", "Армения"), "375": ("🇧🇾", "Беларусь"), "380": ("🇺🇦", "Украина"),
    "994": ("🇦🇿", "Азербайджан"), "995": ("🇬🇪", "Грузия"), "998": ("🇺🇿", "Узбекистан"),
}
KAZAKHSTAN = ("🇰🇿", "Казахстан")
UNKNOWN = ("🌍", "Другая страна")

# ключ фильтра -> префикс страны
KEY_BY_PREFIX = {"1": "us", "49": "de", "375": "by"}

# кнопки фильтров каталога (порядок = порядок на экране)
FILTERS = [
    ("ru", "🇷🇺  РОССИЙСКИЕ НОМЕРА"),
    ("de", "🇩🇪  Германия (+49)"),
    ("by", "🇧🇾  Беларусь (+375)"),
    ("us", "🇺🇸  Америка (+1)"),
    ("other", "🌐  Другие страны"),
    ("prem", "💎  Премиум"),
    ("long", "⏳  Долгосроки"),
]
# Цвета кнопок (Telegram умеет только: primary — синяя, success — зелёная, danger — красная, None — серая)
FILTER_STYLES = {
    "all": "success", "ru": "danger", "de": "primary", "by": "success", "us": "primary",
    "other": None, "prem": "success", "long": "primary",
}
TITLES = {
    "all": "🌍 Все аккаунты", "ru": "🇷🇺 РОССИЙСКИЕ НОМЕРА", "de": "🇩🇪 Германия (+49)",
    "by": "🇧🇾 Беларусь (+375)", "us": "🇺🇸 Америка (+1)", "other": "🌐 Другие страны",
    "prem": "💎 Премиум-аккаунты", "long": "⏳ Долгосроки (старые — выше)",
}


def title_of(key: str) -> str:
    return TITLES.get(key, "Каталог")


def digits_of(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _prefix(phone: str) -> str | None:
    d = digits_of(phone)
    for n in (3, 2, 1):
        if d[:n] in COUNTRIES:
            return d[:n]
    return None


def country_of(phone: str) -> tuple[str, str]:
    d = digits_of(phone)
    if d.startswith("7") and len(d) > 1 and d[1] in "67":
        return KAZAKHSTAN
    p = _prefix(phone)
    return COUNTRIES[p] if p else UNKNOWN


def country_key(phone: str) -> str:
    """Ключ фильтра: ru / us / es / in / jp / cn / other."""
    d = digits_of(phone)
    if d.startswith("7"):
        return "other" if (len(d) > 1 and d[1] in "67") else "ru"   # +76/+77 — Казахстан
    return KEY_BY_PREFIX.get(_prefix(phone) or "", "other")


# ---------- возраст аккаунта ----------
# Telegram не отдаёт дату регистрации. Оценка по ID — ПРИБЛИЗИТЕЛЬНАЯ, при добавлении аккаунта
# можно ввести год вручную.
_ID_YEARS = [
    (100_000_000, 2014), (250_000_000, 2015), (450_000_000, 2016), (650_000_000, 2017),
    (850_000_000, 2018), (1_100_000_000, 2019), (1_500_000_000, 2020), (5_000_000_000, 2021),
    (5_900_000_000, 2022), (6_900_000_000, 2023), (7_700_000_000, 2024),
]


def estimate_reg_year(tg_id: int) -> int:
    for limit, year in _ID_YEARS:
        if tg_id <= limit:
            return year
    return 2025


def age_years(reg_year) -> int | None:
    if not reg_year:
        return None
    return max(0, datetime.date.today().year - int(reg_year))


def _years_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "лет"
    return {1: "год", 2: "года", 3: "года", 4: "года"}.get(n % 10, "лет")


def age_short(reg_year) -> str:
    y = age_years(reg_year)
    return "" if y is None else (f"{y} г." if y >= 1 else "новый")


def age_text(reg_year) -> str:
    y = age_years(reg_year)
    if y is None:
        return "не указан"
    return f"{y} {_years_word(y)} (с {reg_year})" if y >= 1 else f"меньше года (с {reg_year})"
def apply_filter(rows, key: str):
    """Фильтрует список товаров по ключу (ru, us, es, in, jp, cn, other, prem, long)"""
    if key == "all":
        return rows
    if key == "prem":
        return [r for r in rows if r["premium"] == 1]
    if key == "long":
        return sorted(rows, key=lambda x: x["reg_year"] or 2025)
    
    filtered = []
    for r in rows:
        phone = r["phone"] or ""
        if country_key(phone) == key:
            filtered.append(r)
    return filtered