import asyncio
import logging
import os
import json
import re
import zipfile
import shutil
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, FSInputFile, BufferedInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
import sqlite3
from dotenv import load_dotenv

# ── Загрузка переменных окружения ────────────────────────────────────────────
load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "0"))
ONLYSQ_API_KEY  = os.getenv("ONLYSQ_API_KEY", "openai")   # Получите ключ на https://my.onlysq.ru
API_URL         = "http://api.onlysq.ru/ai/v2"

# ── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── Инициализация бота ────────────────────────────────────────────────────────
bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ── Кастомные символы вместо эмодзи ─────────────────────────────────────────
S = {
    "ok":      "✯",   # успех / галочка
    "err":     "⬊",   # ошибка / неудача
    "warn":    "⬈",   # предупреждение
    "arrow":   "☛",   # стрелка / указатель
    "deco":    "༄",   # декоративный / настройки
    "copy":    "©",   # информация / авторство
    "up":      "⬈",   # направление вверх
    "down":    "⬊",   # направление вниз
}

# ── Доступные модели ──────────────────────────────────────────────────────────
AVAILABLE_MODELS = [
    "gpt-5.2-chat",
    "deepseek-v3", "deepseek-r1",
    "gemini-3-pro", "gemini-3-pro-preview", "gemini-3-flash",
    "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
    "gemini-2.0-flash", "gemini-2.0-flash-lite",
]

# ── FSM состояния ─────────────────────────────────────────────────────────────
class BotStates(StatesGroup):
    waiting_for_code    = State()   # ждём файл
    waiting_for_request = State()   # ждём описание изменений
    chat_mode           = State()   # режим свободного чата с ИИ


# ══════════════════════════════════════════════════════════════════════════════
#  База данных
# ══════════════════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            current_model TEXT    DEFAULT 'gemini-3-flash',
            last_code     TEXT,
            last_filename TEXT,
            requests      INTEGER DEFAULT 0
        )
    """)
    # Добавляем колонку requests если её нет (миграция)
    try:
        c.execute("ALTER TABLE users ADD COLUMN requests INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _ensure_user(c, user_id):
    c.execute(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
        (user_id,)
    )


def get_user_model(user_id: int) -> str:
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    conn.commit()
    c.execute("SELECT current_model FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result and result[0] else "gemini-3-flash"


def set_user_model(user_id: int, model: str):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    c.execute(
        "UPDATE users SET current_model = ? WHERE user_id = ?",
        (model, user_id)
    )
    conn.commit()
    conn.close()


def save_user_code(user_id: int, code: str, filename: str):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    c.execute(
        "UPDATE users SET last_code = ?, last_filename = ? WHERE user_id = ?",
        (code, filename, user_id)
    )
    conn.commit()
    conn.close()


def get_user_code(user_id: int):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    c.execute(
        "SELECT last_code, last_filename FROM users WHERE user_id = ?",
        (user_id,)
    )
    result = c.fetchone()
    conn.close()
    return result if result else (None, None)


def clear_user_cache(user_id: int):
    """Очищает сохранённый код пользователя."""
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    c.execute(
        "UPDATE users SET last_code = NULL, last_filename = NULL WHERE user_id = ?",
        (user_id,)
    )
    conn.commit()
    conn.close()


def increment_requests(user_id: int):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    c.execute(
        "UPDATE users SET requests = requests + 1 WHERE user_id = ?",
        (user_id,)
    )
    conn.commit()
    conn.close()


def get_db_stats():
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT SUM(requests) FROM users")
    reqs = c.fetchone()[0] or 0
    conn.close()
    return total, reqs


# ══════════════════════════════════════════════════════════════════════════════
#  Клавиатуры
# ══════════════════════════════════════════════════════════════════════════════
def main_keyboard(is_admin=False):
    buttons = [
        [KeyboardButton(text="☛ Изменить код"),
         KeyboardButton(text="✯ Чат с ИИ")],
        [KeyboardButton(text="༄ Сменить модель"),
         KeyboardButton(text="© Информация")],
        [KeyboardButton(text="⬈ Поддержка"),
         KeyboardButton(text="⬊ Очистить кэш")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="☛ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def models_keyboard(current_model: str):
    buttons = []
    for i in range(0, len(AVAILABLE_MODELS), 2):
        row = []
        for j in range(i, min(i + 2, len(AVAILABLE_MODELS))):
            model = AVAILABLE_MODELS[j]
            mark = f"{S['ok']} " if model == current_model else ""
            row.append(InlineKeyboardButton(
                text=f"{mark}{model}",
                callback_data=f"model_{model}"
            ))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="☛ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_clear_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{S['ok']} Да, очистить", callback_data="confirm_clear"),
        InlineKeyboardButton(text=f"{S['err']} Отмена",      callback_data="cancel_clear"),
    ]])


# ══════════════════════════════════════════════════════════════════════════════
#  AI функции
# ══════════════════════════════════════════════════════════════════════════════
async def _call_api(messages: list, model: str) -> str | None:
    payload = {
        "model": model,
        "request": {"messages": messages}
    }
    headers = {"Authorization": f"Bearer {ONLYSQ_API_KEY}"}
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            logging.info(f"Запрос к API | модель={model}")
            async with session.post(API_URL, json=payload, headers=headers) as resp:
                text = await resp.text()
                logging.info(f"Ответ API | статус={resp.status} | {text[:200]}")
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return data["choices"][0]["message"]["content"]
                logging.error(f"API вернул {resp.status}: {text}")
    except aiohttp.ClientError as e:
        logging.error(f"Ошибка соединения: {e}")
    except Exception as e:
        logging.error(f"Ошибка API-запроса: {e}")
    return None


async def send_ai_request(code: str, user_request: str, model: str) -> str | None:
    """Запрос на модификацию кода."""
    system_prompt = """Ты AI ассистент для модификации кода.
Пользователь отправит тебе код и запрос на изменение.

ТВОЯ ЗАДАЧА:
1. Проанализировать код
2. Понять что нужно изменить, добавить или удалить
3. Вернуть ТОЛЬКО JSON в следующем формате:

{
  "summary": "Краткое описание что изменено",
  "changes": [
    {
      "action": "replace",
      "old_code": "точный код который нужно заменить",
      "new_code": "новый код на замену"
    },
    {
      "action": "add_after",
      "marker": "код после которого добавить",
      "new_code": "код для добавления"
    },
    {
      "action": "delete",
      "code_to_delete": "код который нужно удалить"
    }
  ]
}

ВАЖНО:
- Возвращай ТОЛЬКО JSON без markdown-блоков и комментариев
- В "old_code" и "marker" — ТОЧНАЯ строка из кода
- Действия: replace | add_after | add_before | delete
"""
    user_prompt = f"КОД:\n```\n{code}\n```\n\nЗАПРОС:\n{user_request}\n\nВерни JSON с изменениями."
    return await _call_api(
        [{"role": "system", "content": system_prompt},
         {"role": "user",   "content": user_prompt}],
        model
    )


async def send_chat_message(history: list, model: str) -> str | None:
    """Свободный диалог с ИИ."""
    system_prompt = (
        "Ты умный и полезный AI ассистент. "
        "Отвечай чётко, по-русски, если пользователь не указал другой язык. "
        "Ты можешь помогать с кодом, вопросами, анализом и любыми задачами."
    )
    messages = [{"role": "system", "content": system_prompt}] + history
    return await _call_api(messages, model)


# ══════════════════════════════════════════════════════════════════════════════
#  Применение изменений к коду
# ══════════════════════════════════════════════════════════════════════════════
def apply_changes(code: str, changes_json: str):
    """Возвращает (success, modified_code, summary)."""
    try:
        cleaned = changes_json
        if "```json" in cleaned:
            cleaned = re.sub(r"```json\s*", "", cleaned)
            cleaned = re.sub(r"```\s*$",    "", cleaned, flags=re.MULTILINE)
        elif "```" in cleaned:
            cleaned = re.sub(r"```\s*", "", cleaned)

        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            cleaned = m.group()

        logging.info(f"Парсинг JSON изменений: {cleaned[:200]}")
        data    = json.loads(cleaned)
        summary = data.get("summary", "Изменения применены")

        modified      = code
        applied_count = 0

        for change in data.get("changes", []):
            action = change.get("action", "")

            if action == "replace":
                old, new = change.get("old_code", ""), change.get("new_code", "")
                if old and old in modified:
                    modified = modified.replace(old, new, 1)
                    applied_count += 1
                else:
                    logging.warning(f"replace: фрагмент не найден — {old[:60]}")

            elif action == "add_after":
                marker, new = change.get("marker", ""), change.get("new_code", "")
                if marker and marker in modified:
                    parts    = modified.split(marker, 1)
                    modified = parts[0] + marker + "\n" + new + parts[1]
                    applied_count += 1
                else:
                    logging.warning(f"add_after: маркер не найден — {marker[:60]}")

            elif action == "add_before":
                marker, new = change.get("marker", ""), change.get("new_code", "")
                if marker and marker in modified:
                    parts    = modified.split(marker, 1)
                    modified = parts[0] + new + "\n" + marker + parts[1]
                    applied_count += 1
                else:
                    logging.warning(f"add_before: маркер не найден — {marker[:60]}")

            elif action == "delete":
                target = change.get("code_to_delete", "")
                if target and target in modified:
                    modified = modified.replace(target, "", 1)
                    applied_count += 1
                else:
                    logging.warning(f"delete: фрагмент не найден — {target[:60]}")

        logging.info(f"Применено изменений: {applied_count}")

        if applied_count == 0:
            return False, None, "Не удалось применить ни одного изменения — AI указал несуществующие фрагменты."

        return True, modified, f"{summary} (применено {applied_count} изм.)"

    except json.JSONDecodeError as e:
        logging.error(f"JSON ошибка: {e}")
        return False, None, f"AI вернул некорректный JSON: {e}"
    except Exception as e:
        logging.error(f"Ошибка apply_changes: {e}")
        return False, None, f"Ошибка применения изменений: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  ZIP-утилита
# ══════════════════════════════════════════════════════════════════════════════
def pack_to_zip(file_path: str, zip_path: str, arcname: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, arcname)


# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    is_admin = message.from_user.id == ADMIN_ID
    name     = message.from_user.first_name or "пользователь"

    _ensure_user_in_db(message.from_user.id)

    await message.answer(
        f"<blockquote>༄ Добро пожаловать, <b>{name}</b>!</blockquote>\n\n"
        f"{S['arrow']} <b>AI Code Editor</b> — умный редактор кода\n\n"
        f"<blockquote>"
        f"☛ Отправьте файл с кодом\n"
        f"☛ Опишите что нужно изменить\n"
        f"☛ Получите готовый ZIP-архив\n"
        f"✯ Или просто пообщайтесь с ИИ"
        f"</blockquote>\n\n"
        f"<blockquote>{S['copy']} Больше проектов: @dreinnh</blockquote>",
        reply_markup=main_keyboard(is_admin),
        parse_mode="HTML"
    )


def _ensure_user_in_db(user_id: int):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
#  /cancel
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("cancel"))
async def cancel_operation(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer(
            f"{S['err']} <b>Операция отменена</b>\n\n"
            "<blockquote>Возвращаемся в главное меню</blockquote>",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID),
            parse_mode="HTML"
        )
    else:
        await message.answer("Нет активных операций для отмены.")


# ══════════════════════════════════════════════════════════════════════════════
#  /admin  +  кнопка "☛ Админ панель"
# ══════════════════════════════════════════════════════════════════════════════
async def _show_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total, reqs = get_db_stats()
    await message.answer(
        f"☛ <b>Админ панель</b>\n\n"
        f"<blockquote>"
        f"{S['copy']} Пользователей: <b>{total}</b>\n"
        f"✯ Всего запросов: <b>{reqs}</b>\n"
        f"༄ Доступно моделей: <b>{len(AVAILABLE_MODELS)}</b>"
        f"</blockquote>",
        parse_mode="HTML"
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    await _show_admin(message)


@dp.message(F.text == "☛ Админ панель")
async def btn_admin(message: Message):
    await _show_admin(message)


# ══════════════════════════════════════════════════════════════════════════════
#  /test  (только для админа)
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("test"))
async def test_api(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    model      = get_user_model(message.from_user.id)
    status_msg = await message.answer(
        f"༄ <b>Тестирование API</b>\n\n"
        f"<blockquote>{S['up']} URL: <code>{API_URL}</code>\n"
        f"✯ Модель: <code>{model}</code></blockquote>\n\n"
        "⏳ Отправка тестового запроса...",
        parse_mode="HTML"
    )
    ai_response = await send_ai_request(
        "print('Hello, World!')", "Добавь комментарий к этой строке", model
    )
    if ai_response:
        await status_msg.edit_text(
            f"{S['ok']} <b>API работает!</b>\n\n"
            f"<blockquote>{S['up']} Соединение установлено\n"
            f"✯ Модель: <code>{model}</code>\n"
            f"{S['copy']} Получено: {len(ai_response)} символов</blockquote>\n\n"
            f"<blockquote>{ai_response[:300]}</blockquote>",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"{S['err']} <b>API не отвечает</b>\n\n"
            f"<blockquote>{S['up']} URL: <code>{API_URL}</code>\n"
            f"✯ Модель: <code>{model}</code></blockquote>\n\n"
            f"{S['warn']} Проверьте логи для деталей",
            parse_mode="HTML"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Кнопка "☛ Изменить код"
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text == "☛ Изменить код")
async def start_code_modification(message: Message, state: FSMContext):
    await message.answer(
        f"{S['arrow']} <b>Отправьте файл с кодом</b>\n\n"
        "<blockquote>Поддерживаются: .py .js .html .css .ts .txt и др.</blockquote>\n\n"
        f"{S['err']} Для отмены: /cancel",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_code)


# ── Получение файла ───────────────────────────────────────────────────────────
@dp.message(BotStates.waiting_for_code, F.document)
async def receive_code_file(message: Message, state: FSMContext):
    document = message.document
    try:
        file      = await bot.get_file(document.file_id)
        raw       = await bot.download_file(file.file_path)
        code      = raw.read().decode("utf-8")

        save_user_code(message.from_user.id, code, document.file_name)

        await message.answer(
            f"{S['ok']} <b>Файл получен!</b>\n\n"
            f"<blockquote>{S['arrow']} Имя: <code>{document.file_name}</code>\n"
            f"{S['copy']} Размер: {len(code):,} символов</blockquote>\n\n"
            f"☛ <b>Опишите что нужно изменить:</b>\n\n"
            "<blockquote>Например:\n"
            "☛ Добавь функцию для...\n"
            "☛ Измени переменную X на Y\n"
            "☛ Удали функцию Z\n"
            "☛ Исправь ошибку в...</blockquote>\n\n"
            f"{S['err']} Для отмены: /cancel",
            parse_mode="HTML"
        )
        await state.set_state(BotStates.waiting_for_request)

    except UnicodeDecodeError:
        await message.answer(
            f"{S['err']} <b>Не удалось прочитать файл</b>\n\n"
            "<blockquote>Файл не является текстовым или имеет неподдерживаемую кодировку.</blockquote>",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка обработки файла: {e}")
        await message.answer(
            f"{S['err']} <b>Ошибка при обработке файла</b>\n\n"
            f"<blockquote>{e}</blockquote>",
            parse_mode="HTML"
        )


@dp.message(BotStates.waiting_for_code)
async def wrong_type_code(message: Message):
    await message.answer(
        f"{S['warn']} <b>Ожидается файл</b>\n\n"
        "<blockquote>Пожалуйста, прикрепите файл с кодом, а не текст.</blockquote>\n\n"
        f"{S['err']} Для отмены: /cancel",
        parse_mode="HTML"
    )


# ── Запрос на изменение ───────────────────────────────────────────────────────
@dp.message(BotStates.waiting_for_request, F.text)
async def receive_modification_request(message: Message, state: FSMContext):
    user_request = message.text
    if user_request.startswith("/"):
        return

    code, filename = get_user_code(message.from_user.id)
    if not code:
        await message.answer(
            f"{S['err']} Код не найден. Пожалуйста, отправьте файл заново."
        )
        await state.clear()
        return

    model      = get_user_model(message.from_user.id)
    status_msg = await message.answer(
        f"✯ <b>ИИ анализирует код...</b>\n\n"
        f"<blockquote>༄ Модель: <code>{model}</code>\n"
        f"{S['copy']} Размер: {len(code):,} символов</blockquote>",
        parse_mode="HTML"
    )

    ai_response = await send_ai_request(code, user_request, model)

    if not ai_response:
        await status_msg.edit_text(
            f"{S['err']} <b>Ошибка связи с ИИ</b>\n\n"
            f"<blockquote>{S['warn']} Возможные причины:\n"
            "☛ Проблемы с интернетом\n"
            "☛ API временно недоступен\n"
            "☛ Модель не поддерживается</blockquote>\n\n"
            f"{S['arrow']} Попробуйте сменить модель через «༄ Сменить модель»",
            parse_mode="HTML"
        )
        await state.clear()
        return

    await status_msg.edit_text(
        f"༄ <b>Применяю изменения...</b>",
        parse_mode="HTML"
    )

    success, modified_code, summary = apply_changes(code, ai_response)

    if not success:
        await status_msg.edit_text(
            f"{S['warn']} <b>ИИ не смог автоматически изменить код</b>\n\n"
            f"<blockquote>☛ Ответ ИИ:\n{ai_response[:400]}</blockquote>\n\n"
            f"<blockquote>{S['err']} {summary}</blockquote>\n\n"
            f"{S['arrow']} <b>Советы:</b>\n"
            "<blockquote>☛ Переформулируйте запрос конкретнее\n"
            "☛ Укажите точные имена функций\n"
            "☛ Сменить модель ИИ</blockquote>",
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Сохраняем изменённый код + создаём ZIP
    new_filename = f"modified_{filename}"
    tmp_dir      = f"/tmp/codebot_{message.from_user.id}"
    os.makedirs(tmp_dir, exist_ok=True)

    file_path = os.path.join(tmp_dir, new_filename)
    zip_name  = new_filename + ".zip"
    zip_path  = os.path.join(tmp_dir, zip_name)

    try:
        # Записываем файл
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(modified_code)

        # Упаковываем в ZIP
        pack_to_zip(file_path, zip_path, new_filename)

        await status_msg.delete()

        zip_file = FSInputFile(zip_path)
        await message.answer_document(
            document=zip_file,
            caption=(
                f"{S['ok']} <b>Готово!</b>\n\n"
                f"<blockquote>༄ {summary}</blockquote>\n\n"
                f"✯ <b>Модель:</b> <code>{model}</code>\n"
                f"{S['arrow']} <b>Файл:</b> <code>{zip_name}</code>"
            ),
            parse_mode="HTML"
        )

        # Обновляем кэш с новым кодом и считаем запрос
        save_user_code(message.from_user.id, modified_code, new_filename)
        increment_requests(message.from_user.id)

    except Exception as e:
        logging.error(f"Ошибка при создании ZIP: {e}")
        await status_msg.edit_text(
            f"{S['err']} <b>Ошибка при создании архива</b>\n\n"
            f"<blockquote>{e}</blockquote>",
            parse_mode="HTML"
        )
    finally:
        # Мгновенная очистка временных файлов
        shutil.rmtree(tmp_dir, ignore_errors=True)

    await state.clear()


@dp.message(BotStates.waiting_for_request)
async def wrong_type_request(message: Message):
    await message.answer(
        f"{S['warn']} <b>Ожидается текстовый запрос</b>\n\n"
        "<blockquote>Опишите текстом что нужно изменить в коде.</blockquote>\n\n"
        f"{S['err']} Для отмены: /cancel",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Кнопка "✯ Чат с ИИ"  — свободный диалог
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text == "✯ Чат с ИИ")
async def start_chat(message: Message, state: FSMContext):
    await state.set_state(BotStates.chat_mode)
    await state.update_data(chat_history=[])
    model = get_user_model(message.from_user.id)
    await message.answer(
        f"✯ <b>Режим чата с ИИ</b>\n\n"
        f"<blockquote>༄ Модель: <code>{model}</code>\n\n"
        f"☛ Задавайте любые вопросы\n"
        f"☛ История сохраняется в течение сессии</blockquote>\n\n"
        f"{S['err']} Для выхода: /cancel",
        parse_mode="HTML"
    )


@dp.message(BotStates.chat_mode, F.text)
async def handle_chat_message(message: Message, state: FSMContext):
    text = message.text
    if text.startswith("/"):
        return

    data    = await state.get_data()
    history = data.get("chat_history", [])

    model      = get_user_model(message.from_user.id)
    status_msg = await message.answer(
        f"✯ <b>ИИ думает...</b>",
        parse_mode="HTML"
    )

    history.append({"role": "user", "content": text})
    response = await send_chat_message(history, model)

    if not response:
        await status_msg.edit_text(
            f"{S['err']} <b>ИИ не ответил</b>\n\n"
            "<blockquote>Попробуйте ещё раз или смените модель.</blockquote>",
            parse_mode="HTML"
        )
        return

    history.append({"role": "assistant", "content": response})

    # Ограничиваем историю последними 20 сообщениями (10 обменами)
    if len(history) > 20:
        history = history[-20:]

    await state.update_data(chat_history=history)
    increment_requests(message.from_user.id)

    # Телеграм лимит — 4096 символов
    if len(response) > 4000:
        response = response[:4000] + "\n\n<i>... (ответ обрезан)</i>"

    await status_msg.edit_text(
        f"✯ <b>ИИ:</b>\n\n{response}",
        parse_mode="HTML"
    )


@dp.message(BotStates.chat_mode)
async def chat_non_text(message: Message):
    await message.answer(
        f"{S['warn']} В режиме чата поддерживается только текст.\n"
        f"{S['err']} Для выхода: /cancel",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Кнопка "⬊ Очистить кэш"
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text == "⬊ Очистить кэш")
async def ask_clear_cache(message: Message):
    await message.answer(
        f"{S['warn']} <b>Очистить кэш?</b>\n\n"
        "<blockquote>Сохранённый код будет удалён.\n"
        "Модель ИИ и статистика останутся.</blockquote>",
        reply_markup=confirm_clear_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "confirm_clear")
async def do_clear_cache(callback: CallbackQuery):
    clear_user_cache(callback.from_user.id)
    await callback.message.edit_text(
        f"{S['ok']} <b>Кэш очищен!</b>\n\n"
        "<blockquote>Все временные данные удалены мгновенно.</blockquote>",
        parse_mode="HTML"
    )
    await callback.answer("Кэш очищен!")


@dp.callback_query(F.data == "cancel_clear")
async def cancel_clear(callback: CallbackQuery):
    await callback.message.edit_text(
        f"{S['err']} Очистка отменена.",
        parse_mode="HTML"
    )
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  Кнопка "༄ Сменить модель"
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text == "༄ Сменить модель")
async def show_models(message: Message):
    current = get_user_model(message.from_user.id)
    await message.answer(
        f"༄ <b>Выбор ИИ модели</b>\n\n"
        f"<blockquote>Текущая: <code>{current}</code></blockquote>\n\n"
        f"<blockquote>{S['arrow']} Выберите модель:</blockquote>",
        reply_markup=models_keyboard(current),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("model_"))
async def select_model(callback: CallbackQuery):
    model = callback.data.split("model_", 1)[1]
    set_user_model(callback.from_user.id, model)
    await callback.message.edit_text(
        f"{S['ok']} <b>Модель изменена!</b>\n\n"
        f"<blockquote>✯ Новая модель: <code>{model}</code></blockquote>",
        parse_mode="HTML"
    )
    await callback.answer(f"Модель {model} установлена!")


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  Кнопка "© Информация"
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text == "© Информация")
async def show_info(message: Message):
    current  = get_user_model(message.from_user.id)
    is_admin = message.from_user.id == ADMIN_ID

    cmds = "/start — главное меню\n/cancel — отмена операции"
    if is_admin:
        cmds += "\n/admin — админ панель\n/test — тест API"

    await message.answer(
        f"{S['copy']} <b>AI Code Editor</b>\n\n"
        "<blockquote>✯ Умный редактор кода на базе ИИ\n"
        "☛ Поддержка любых языков программирования\n"
        "༄ Генерация ZIP-архивов с результатом\n"
        "✯ Свободный чат с ИИ</blockquote>\n\n"
        f"<b>☛ Как использовать:</b>\n"
        "<blockquote>1. Нажмите «☛ Изменить код»\n"
        "2. Отправьте файл с кодом\n"
        "3. Опишите нужные изменения\n"
        "4. Получите ZIP с готовым файлом</blockquote>\n\n"
        f"<b>{S['copy']} Команды:</b>\n"
        f"<blockquote>{cmds}</blockquote>\n\n"
        f"✯ <b>Модель:</b> <code>{current}</code>\n"
        f"༄ <b>Моделей доступно:</b> {len(AVAILABLE_MODELS)}\n\n"
        f"<blockquote>{S['copy']} Больше проектов: @dreinnh</blockquote>",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Кнопка "⬈ Поддержка"
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text == "⬈ Поддержка")
async def show_support(message: Message):
    await message.answer(
        f"{S['up']} <b>Поддержка</b>\n\n"
        "<blockquote>Если у вас возникли вопросы или проблемы — "
        "обращайтесь напрямую:</blockquote>\n\n"
        f"☛ <b>Контакт поддержки:</b> @ke9ab\n"
        f"{S['copy']} <b>Больше проектов:</b> @dreinnh\n\n"
        "<blockquote>༄ Советы по устранению проблем:\n\n"
        "☛ ИИ не отвечает — попробуйте /test\n"
        "☛ Смените модель через «༄ Сменить модель»\n"
        "☛ Переформулируйте запрос конкретнее\n"
        "☛ Очистите кэш через «⬊ Очистить кэш»</blockquote>",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Запуск
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    init_db()
    logging.info("༄ AI Code Editor Bot запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
