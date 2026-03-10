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
    CallbackQuery, FSInputFile,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
import sqlite3
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))
ONLYSQ_API_KEY = os.getenv("ONLYSQ_API_KEY", "openai")
API_URL        = "http://api.onlysq.ru/ai/v2"
AI_MODEL       = "gemini-2.5-flash-lite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

S = {
    "ok":    "✯",
    "err":   "⬊",
    "warn":  "⬈",
    "arrow": "☛",
    "deco":  "༄",
    "copy":  "©",
}


class BotStates(StatesGroup):
    waiting_for_code    = State()
    waiting_for_request = State()
    chat_mode           = State()


# ══ БД ═══════════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            last_code     TEXT,
            last_filename TEXT,
            requests      INTEGER DEFAULT 0
        )
    """)
    try:
        c.execute("ALTER TABLE users ADD COLUMN requests INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _ensure_user(c, user_id: int):
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def save_user_code(user_id: int, code: str, filename: str):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    c.execute("UPDATE users SET last_code = ?, last_filename = ? WHERE user_id = ?",
              (code, filename, user_id))
    conn.commit()
    conn.close()


def get_user_code(user_id: int):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    c.execute("SELECT last_code, last_filename FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result if result else (None, None)


def clear_user_cache(user_id: int):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET last_code = NULL, last_filename = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def increment_requests(user_id: int):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    c.execute("UPDATE users SET requests = requests + 1 WHERE user_id = ?", (user_id,))
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


def _ensure_user_in_db(user_id: int):
    conn = sqlite3.connect("code_bot.db")
    c = conn.cursor()
    _ensure_user(c, user_id)
    conn.commit()
    conn.close()


# ══ Клавиатуры ════════════════════════════════════════════════════════════════
def main_keyboard(is_admin: bool = False):
    buttons = [
        [KeyboardButton(text="☛ Изменить код"),
         KeyboardButton(text="✯ Чат с ИИ")],
        [KeyboardButton(text="© Информация"),
         KeyboardButton(text="⬈ Поддержка")],
        [KeyboardButton(text="⬊ Очистить кэш")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="☛ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def confirm_clear_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✯ Да, очистить", callback_data="confirm_clear"),
        InlineKeyboardButton(text="⬊ Отмена",       callback_data="cancel_clear"),
    ]])


# ══ AI ════════════════════════════════════════════════════════════════════════
async def _call_api(messages: list) -> str | None:
    payload = {"model": AI_MODEL, "request": {"messages": messages}}
    headers = {"Authorization": f"Bearer {ONLYSQ_API_KEY}"}
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            logging.info(f"Запрос к API | модель={AI_MODEL}")
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
        logging.error(f"Ошибка API: {e}")
    return None


async def send_ai_request(code: str, user_request: str) -> str | None:
    system_prompt = (
        "Ты AI ассистент для модификации кода.\n"
        "Пользователь отправит тебе код и запрос на изменение.\n\n"
        "ТВОЯ ЗАДАЧА:\n"
        "1. Проанализировать код\n"
        "2. Понять что нужно изменить, добавить или удалить\n"
        '3. Вернуть ТОЛЬКО JSON в следующем формате:\n\n'
        '{\n'
        '  "summary": "Краткое описание что изменено",\n'
        '  "changes": [\n'
        '    {"action": "replace", "old_code": "точный код", "new_code": "новый код"},\n'
        '    {"action": "add_after", "marker": "код после которого", "new_code": "добавить"},\n'
        '    {"action": "delete", "code_to_delete": "код для удаления"}\n'
        '  ]\n'
        '}\n\n'
        "ВАЖНО:\n"
        "- Возвращай ТОЛЬКО JSON без markdown и комментариев\n"
        '- В "old_code" и "marker" — ТОЧНАЯ строка из кода\n'
        "- Действия: replace | add_after | add_before | delete"
    )
    user_prompt = "КОД:\n" + code + "\n\nЗАПРОС:\n" + user_request + "\n\nВерни JSON с изменениями."
    return await _call_api([
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ])


async def send_chat_message(history: list) -> str | None:
    system_prompt = (
        "Ты умный и полезный AI ассистент. "
        "Отвечай чётко, по-русски, если пользователь не указал другой язык. "
        "Помогай с кодом, вопросами, анализом и любыми задачами."
    )
    return await _call_api([{"role": "system", "content": system_prompt}] + history)


# ══ Применение изменений ══════════════════════════════════════════════════════
def apply_changes(code: str, changes_json: str):
    try:
        cleaned = changes_json
        if "```json" in cleaned:
            cleaned = re.sub(r"```json\s*", "", cleaned)
            cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE)
        elif "```" in cleaned:
            cleaned = re.sub(r"```\s*", "", cleaned)
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            cleaned = m.group()
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
                    logging.warning(f"replace: не найден — {old[:60]}")
            elif action == "add_after":
                marker, new = change.get("marker", ""), change.get("new_code", "")
                if marker and marker in modified:
                    parts    = modified.split(marker, 1)
                    modified = parts[0] + marker + "\n" + new + parts[1]
                    applied_count += 1
                else:
                    logging.warning(f"add_after: не найден — {marker[:60]}")
            elif action == "add_before":
                marker, new = change.get("marker", ""), change.get("new_code", "")
                if marker and marker in modified:
                    parts    = modified.split(marker, 1)
                    modified = parts[0] + new + "\n" + marker + parts[1]
                    applied_count += 1
                else:
                    logging.warning(f"add_before: не найден — {marker[:60]}")
            elif action == "delete":
                target = change.get("code_to_delete", "")
                if target and target in modified:
                    modified = modified.replace(target, "", 1)
                    applied_count += 1
                else:
                    logging.warning(f"delete: не найден — {target[:60]}")
        logging.info(f"Применено изменений: {applied_count}")
        if applied_count == 0:
            return False, None, "ИИ указал несуществующие фрагменты кода."
        return True, modified, f"{summary} (применено {applied_count} изм.)"
    except json.JSONDecodeError as e:
        return False, None, f"Некорректный JSON от ИИ: {e}"
    except Exception as e:
        return False, None, f"Ошибка применения изменений: {e}"


def pack_to_zip(file_path: str, zip_path: str, arcname: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, arcname)


# ══ /start ════════════════════════════════════════════════════════════════════
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    is_admin = message.from_user.id == ADMIN_ID
    name     = message.from_user.first_name or "пользователь"
    _ensure_user_in_db(message.from_user.id)
    await message.answer(
        f"<blockquote>༄ Добро пожаловать, <b>{name}</b>!</blockquote>\n\n"
        f"{S['arrow']} <b>dreinn.code</b> — умный редактор кода на базе ИИ\n\n"
        "<blockquote>"
        "☛ Отправьте файл с кодом\n"
        "☛ Опишите что нужно изменить\n"
        "☛ Получите готовый ZIP-архив\n"
        "✯ Или просто пообщайтесь с ИИ"
        "</blockquote>\n\n"
        f"<blockquote>{S['copy']} Больше проектов: @dreinnh</blockquote>",
        reply_markup=main_keyboard(is_admin),
        parse_mode="HTML"
    )


# ══ /cancel ═══════════════════════════════════════════════════════════════════
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


# ══ /admin ════════════════════════════════════════════════════════════════════
async def _show_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total, reqs = get_db_stats()
    await message.answer(
        "☛ <b>Админ панель — dreinn.code</b>\n\n"
        "<blockquote>"
        f"{S['copy']} Пользователей: <b>{total}</b>\n"
        f"✯ Всего запросов: <b>{reqs}</b>\n"
        f"༄ Модель: <code>{AI_MODEL}</code>"
        "</blockquote>",
        parse_mode="HTML"
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    await _show_admin(message)


@dp.message(F.text == "☛ Админ панель")
async def btn_admin(message: Message):
    await _show_admin(message)


# ══ /test ═════════════════════════════════════════════════════════════════════
@dp.message(Command("test"))
async def test_api(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    status_msg = await message.answer(
        "༄ <b>Тестирование соединения...</b>\n\n"
        f"<blockquote>✯ Модель: <code>{AI_MODEL}</code></blockquote>",
        parse_mode="HTML"
    )
    ai_response = await send_ai_request("print('Hello, World!')", "Добавь комментарий")
    if ai_response:
        await status_msg.edit_text(
            f"{S['ok']} <b>Соединение активно</b>\n\n"
            f"<blockquote>✯ Модель: <code>{AI_MODEL}</code>\n"
            f"{S['copy']} Получено: {len(ai_response)} символов</blockquote>",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"{S['err']} <b>Нет ответа</b>\n\n"
            f"<blockquote>✯ Модель: <code>{AI_MODEL}</code>\n"
            f"{S['warn']} Проверьте ONLYSQ_API_KEY и интернет</blockquote>",
            parse_mode="HTML"
        )


# ══ Изменить код ══════════════════════════════════════════════════════════════
@dp.message(F.text == "☛ Изменить код")
async def start_code_modification(message: Message, state: FSMContext):
    await message.answer(
        f"{S['arrow']} <b>Отправьте файл с кодом</b>\n\n"
        "<blockquote>Поддерживаются: .py .js .ts .html .css .txt и др.</blockquote>\n\n"
        f"{S['err']} Для отмены: /cancel",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_code)


@dp.message(BotStates.waiting_for_code, F.document)
async def receive_code_file(message: Message, state: FSMContext):
    document = message.document
    try:
        file = await bot.get_file(document.file_id)
        raw  = await bot.download_file(file.file_path)
        code = raw.read().decode("utf-8")
        save_user_code(message.from_user.id, code, document.file_name)
        await message.answer(
            f"{S['ok']} <b>Файл получен!</b>\n\n"
            f"<blockquote>{S['arrow']} Имя: <code>{document.file_name}</code>\n"
            f"{S['copy']} Размер: {len(code):,} символов</blockquote>\n\n"
            "☛ <b>Опишите что нужно изменить:</b>\n\n"
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
        logging.error(f"Ошибка файла: {e}")
        await message.answer(
            f"{S['err']} <b>Ошибка при обработке файла</b>\n\n<blockquote>{e}</blockquote>",
            parse_mode="HTML"
        )


@dp.message(BotStates.waiting_for_code)
async def wrong_type_code(message: Message):
    await message.answer(
        f"{S['warn']} <b>Ожидается файл</b>\n\n"
        "<blockquote>Прикрепите файл с кодом, а не текст.</blockquote>\n\n"
        f"{S['err']} Для отмены: /cancel",
        parse_mode="HTML"
    )


@dp.message(BotStates.waiting_for_request, F.text)
async def receive_modification_request(message: Message, state: FSMContext):
    user_request = message.text
    if user_request.startswith("/"):
        return
    code, filename = get_user_code(message.from_user.id)
    if not code:
        await message.answer(f"{S['err']} Код не найден. Отправьте файл заново.")
        await state.clear()
        return
    status_msg = await message.answer(
        "✯ <b>ИИ анализирует код...</b>\n\n"
        f"<blockquote>{S['copy']} Размер: {len(code):,} символов</blockquote>",
        parse_mode="HTML"
    )
    ai_response = await send_ai_request(code, user_request)
    if not ai_response:
        await status_msg.edit_text(
            f"{S['err']} <b>Ошибка связи с ИИ</b>\n\n"
            "<blockquote>☛ Проблемы с интернетом\n☛ Сервис временно недоступен</blockquote>\n\n"
            f"{S['arrow']} Попробуйте повторить или используйте /test",
            parse_mode="HTML"
        )
        await state.clear()
        return
    await status_msg.edit_text("༄ <b>Применяю изменения...</b>", parse_mode="HTML")
    success, modified_code, summary = apply_changes(code, ai_response)
    if not success:
        await status_msg.edit_text(
            f"{S['warn']} <b>ИИ не смог применить изменения</b>\n\n"
            f"<blockquote>☛ Ответ ИИ:\n{ai_response[:400]}</blockquote>\n\n"
            f"<blockquote>{S['err']} {summary}</blockquote>\n\n"
            f"{S['arrow']} <b>Советы:</b>\n"
            "<blockquote>☛ Переформулируйте запрос конкретнее\n"
            "☛ Укажите точные имена функций\n"
            "☛ Разбейте задачу на шаги</blockquote>",
            parse_mode="HTML"
        )
        await state.clear()
        return
    new_filename = f"modified_{filename}"
    tmp_dir      = f"/tmp/dreinncode_{message.from_user.id}"
    os.makedirs(tmp_dir, exist_ok=True)
    file_path = os.path.join(tmp_dir, new_filename)
    zip_name  = new_filename + ".zip"
    zip_path  = os.path.join(tmp_dir, zip_name)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(modified_code)
        pack_to_zip(file_path, zip_path, new_filename)
        await status_msg.delete()
        await message.answer_document(
            document=FSInputFile(zip_path),
            caption=(
                f"{S['ok']} <b>Готово!</b>\n\n"
                f"<blockquote>༄ {summary}</blockquote>\n\n"
                f"{S['arrow']} <b>Файл:</b> <code>{zip_name}</code>"
            ),
            parse_mode="HTML"
        )
        save_user_code(message.from_user.id, modified_code, new_filename)
        increment_requests(message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка ZIP: {e}")
        await status_msg.edit_text(
            f"{S['err']} <b>Ошибка при создании архива</b>\n\n<blockquote>{e}</blockquote>",
            parse_mode="HTML"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    await state.clear()


@dp.message(BotStates.waiting_for_request)
async def wrong_type_request(message: Message):
    await message.answer(
        f"{S['warn']} <b>Ожидается текстовый запрос</b>\n\n"
        "<blockquote>Опишите текстом что нужно изменить.</blockquote>\n\n"
        f"{S['err']} Для отмены: /cancel",
        parse_mode="HTML"
    )


# ══ Чат с ИИ ══════════════════════════════════════════════════════════════════
@dp.message(F.text == "✯ Чат с ИИ")
async def start_chat(message: Message, state: FSMContext):
    await state.set_state(BotStates.chat_mode)
    await state.update_data(chat_history=[])
    await message.answer(
        "✯ <b>Режим чата с ИИ</b>\n\n"
        "<blockquote>"
        "☛ Задавайте любые вопросы\n"
        "☛ Помогу с кодом, анализом, задачами\n"
        "☛ История сохраняется в течение сессии"
        "</blockquote>\n\n"
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
    status_msg = await message.answer("✯ <b>ИИ думает...</b>", parse_mode="HTML")
    history.append({"role": "user", "content": text})
    response = await send_chat_message(history)
    if not response:
        await status_msg.edit_text(
            f"{S['err']} <b>ИИ не ответил</b>\n\n<blockquote>Попробуйте повторить.</blockquote>",
            parse_mode="HTML"
        )
        return
    history.append({"role": "assistant", "content": response})
    if len(history) > 20:
        history = history[-20:]
    await state.update_data(chat_history=history)
    increment_requests(message.from_user.id)
    if len(response) > 4000:
        response = response[:4000] + "\n\n<i>... (ответ обрезан)</i>"
    await status_msg.edit_text(f"✯ <b>ИИ:</b>\n\n{response}", parse_mode="HTML")


@dp.message(BotStates.chat_mode)
async def chat_non_text(message: Message):
    await message.answer(
        f"{S['warn']} В режиме чата поддерживается только текст.\n"
        f"{S['err']} Для выхода: /cancel",
        parse_mode="HTML"
    )


# ══ Очистить кэш ══════════════════════════════════════════════════════════════
@dp.message(F.text == "⬊ Очистить кэш")
async def ask_clear_cache(message: Message):
    await message.answer(
        f"{S['warn']} <b>Очистить кэш?</b>\n\n"
        "<blockquote>Сохранённый код будет удалён.\nСтатистика останется.</blockquote>",
        reply_markup=confirm_clear_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "confirm_clear")
async def do_clear_cache(callback: CallbackQuery):
    clear_user_cache(callback.from_user.id)
    await callback.message.edit_text(
        f"{S['ok']} <b>Кэш очищен!</b>\n\n<blockquote>Все временные данные удалены.</blockquote>",
        parse_mode="HTML"
    )
    await callback.answer("Кэш очищен!")


@dp.callback_query(F.data == "cancel_clear")
async def cancel_clear(callback: CallbackQuery):
    await callback.message.edit_text(f"{S['err']} Очистка отменена.", parse_mode="HTML")
    await callback.answer()


# ══ Информация ════════════════════════════════════════════════════════════════
@dp.message(F.text == "© Информация")
async def show_info(message: Message):
    is_admin = message.from_user.id == ADMIN_ID
    cmds = "/start — главное меню\n/cancel — отмена операции"
    if is_admin:
        cmds += "\n/admin — админ панель\n/test — проверка соединения"
    await message.answer(
        f"{S['copy']} <b>dreinn.code</b>\n\n"
        "<blockquote>"
        "✯ Умный редактор кода на базе ИИ\n"
        "☛ Поддержка любых языков программирования\n"
        "༄ Генерация ZIP-архивов с результатом\n"
        "✯ Свободный чат с ИИ"
        "</blockquote>\n\n"
        "<b>☛ Как использовать:</b>\n"
        "<blockquote>"
        "1. Нажмите «☛ Изменить код»\n"
        "2. Отправьте файл с кодом\n"
        "3. Опишите нужные изменения\n"
        "4. Получите ZIP с готовым файлом"
        "</blockquote>\n\n"
        f"<b>{S['copy']} Команды:</b>\n<blockquote>{cmds}</blockquote>\n\n"
        f"<blockquote>{S['copy']} Больше проектов: @dreinnh</blockquote>",
        parse_mode="HTML"
    )


# ══ Поддержка ═════════════════════════════════════════════════════════════════
@dp.message(F.text == "⬈ Поддержка")
async def show_support(message: Message):
    await message.answer(
        f"{S['arrow']} <b>Поддержка</b>\n\n"
        "<blockquote>Если возникли вопросы или проблемы — обращайтесь напрямую:</blockquote>\n\n"
        "☛ <b>Контакт поддержки:</b> @ke9ab\n"
        f"{S['copy']} <b>Больше проектов:</b> @dreinnh\n\n"
        "<blockquote>༄ Советы:\n\n"
        "☛ ИИ не отвечает — попробуйте /test\n"
        "☛ Переформулируйте запрос конкретнее\n"
        "☛ Очистите кэш через «⬊ Очистить кэш»"
        "</blockquote>",
        parse_mode="HTML"
    )


# ══ Запуск ════════════════════════════════════════════════════════════════════
async def main():
    init_db()
    logging.info(f"༄ dreinn.code запущен | модель: {AI_MODEL}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())