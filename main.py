import logging
import sqlite3
import re
import asyncio
import csv
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from openai import OpenAI
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile,
    ReplyKeyboardRemove
)
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Конфигурация
TELEGRAM_BOT_TOKEN = "8032802242:AAFNq2Y8i5cz2fEh_s-lqV47JZzd-gNtB10"
OPENROUTER_API_KEY = "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"
ADMIN_IDS = {1349134736,1174525096}  # Замените на ваш ID
MAX_REQUESTS_PER_MINUTE = 5
ITEMS_PER_PAGE = 5

# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Инициализация OpenAI
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://your-site.com",
        "X-Title": "Study Planner Bot"
    }
)


# Состояния FSM
class AdminStates(StatesGroup):
    BROADCAST = State()
    BLOCK_USER = State()
    VIEW_QUERIES = State()


# Database класс
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('edu_tutor.db', check_same_thread=False)
        self._init_db()

    def _init_db(self):
        with self.conn:
            # Пользователи
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    interactions INTEGER DEFAULT 0,
                    last_interaction TIMESTAMP,
                    is_blocked BOOLEAN DEFAULT 0,
                    block_reason TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )''')

            # Запросы
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    query_text TEXT,
                    response TEXT,
                    category TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )''')

            # Индексы
            self.conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_queries_user 
                ON queries(user_id, timestamp)''')

    def register_user(self, user_id: int, username: str, full_name: str):
        self.conn.execute('''
            INSERT OR IGNORE INTO users (user_id, username, full_name) 
            VALUES (?, ?, ?)
        ''', (user_id, username, full_name))
        self.conn.commit()

    def log_interaction(self, user_id: int):
        self.conn.execute('''
            UPDATE users 
            SET interactions = interactions + 1,
                last_interaction = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (user_id,))
        self.conn.commit()

    def log_query(self, user_id: int, query: str, response: str, category: Optional[str] = None):
        self.conn.execute('''
            INSERT INTO queries (user_id, query_text, response, category)
            VALUES (?, ?, ?, ?)
        ''', (user_id, query, response, category))
        self.conn.commit()

    def is_blocked(self, user_id: int) -> bool:
        cursor = self.conn.execute(
            'SELECT is_blocked FROM users WHERE user_id = ?',
            (user_id,)
        )
        result = cursor.fetchone()
        return result[0] if result else False

    def block_user(self, user_id: int, reason: str):
        self.conn.execute('''
            UPDATE users 
            SET is_blocked = 1,
                block_reason = ?
            WHERE user_id = ?
        ''', (reason, user_id))
        self.conn.commit()

    def unblock_user(self, user_id: int):
        self.conn.execute('''
            UPDATE users 
            SET is_blocked = 0,
                block_reason = NULL
            WHERE user_id = ?
        ''', (user_id,))
        self.conn.commit()

    def get_stats(self) -> Dict[str, Any]:
        cursor = self.conn.execute('''
            SELECT 
                COUNT(DISTINCT user_id),
                (SELECT COUNT(*) FROM queries),
                SUM(is_blocked),
                (SELECT COUNT(*) FROM users 
                 WHERE registered_at > datetime('now', '-1 day'))
            FROM users
        ''')
        result = cursor.fetchone()
        return {
            'total_users': result[0],
            'total_queries': result[1],
            'blocked_users': result[2] or 0,
            'new_users': result[3]
        }

    def get_queries_page(self, page: int) -> List[Any]:
        offset = (page - 1) * ITEMS_PER_PAGE
        cursor = self.conn.execute('''
            SELECT * FROM queries 
            ORDER BY timestamp DESC 
            LIMIT ? OFFSET ?
        ''', (ITEMS_PER_PAGE, offset))
        return cursor.fetchall()


db = Database()

# Системные сообщения
MESSAGES = {
    'start': "🚀 Я ваш персональный образовательный репетитор!",
    'help': "🆘 Техническая поддержка: @vveemoon\n⏰ Часы работы: Пн-Пт 9:00-20:00",
    'admin_denied': "⛔ Доступ запрещен",
    'broadcast_start': "Введите сообщение для рассылки:",
    'broadcast_cancel': "❌ Рассылка отменена",
    'block_user': "Введите ID пользователя для блокировки:",
    'user_blocked': "✅ Пользователь {user_id} заблокирован",
    'user_unblocked': "✅ Пользователь {user_id} разблокирован",
    'stats': """📊 Статистика:
👥 Всего пользователей: {total_users}
📝 Всего запросов: {total_queries}
🚫 Заблокированных: {blocked_users}
🆕 Новые за 24ч: {new_users}""",
    'request_limit': "🚫 Превышен лимит запросов. Попробуйте позже",
    'layout_fix': "⚠ Возможно опечатка:\n{fixed_text}"
}


# Клавиатуры
async def main_menu():
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="📚 Категории", callback_data="categories"),
        InlineKeyboardButton(text="ℹ Помощь", callback_data="help")
    )
    return keyboard.as_markup()


async def admin_panel():
    keyboard = InlineKeyboardBuilder()
    keyboard.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton(text="📤 Рассылка", callback_data="admin_broadcast"),
        InlineKeyboardButton(text="🚫 Блокировки", callback_data="admin_block"),
        InlineKeyboardButton(text="📥 Экспорт", callback_data="admin_export"),
        InlineKeyboardButton(text="📝 Запросы", callback_data="admin_queries")
    )
    keyboard.adjust(2, repeat=True)
    return keyboard.as_markup()


# Системный промпт
SYSTEM_PROMPT = """
Ты профессиональный образовательный репетитор. Составляй детальные планы по шаблону:
1. 🎯 Цель: [конкретная цель] за [срок]
2. 📅 Ежедневная рутина:
   - Утро: [задача]
   - День: [задача]
   - Вечер: [задача]
3. 📚 Ресурсы:
   - YouTube-каналы: [названия]
   - Учебники: [названия]
4. 📝 Проверка прогресса:
   - Тесты: [формат]
   - Контрольные работы: [периодичность]
5. 💡 Пример задания: [практический пример]
Следуй инструкциям:
- Отвечай только на русском
- Давай конкретные примеры
- Указывай реальные сроки
- Используй эмодзи для структуры
- Минимум 200 слов
"""


# Обработчики команд
@dp.message(CommandStart())
async def start_command(message: Message):
    user = message.from_user
    db.register_user(user.id, user.username, user.full_name)

    if user.id in ADMIN_IDS:
        await message.answer("👋 Добро пожаловать в админ-панель!", reply_markup=await admin_panel())
    else:
        await message.answer(MESSAGES['start'], reply_markup=await main_menu())


@dp.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    await callback.message.edit_text(MESSAGES['help'])


@dp.callback_query(F.data == "categories")
async def show_categories(callback: CallbackQuery):
    keyboard = InlineKeyboardBuilder()
    categories = {
        "academic": "📚 Академические",
        "coding": "💻 Программирование",
        "languages": "🌐 Языки",
        "creative": "🎨 Творчество"
    }

    for key, name in categories.items():
        keyboard.button(text=name, callback_data=f"category_{key}")

    keyboard.adjust(2)
    await callback.message.edit_text(
        "📚 Выберите категорию обучения:",
        reply_markup=keyboard.as_markup()
    )


@dp.callback_query(F.data.startswith("category_"))
async def handle_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[1]
    await state.update_data(category=category)
    await callback.message.answer(f"Выбрана категория: {category}\nВведите ваш запрос:")


@dp.message(F.text)
async def handle_query(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if db.is_blocked(user_id):
        return await message.answer("⚠ Ваш доступ временно ограничен")

    # Проверка лимита запросов
    count = db.conn.execute(
        '''SELECT COUNT(*) FROM queries 
        WHERE user_id = ? AND timestamp > datetime('now', '-1 minute')''',
        (user_id,)
    ).fetchone()[0]

    if count >= MAX_REQUESTS_PER_MINUTE:
        return await message.answer(MESSAGES['request_limit'])

    # Обработка текста
    text = message.text.strip()
    if re.fullmatch(r'^[a-zA-Z]+$', text):
        fixed = fix_keyboard_layout(text)
        return await message.answer(MESSAGES['layout_fix'].format(fixed_text=fixed))

    # Генерация ответа
    try:
        data = await state.get_data()
        response = await generate_response(text, data.get('category'))
        db.log_query(user_id, text, response, data.get('category'))
        await message.answer(response[:4000])
    except Exception as e:
        logging.error(f"Ошибка: {str(e)}")
        await message.answer("⚠ Произошла ошибка при обработке запроса")
    finally:
        await state.clear()


# Админ-панель
@dp.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer(MESSAGES['admin_denied'])

    await message.answer("🔧 Админ-панель:", reply_markup=await admin_panel())


@dp.callback_query(F.data.startswith("admin_"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[1]

    if action == "stats":
        stats = db.get_stats()
        await callback.message.edit_text(
            MESSAGES['stats'].format(**stats),
            reply_markup=await admin_panel()
        )

    elif action == "broadcast":
        await state.set_state(AdminStates.BROADCAST)
        await callback.message.answer(
            MESSAGES['broadcast_start'],
            reply_markup=ReplyKeyboardRemove()
        )

    elif action == "block":
        await state.set_state(AdminStates.BLOCK_USER)
        await callback.message.answer(
            MESSAGES['block_user'],
            reply_markup=ReplyKeyboardRemove()
        )

    elif action == "export":
        await export_data(callback.message)

    elif action == "queries":
        await state.set_state(AdminStates.VIEW_QUERIES)
        await show_queries(callback.message, page=1)

    await callback.answer()


@dp.message(AdminStates.BROADCAST)
async def process_broadcast(message: Message, state: FSMContext):
    cursor = db.conn.execute('SELECT user_id FROM users')
    success = 0

    for row in cursor:
        try:
            await bot.send_message(row[0], message.text)
            success += 1
        except Exception:
            continue

    await message.answer(f"✅ Рассылка завершена\nДоставлено: {success}/{cursor.rowcount}")
    await state.clear()


@dp.message(AdminStates.BLOCK_USER)
async def process_block_user(message: Message, state: FSMContext):
    try:
        user_id = int(message.text)
        db.block_user(user_id, "Административная блокировка")
        await message.answer(MESSAGES['user_blocked'].format(user_id=user_id))
    except ValueError:
        await message.answer("❌ Неверный формат ID пользователя")
    finally:
        await state.clear()


async def show_queries(message: Message, page: int):
    queries = db.get_queries_page(page)
    text = "📝 Последние запросы:\n\n"

    for q in queries:
        text += f"🕒 {q[5]}\n👤 {q[1]}\n📄 {q[2][:50]}...\n\n"

    keyboard = InlineKeyboardBuilder()
    if page > 1:
        keyboard.button(text="◀ Назад", callback_data=f"page_{page - 1}")
    if len(queries) == ITEMS_PER_PAGE:
        keyboard.button(text="▶ Вперед", callback_data=f"page_{page + 1}")

    await message.answer(text, reply_markup=keyboard.as_markup())


async def export_data(message: Message):
    try:
        with open('data.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['User ID', 'Query', 'Response', 'Category', 'Timestamp'])

            cursor = db.conn.execute('SELECT * FROM queries')
            for row in cursor:
                writer.writerow(row)

        await message.answer_document(FSInputFile('data.csv'))
    except Exception as e:
        await message.answer(f"❌ Ошибка экспорта: {str(e)}")


def fix_keyboard_layout(text: str) -> str:
    layout = str.maketrans(
        'qwertyuiop[]asdfghjkl;\'zxcvbnm,./',
        'йцукенгшщзхъфывапролджэячсмитьбю.'
    )
    return text.translate(layout)


async def generate_response(query: str, category: Optional[str] = None) -> str:
    try:
        prompt = SYSTEM_PROMPT
        if category:
            prompt += f"\nКатегория: {category}"

        response = client.chat.completions.create(
            model="mistralai/mixtral-8x7b-instruct",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": query}
            ],
            max_tokens=1000,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Ошибка генерации: {str(e)}")
        return "⚠ Произошла ошибка при генерации ответа"


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    asyncio.run(main())