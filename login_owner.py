# Вход в ТВОЙ аккаунт (@jonnkq), на который приходят подарки/сообщения. Запусти один раз: python login_owner.py
import asyncio

import accounts
import config


async def main():
    client = accounts.make_client(config.OWNER_SESSION)
    await accounts.login_interactive(client)
    me = await client.get_me()
    print(f"✅ Готово. Вошли как {me.first_name} (@{me.username}), ID {me.id}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
