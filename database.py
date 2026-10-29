# database.py — SQLite: товары, заказы, пользователи, отзывы, рулетка
import sqlite3
import time
from contextlib import contextmanager

import config


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> int:
    return int(time.time())


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                account_id TEXT,
                spam_block TEXT,
                price_stars INTEGER,
                session_name TEXT,
                cloud_password TEXT,
                status TEXT DEFAULT 'available',   -- available / reserved / sold / removed
                reserved_by INTEGER,
                reserved_until INTEGER,
                premium INTEGER DEFAULT 0,          -- есть ли Telegram Premium на аккаунте
                reg_year INTEGER,                   -- год регистрации аккаунта
                synced INTEGER DEFAULT 1            -- 1 = реальный аккаунт (есть сессия), 0 = добавлен вручную
            )""")
        # миграция для старой базы
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
        if "premium" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN premium INTEGER DEFAULT 0")
        if "reg_year" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN reg_year INTEGER")
        if "synced" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN synced INTEGER DEFAULT 1")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at INTEGER,
                balance INTEGER DEFAULT 0           -- баланс в токенах
            )""")
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "balance" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER,
                user_id INTEGER,
                status TEXT DEFAULT 'new',         -- new / paid / done / cancelled / expired / failed / rejected
                price_stars INTEGER,
                paid_stars INTEGER DEFAULT 0,
                method TEXT DEFAULT 'gift',        -- gift / tokens
                created_at INTEGER,
                paid_at INTEGER
            )""")
        ocols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        if "method" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN method TEXT DEFAULT 'gift'")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gift_events (
                sender_id INTEGER,
                msg_id INTEGER,
                stars INTEGER,
                created_at INTEGER,
                PRIMARY KEY (sender_id, msg_id)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                user_id INTEGER,
                text TEXT,
                created_at INTEGER
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS roulette (
                user_id INTEGER PRIMARY KEY,
                last_spin INTEGER DEFAULT 0
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                amount INTEGER,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                created_at INTEGER
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT,
                user_id INTEGER,
                created_at INTEGER,
                PRIMARY KEY (code, user_id)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_id INTEGER PRIMARY KEY,   -- кого привели (может быть рефералом только 1 раз)
                referrer_id INTEGER,               -- кто привёл
                created_at INTEGER
            )""")


# ---------- пользователи ----------
def add_user(tg_id, username):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (tg_id, username, created_at) VALUES (?, ?, ?)",
                     (tg_id, username, now()))


# ---------- товары ----------
def add_product(phone, account_id, spam_block, price_stars, session_name, cloud_password,
                premium=0, reg_year=None, synced=1) -> int:
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO products (phone, account_id, spam_block, price_stars, session_name, cloud_password,
                                  premium, reg_year, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (phone, str(account_id), spam_block, price_stars, session_name, cloud_password or "",
              1 if premium else 0, reg_year, 1 if synced else 0))
        return cur.lastrowid


def add_manual_product(phone, price_stars, premium=0, reg_year=None) -> int:
    """Карточка в каталоге без реального входа (добавлена админом вручную в боте).
    Покупка такого аккаунта оформляется администратором лично."""
    return add_product(phone, "—", "не проверен (добавлено вручную)", price_stars,
                       "", "", premium, reg_year, synced=0)


def get_product(product_id):
    with db() as conn:
        return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def get_product_by_session(session_name: str):
    with db() as conn:
        return conn.execute("SELECT * FROM products WHERE session_name = ? AND session_name != ''",
                            (session_name,)).fetchone()


def is_buyable(row) -> bool:
    if row is None:
        return False
    if row["status"] == "available":
        return True
    return row["status"] == "reserved" and (row["reserved_until"] or 0) < now()


def get_available_products():
    with db() as conn:
        return conn.execute("""
            SELECT * FROM products
            WHERE status = 'available' OR (status = 'reserved' AND reserved_until < ?)
            ORDER BY id
        """, (now(),)).fetchall()


def list_products():
    with db() as conn:
        return conn.execute("SELECT * FROM products ORDER BY id").fetchall()


def reserve_product(product_id, user_id) -> bool:
    with db() as conn:
        cur = conn.execute("""
            UPDATE products SET status = 'reserved', reserved_by = ?, reserved_until = ?
            WHERE id = ? AND (status = 'available'
                  OR (status = 'reserved' AND (reserved_by = ? OR reserved_until < ?)))
        """, (user_id, now() + config.PAYMENT_MINUTES * 60, product_id, user_id, now()))
        return cur.rowcount == 1


def release_product(product_id, user_id):
    with db() as conn:
        conn.execute("""
            UPDATE products SET status = 'available', reserved_by = NULL, reserved_until = NULL
            WHERE id = ? AND status = 'reserved' AND reserved_by = ?
        """, (product_id, user_id))


def lock_for_sale(product_id, user_id) -> bool:
    """Окончательно закрепляет аккаунт за покупателем (после оплаты)."""
    with db() as conn:
        cur = conn.execute("""
            UPDATE products SET status = 'sold', reserved_by = ?, reserved_until = NULL
            WHERE id = ? AND (status = 'available'
                  OR (status = 'reserved' AND (reserved_by = ? OR reserved_until < ?)))
        """, (user_id, product_id, user_id, now()))
        return cur.rowcount == 1


def set_price(product_id, stars) -> bool:
    with db() as conn:
        return conn.execute("UPDATE products SET price_stars = ? WHERE id = ? AND status != 'sold'",
                            (stars, product_id)).rowcount == 1


def remove_product(product_id) -> bool:
    with db() as conn:
        return conn.execute(
            "UPDATE products SET status = 'removed' WHERE id = ? AND status IN ('available', 'reserved')",
            (product_id,)).rowcount == 1


# ---------- заказы ----------
def create_order(product_id, user_id, price_stars, method="gift") -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO orders (product_id, user_id, status, price_stars, method, created_at) "
            "VALUES (?, ?, 'new', ?, ?, ?)",
            (product_id, user_id, price_stars, method, now()))
        return cur.lastrowid


def get_order(order_id):
    with db() as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def set_order_status(order_id, status):
    with db() as conn:
        if status == "paid":
            conn.execute("UPDATE orders SET status = ?, paid_at = ? WHERE id = ?", (status, now(), order_id))
        else:
            conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


def cancel_user_pending(user_id):
    """Отменяет неоплаченные заказы пользователя и снимает резерв."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE user_id = ? AND status = 'new'", (user_id,)).fetchall()
        for r in rows:
            conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (r["id"],))
            conn.execute("""UPDATE products SET status = 'available', reserved_by = NULL, reserved_until = NULL
                            WHERE id = ? AND status = 'reserved' AND reserved_by = ?""",
                         (r["product_id"], user_id))
        return rows


def get_pending_order(user_id):
    """Последний неоплаченный заказ пользователя — к нему привязываем входящую оплату."""
    with db() as conn:
        return conn.execute("SELECT * FROM orders WHERE user_id = ? AND status = 'new' ORDER BY id DESC LIMIT 1",
                            (user_id,)).fetchone()


def add_paid_stars(order_id, stars) -> int:
    with db() as conn:
        conn.execute("UPDATE orders SET paid_stars = COALESCE(paid_stars, 0) + ? WHERE id = ?", (stars, order_id))
        return conn.execute("SELECT paid_stars FROM orders WHERE id = ?", (order_id,)).fetchone()["paid_stars"]


def expire_orders():
    """Закрывает заказы, у которых вышло время ожидания оплаты."""
    border = now() - config.PAYMENT_MINUTES * 60
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE status = 'new' AND created_at < ?", (border,)).fetchall()
        for r in rows:
            conn.execute("UPDATE orders SET status = 'expired' WHERE id = ?", (r["id"],))
            conn.execute("""UPDATE products SET status = 'available', reserved_by = NULL, reserved_until = NULL
                            WHERE id = ? AND status = 'reserved' AND reserved_by = ?""",
                         (r["product_id"], r["user_id"]))
        return rows


def get_expired_access_orders():
    """Оплаченные заказы, у которых вышло время на вход в аккаунт."""
    border = now() - int(config.ACCESS_HOURS * 3600)
    with db() as conn:
        return conn.execute("SELECT * FROM orders WHERE status = 'paid' AND paid_at < ?", (border,)).fetchall()


def get_active_paid_orders():
    """Оплаченные заказы с реальной (синхронизированной) сессией — для автослежения за кодом входа."""
    with db() as conn:
        return conn.execute("""
            SELECT o.id AS order_id, o.user_id, o.product_id, o.paid_at, p.session_name, p.phone
            FROM orders o JOIN products p ON p.id = o.product_id
            WHERE o.status = 'paid' AND p.synced = 1 AND p.session_name != ''
        """).fetchall()


def get_paid_order_for_product(product_id):
    """Текущий (единственный) активный оплаченный заказ на этот аккаунт — для проверки получателя кода."""
    with db() as conn:
        return conn.execute("SELECT * FROM orders WHERE product_id = ? AND status = 'paid' LIMIT 1",
                            (product_id,)).fetchone()


def get_user_orders(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT o.id, o.status, o.paid_at, p.phone FROM orders o JOIN products p ON p.id = o.product_id
            WHERE o.user_id = ? AND o.status IN ('paid', 'done') ORDER BY o.id DESC
        """, (user_id,)).fetchall()


# ---------- баланс (токены) и профиль ----------
def get_user_row(tg_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()


def get_balance(tg_id) -> int:
    row = get_user_row(tg_id)
    return row["balance"] if row else 0


def add_balance(tg_id, amount: int) -> int:
    """Начисляет (amount может быть отрицательным) и возвращает новый баланс."""
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (tg_id, username, created_at, balance) VALUES (?, '', ?, 0)",
                     (tg_id, now()))
        conn.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, tg_id))
        return conn.execute("SELECT balance FROM users WHERE tg_id = ?", (tg_id,)).fetchone()["balance"]


def spend_balance(tg_id, amount: int) -> bool:
    """Атомарно списывает токены. False, если баланс недостаточен — ничего не меняется."""
    with db() as conn:
        cur = conn.execute("UPDATE users SET balance = balance - ? WHERE tg_id = ? AND balance >= ?",
                           (amount, tg_id, amount))
        return cur.rowcount == 1


def get_user_stats(tg_id):
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(price_stars), 0) AS spent FROM orders "
            "WHERE user_id = ? AND status IN ('paid', 'done')", (tg_id,)).fetchone()
        return {"orders": row["cnt"], "spent_stars": row["spent"]}


# ---------- промокоды ----------
def create_promo(code: str, amount: int, max_uses: int = 1):
    with db() as conn:
        conn.execute("""
            INSERT INTO promo_codes (code, amount, max_uses, used_count, created_at) VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(code) DO UPDATE SET amount = excluded.amount, max_uses = excluded.max_uses
        """, (code, amount, max_uses, now()))


def redeem_promo(code: str, user_id: int):
    """Возвращает (True, сумма) при успехе либо (False, причина)."""
    with db() as conn:
        promo = conn.execute("SELECT * FROM promo_codes WHERE code = ?", (code,)).fetchone()
        if not promo:
            return False, "not_found"
        if promo["used_count"] >= promo["max_uses"]:
            return False, "exhausted"
        already = conn.execute("SELECT 1 FROM promo_redemptions WHERE code = ? AND user_id = ?",
                               (code, user_id)).fetchone()
        if already:
            return False, "already_used"
        conn.execute("INSERT INTO promo_redemptions (code, user_id, created_at) VALUES (?, ?, ?)",
                     (code, user_id, now()))
        conn.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code = ?", (code,))
        conn.execute("INSERT OR IGNORE INTO users (tg_id, username, created_at, balance) VALUES (?, '', ?, 0)",
                     (user_id, now()))
        conn.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (promo["amount"], user_id))
        new_balance = conn.execute("SELECT balance FROM users WHERE tg_id = ?", (user_id,)).fetchone()["balance"]
        return True, {"amount": promo["amount"], "balance": new_balance}


# ---------- статистика ----------
def count_sold() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE status IN ('paid', 'done')").fetchone()[0]


def count_users() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ---------- рефералы ----------
def record_referral(referred_id: int, referrer_id: int) -> bool:
    """True — реферал засчитан первый раз. False — уже был засчитан раньше (ничего не меняется)."""
    with db() as conn:
        try:
            conn.execute("INSERT INTO referrals (referred_id, referrer_id, created_at) VALUES (?, ?, ?)",
                         (referred_id, referrer_id, now()))
            return True
        except sqlite3.IntegrityError:
            return False


def get_referral_count(referrer_id: int) -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)).fetchone()[0]


# ---------- подарки ----------
def record_gift(sender_id, msg_id, stars) -> bool:
    """True — подарок новый; False — уже обработан."""
    with db() as conn:
        try:
            conn.execute("INSERT INTO gift_events (sender_id, msg_id, stars, created_at) VALUES (?, ?, ?, ?)",
                         (sender_id, msg_id, stars, now()))
            return True
        except sqlite3.IntegrityError:
            return False


# ---------- отзывы ----------
def add_review(order_id, user_id, text):
    with db() as conn:
        conn.execute("INSERT INTO reviews (order_id, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                     (order_id, user_id, text, now()))


# ---------- рулетка ----------
def get_last_roulette_time(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT last_spin FROM roulette WHERE user_id = ?", (user_id,)).fetchone()
        return row["last_spin"] if row else 0


def set_last_roulette_time(user_id: int):
    with db() as conn:
        conn.execute("""
            INSERT INTO roulette (user_id, last_spin) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_spin = ?
        """, (user_id, now(), now()))