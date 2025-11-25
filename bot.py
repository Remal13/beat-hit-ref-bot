import asyncio
import logging
import sqlite3
import datetime
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

# ============= НАСТРОЙКИ =============

API_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # токен задаём через переменную окружения
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@riversvskeys")  # @username канала по умолчанию

MAX_GIFTS = 20        # всего подарков (ваучеров на песню)
REQUIRED_INVITES = 4  # сколько друзей нужно привести

# сюда твой Telegram user_id (можно несколько, через
# @userinfobot или /id в некоторых ботах)
ADMIN_IDS = [5210074523]

# ============= ЛОГИРОВАНИЕ =============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ============= БАЗА ДАННЫХ =============

DB_PATH = "referrals.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Таблица пользователей
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            invited_by INTEGER,
            joined_at TEXT,
            status TEXT DEFAULT 'pending'
        )
        """
    )

    # Таблица инвайтов (кого кто пригласил)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            created_at TEXT
        )
        """
    )

    # Таблица победителей
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS winners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            selected_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def get_connection():
    return sqlite3.connect(DB_PATH)


# ============= БОТ =============

bot = Bot(token=API_TOKEN)
dp = Dispatcher()


# -------- УТИЛИТЫ --------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def user_in_channel(user_id: int) -> bool:
    """
    Проверяем, подписан ли пользователь на канал.
    """
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Не удалось проверить подписку для {user_id}: {e}")
        # Если не смогли проверить, считаем, что не подписан
        return False


def add_user_if_not_exists(user_id: int, invited_by: int | None = None):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            """
            INSERT INTO users (user_id, invited_by, joined_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                invited_by,
                datetime.datetime.utcnow().isoformat(),
                "pending",
            ),
        )
        conn.commit()

    conn.close()


def add_referral(referrer_id: int, referred_id: int):
    """
    Добавляет запись о том, что referrer_id пригласил referred_id.
    """
    conn = get_connection()
    cur = conn.cursor()

    # Проверяем, нет ли уже такой записи
    cur.execute(
        """
        SELECT id FROM referrals
        WHERE referrer_id = ? AND referred_id = ?
        """,
        (referrer_id, referred_id),
    )
    if cur.fetchone() is None:
        cur.execute(
            """
            INSERT INTO referrals (referrer_id, referred_id, created_at)
            VALUES (?, ?, ?)
            """,
            (
                referrer_id,
                referred_id,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()

    conn.close()


def count_valid_referrals(referrer_id: int) -> int:
    """
    Считает число рефералов, которых привёл referrer_id.
    В упрощённом виде считаем всех в таблице referrals.
    При желании можно добавить проверку, чтобы учитывались
    только те, кто подписан на канал или не заблокировал бота.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*) FROM referrals
        WHERE referrer_id = ?
        """,
        (referrer_id,),
    )
    (count,) = cur.fetchone()
    conn.close()
    return count


def get_user_status(user_id: int) -> str | None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0]
    return None


def set_user_status(user_id: int, status: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET status = ? WHERE user_id = ?",
        (status, user_id),
    )
    conn.commit()
    conn.close()


def get_pending_users(min_referrals: int) -> list[tuple[int, int]]:
    """
    Возвращает список пользователей, у которых статус 'pending' и
    число приглашённых >= min_referrals.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT u.user_id, COUNT(r.id) AS cnt
        FROM users AS u
        LEFT JOIN referrals AS r
            ON u.user_id = r.referrer_id
        WHERE u.status = 'pending'
        GROUP BY u.user_id
        HAVING cnt >= ?
        """,
        (min_referrals,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def add_winner(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO winners (user_id, selected_at)
        VALUES (?, ?)
        """,
        (
            user_id,
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_all_winners() -> list[int]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM winners")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


# ============= ХЭНДЛЕРЫ =============

@dp.message(CommandStart())
async def cmd_start(message: Message):
    """
    /start с возможным реферальным кодом.
    Пример: t.me/YourBot?start=123456
    """
    user_id = message.from_user.id

    # Сначала проверяем, подписан ли человек на канал
    subscribed = await user_in_channel(user_id)
    if not subscribed:
        await message.answer(
            "Привет! Чтобы участвовать в акции и получить шанс на персональную песню, "
            f"подпишись на наш канал: {CHANNEL_USERNAME}\n\n"
            "После подписки вернись в бота и снова нажми /start."
        )
        return

    # Определяем, есть ли реферальный код
    args = message.text.split(maxsplit=1)
    invited_by = None

    if len(args) > 1:
        ref_arg = args[1].strip()
        if ref_arg.isdigit():
            invited_by = int(ref_arg)
            if invited_by == user_id:
                invited_by = None  # человек не может пригласить сам себя

    # Добавляем пользователя, если его ещё нет
    add_user_if_not_exists(user_id, invited_by)

    # Если есть пригласивший и это не сам человек — запишем реферал
    if invited_by and invited_by != user_id:
        add_referral(invited_by, user_id)

    # Формируем реферальную ссылку
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={user_id}"

    # Считаем рефералов
    referrals_count = count_valid_referrals(user_id)

    await message.answer(
        "Привет! 🎵\n\n"
        "Это бот акции «1+4 = музыка».\n\n"
        "1) Ты подписываешься на наш канал.\n"
        "2) Зовёшь 4 друзей по своей ссылке.\n"
        "3) Ваша компания получает шанс на персональную песню.\n\n"
        f"Твоя реферальная ссылка:\n{ref_link}\n\n"
        f"Сейчас по твоей ссылке приглашено: {referrals_count} человек(а)."
    )


@dp.message()
async def default_handler(message: Message):
    user_id = message.from_user.id

    if message.text == "/my":
        # Показываем, сколько рефералов есть у пользователя
        count = count_valid_referrals(user_id)
        status = get_user_status(user_id) or "unknown"
        await message.answer(
            f"У тебя сейчас {count} приглашённых.\n"
            f"Твой статус: {status}.\n\n"
            f"Нужно пригласить хотя бы {REQUIRED_INVITES} друзей, "
            "чтобы участвовать в розыгрыше."
        )
        return

    if message.text == "/pending" and is_admin(user_id):
        # Показываем всех, кто набрал нужное количество приглашений
        pending_users = get_pending_users(REQUIRED_INVITES)
        if not pending_users:
            await message.answer("Нет пользователей, которые набрали нужное количество приглашённых.")
            return

        lines = ["Пользователи, набравшие нужное количество приглашённых:"]
        for uid, cnt in pending_users:
            lines.append(f"• {uid} — {cnt} приглашённых")

        await message.answer("\n".join(lines))
        return

    if message.text.startswith("/approve") and is_admin(user_id):
        # /approve <user_id>
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /approve <user_id>")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("user_id должен быть числом.")
            return

        set_user_status(target_id, "approved")
        add_winner(target_id)
        await message.answer(f"Пользователь {target_id} отмечен как победитель.")
        try:
            await bot.send_message(
                target_id,
                "Поздравляем! 🎉\n"
                "Ты стал одним из победителей акции. "
                "Мы свяжемся с тобой, чтобы обсудить детали персональной песни.",
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение пользователю {target_id}: {e}")
        return

    if message.text == "/winners" and is_admin(user_id):
        winners = get_all_winners()
        if not winners:
            await message.answer("Победителей пока нет.")
            return

        lines = ["Список победителей:"]
        for uid in winners:
            lines.append(f"• {uid}")
        await message.answer("\n".join(lines))
        return

    # На любые другие сообщения — простая подсказка
    await message.answer(
        "Привет! Я бот акции «1+4 = музыка».\n\n"
        "Команды:\n"
        "/start — получить свою реферальную ссылку\n"
        "/my — посмотреть, сколько друзей ты уже пригласил\n"
        "\n"
        "Администраторские команды:\n"
        "/pending — список тех, кто набрал нужное количество приглашений\n"
        "/approve <user_id> — отметить пользователя как победителя\n"
        "/winners — список победителей"
    )


# -------- ИНИЦИАЛИЗАЦИЯ И ЗАПУСК --------

async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
