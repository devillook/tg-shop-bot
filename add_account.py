# Добавить аккаунт в магазин. Запуск: python add_account.py
import asyncio

import accounts
import database
import geo


async def main():
    database.init_db()
    default = accounts.next_free_session_name()
    name = input(f"Имя сессии для НОВОГО аккаунта (Enter = {default}): ").strip() or default
    if accounts.session_exists(name):
        print(f"ℹ️ Сессия «{name}» уже существует — используем сохранённый вход (код не понадобится).")

    client = accounts.make_client(name)
    await accounts.login_interactive(client)

    me = await client.get_me()
    phone = ("+" + me.phone) if me.phone else "неизвестно"
    flag, country = geo.country_of(phone)
    premium = 1 if getattr(me, "premium", False) else 0
    print(f"✅ Вошли: {phone} {flag} {country}, ID {me.id}")
    print("Telegram Premium:", "есть" if premium else "нет")

    print("Проверяю спам-блок через @SpamBot...")
    spam = await accounts.check_spam(client)
    print("Спам-блок:", spam)
    await client.disconnect()

    est = geo.estimate_reg_year(me.id)
    y = input(f"Год регистрации аккаунта (Enter — оценка по ID, примерно {est}): ").strip()
    reg_year = int(y) if y.isdigit() else est

    cloud = input("Облачный пароль аккаунта (Enter, если нет): ").strip()
    price = int(input("Цена в звёздах (сумма подарков): ").strip())
    pid = database.add_product(phone, me.id, spam, price, name, cloud, premium, reg_year)
    print(f"🎉 Готово! Аккаунт добавлен в каталог, товар #{pid}")


if __name__ == "__main__":
    asyncio.run(main())
