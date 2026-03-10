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

# ── Переменные окружения ──────────────────────────────────────────
load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))
ONLYSQ_API_KEY = os.getenv("ONLYSQ_API_KEY", "openai")
API_URL        = "http://api.onlysq.ru/ai/v2"

# Фиксированная модель
AI_MODEL = "gemini-2.5-flash-lite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

S = {
    "ok":    "✟",
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


# ══ БД ══════════════════════════════════════════════════════════════════════════════
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
    c.execute("UPDATE users SET last_code = ?, last_filename = ? WHERE user_id = ?", (code, filename, user_id))
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


# ══ Клавиатуры ══════════════════════════════════════════════════════════════════════════
def main_keyboard(is_admin: bool = False):
    buttons = [
        [KeyboardButton(text="☛ Изменить код"),
         KeyboardButton(text="✟ Чат с ИИ")],
        [KeyboardButton(text="© Информация"),
         KeyboardButton(text="⬈ Поддержка")],
        [KeyboardButton(text="⬊ Очистить кэш")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="☛ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def confirm_clear_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{S['ok']} Да, очистить", callback_data="confirm_clear"),
        InlineKeyboardButton(text=f"{S['err']} Отмена",      callback_data="cancel_clear"),
    ]])


# ══ AI ══════════════════════════════════════════════════════════════════════════════
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
    user_prompt = f"КОД:
```
{code}
```

ЗАПРОС:
{user_request}

Верни JSON с изменениями."
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
            elif action == "add_after":
                marker, new = change.get("marker", ""), change.get("new_code", "")
                if marker and marker in modified:
                    parts    = modified.split(marker, 1)
                    modified = parts[0] + marker + "
" + new + parts[1]
                    applied_count += 1
            elif action == "add_before":
                marker, new = change.get("marker", ""), change.get("new_code", "")
                if marker and marker in modified:
                    parts    = modified.split(marker, 1)
                    modified = parts[0] + new + "
" + marker + parts[1]
                    applied_count += 1
            elif action == "delete":
                target = change.get("code_to_delete", "")
                if target and target in modified:
                    modified = modified.replace(target, "", 1)
                    applied_count += 1
        if applied_count == 0:
            return False, None, "Не удалось применить изменения — ИИ указал несуществующие фрагменты."
        return True, modified, f"{summary} (применено {applied_count} изм.)"
    except json.JSONDecodeError as e:
        return False, None, f"Некорректный JSON от ИИ: {e}"
    except Exception as e:
        return False, None, f"Ошибка применения изменений: {e}"


def pack_to_zip(file_path: str, zip_path: str, arcname: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, arcname)


# ══ /start ════════════════════════════════════════════════════════════════════════════
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    is_admin = message.from_user.id == ADMIN_ID
    name     = message.from_user.first_name or "пользователь"
    _ensure_user_in_db(message.from_user.id)
    await message.answer(
        f"<blockquote>༄ Добро пожаловать, <b>{name}</b>!</blockquote>

"
        f"{S['arrow']} <b>dreinn.code</b> — умный редактор кода на базе ИИ

"
        "<blockquote>"
        "☛ Отправьте файл с кодом
"
        "☛ Опишите что нужно изменить
"
        "☛ Получите готовый ZIP-архив
"
        "✟ Или просто пообщайтесь с ИИ"
        "</blockquote>

"
        f"<blockquote>{S['copy']} Больше проектов: @dreinnh</blockquote>",
        reply_markup=main_keyboard(is_admin),
        parse_mode="HTML"
    )


@dp.message(Command("cancel"))
async def cancel_operation(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer(
            f"{S['err']} <b>Операция отменена</b>

"
            "<blockquote>Возвращаемся в главное меню</blockquote>",
            reply_markup=main_keyboard(message.from_user.id == ADMIN_ID),
            parse_mode="HTML"
        )
    else:
        await message.answer("Нет активных операций для отмены.")


async def _show_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total, reqs = get_db_stats()
    await message.answer(
        "☛ <b>Админ панель — dreinn.code</b>

"
        "<blockquote>"
        f"{S['copy']} Пользователей: <b>{total}</b>
"
        f"✟ Всего запросов: <b>{reqs}</b>
"
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


@dp.message(Command("test"))
async def test_api(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    status_msg = await message.answer(
        "༄ <b>Тестирование соединения...</b>

"
        f"<blockquote>✟ Модель: <code>{AI_MODEL}</code></blockquote>",
        parse_mode="HTML"
    )
    ai_response = await send_ai_request("print('Hello, World!')", "Добавь комментарий")
    if ai_response:
        await status_msg.edit_text(
            f"{S['ok']} <b>Соединение активно</b>

"
            f"<blockquote>✟ Модель: <code>{AI_MODEL}</code>
"
            f"{S['copy']} Получено: {len(ai_response)} символов</blockquote>",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"{S['err']} <b>Нет ответа</b>

"
            f"<blockquote>✟ Модель: <code>{AI_MODEL}</code>
"
            f"{S['warn']} Проверьте ONLYSQ_API_KEY и интернет-соединение</blockquote>",
            parse_mode="HTML"
        )


@dp.message(F.text == "☛ Изменить код")
async def start_code_modification(message: Message, state: FSMContext):
    await message.answer(
        f"{S['arrow']} <b>Отправьте файл с кодом</b>

"
        "<blockquote>Поддерживаются: .py .js .ts .html .css .txt и др.</blockquote>

"
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
            f"{S['ok']} <b>Файл получен!</b>

"
            f"<blockquote>{S['arrow']} Имя: <code>{document.file_name}</code>
"
            f"{S['copy']} Размер: {len(code):,} символов</blockquote>

"
            "☛ <b>Опишите что нужно изменить:</b>

"
            "<blockquote>Например:
"
            "☛ Добавь функцию для...
"
            "☛ Измени переменную X на Y
"
            "☛ Удали функцию Z
"
            "☛ Исправь ошибку в...</blockquote>

"
            f"{S['err']} Для отмены: /cancel",
            parse_mode="HTML"
        )
        await state.set_state(BotStates.waiting_for_request)
    except UnicodeDecodeError:
        await message.answer(
            f"{S['err']} <b>Не удалось прочитать файл</b>

"
            "<blockquote>Файл не является текстовым или имеет неподдерживаемую кодировку.</blockquote>",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка файла: {e}")
        await message.answer(f"{S['err']} <b>Ошибка при обработке файла</b>

<blockquote>{e}</blockquote>", parse_mode="HTML")


@dp.message(BotStates.waiting_for_code)
async def wrong_type_code(message: Message):
    await message.answer(
        f"{S['warn']} <b>Ожидается файл</b>

"
        "<blockquote>Прикрепите файл с кодом, а не текст.</blockquote>

"
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
        "✟ <b>ИИ анализирует код...</b>

"
        f"<blockquote>{S['copy']} Размер: {len(code):,} символов</blockquote>",
        parse_mode="HTML"
    )
    ai_response = await send_ai_request(code, user_request)
    if not ai_response:
        await status_msg.edit_text(
            f"{S['err']} <b>Ошибка связи с ИИ</b>

"
            "<blockquote>☛ Проблемы с интернетом
☛ Сервис временно недоступен</blockquote>

"
            f"{S['arrow']} Попробуйте повторить запрос или используйте /test",
            parse_mode="HTML"
        )
        await state.clear()
        return
    await status_msg.edit_text("༄ <b>Применяю изменения...</b>", parse_mode="HTML")
    success, modified_code, summary = apply_changes(code, ai_response)
    if not success:
        await status_msg.edit_text(
            f"{S['warn']} <b>ИИ не смог применить изменения</b>

"
            f"<blockquote>☛ Ответ ИИ:
{ai_response[:400]}</blockquote>

"
            f"<blockquote>{S['err']} {summary}</blockquote>

"
            f"{S['arrow']} <b>Советы:</b>
"
            "<blockquote>☛ Переформулируйте запрос конкретнее
☛ Укажите точные имена функций
☛ Разбейте задачу на шаги</blockquote>",
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
                f"{S['ok']} <b>Готово!</b>

"
                f"<blockquote>༄ {summary}</blockquote>

"
                f"{S['arrow']} <b>Файл:</b> <code>{zip_name}</code>"
            ),
            parse_mode="HTML"
        )
        save_user_code(message.from_user.id, modified_code, new_filename)
        increment_requests(message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка ZIP: {e}")
        await status_msg.edit_text(f"{S['err']} <b>Ошибка при создании архива</b>

<blockquote>{e}</blockquote>", parse_mode="HTML")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    await state.clear()


@dp.message(BotStates.waiting_for_request)
async def wrong_type_request(message: Message):
    await message.answer(
        f"{S['warn']} <b>Ожидается текстовый запрос</b>

"
        "<blockquote>Опишите текстом что нужно изменить.</blockquote>

"
        f"{S['err']} Для отмены: /cancel",
        parse_mode="HTML"
    )


@dp.message(F.text == "✟ Чат с ИИ")
async def start_chat(message: Message, state: FSMContext):
    await state.set_state(BotStates.chat_mode)
    await state.update_data(chat_history=[])
    await message.answer(
        "✟ <b>Режим чата с ИИ</b>

"
        "<blockquote>☛ Задавайте любые вопросы
☛ Помогу с кодом, анализом, задачами
☛ История сохраняется в течение сессии</blockquote>

"
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
    status_msg = await message.answer("✟ <b>ИИ думает...</b>", parse_mode="HTML")
    history.append({"role": "user", "content": text})
    response = await send_chat_message(history)
    if not response:
        await status_msg.edit_text(f"{S['err']} <b>ИИ не ответил</b>

<blockquote>Попробуйте повторить запрос.</blockquote>", parse_mode="HTML")
        return
    history.append({"role": "assistant", "content": response})
    if len(history) > 20:
        history = history[-20:]
    await state.update_data(chat_history=history)
    increment_requests(message.from_user.id)
    if len(response) > 4000:
        response = response[:4000] + "

<i>... (ответ обрезан)</i>"
    await status_msg.edit_text(f"✟ <b>ИИ:</b>

{response}", parse_mode="HTML")


@dp.message(BotStates.chat_mode)
async def chat_non_text(message: Message):
    await message.answer(f"{S['warn']} В режиме чата поддерживается только текст.
{S['err']} Для выхода: /cancel", parse_mode="HTML")


@dp.message(F.text == "⬊ Очистить кэш")
async def ask_clear_cache(message: Message):
    await message.answer(
        f"{S['warn']} <b>Очистить кэш?</b>

"
        "<blockquote>Сохранённый код будет удалён.
Статистика останется.</blockquote>",
        reply_markup=confirm_clear_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "confirm_clear")
async def do_clear_cache(callback: CallbackQuery):
    clear_user_cache(callback.from_user.id)
    await callback.message.edit_text(
        f"{S['ok']} <b>Кэш очищен!</b>

<blockquote>Все временные данные удалены.</blockquote>",
        parse_mode="HTML"
    )
    await callback.answer("Кэш очищен!")


@dp.callback_query(F.data == "cancel_clear")
async def cancel_clear(callback: CallbackQuery):
    await callback.message.edit_text(f"{S['err']} Очистка отменена.", parse_mode="HTML")
    await callback.answer()


@dp.message(F.text == "© Информация")
async def show_info(message: Message):
    is_admin = message.from_user.id == ADMIN_ID
    cmds = "/start — главное меню
/cancel — отмена операции"
    if is_admin:
        cmds += "
/admin — админ панель
/test — проверка соединения"
    await message.answer(
        f"{S['copy']} <b>dreinn.code</b>

"
        "<blockquote>✟ Умный редактор кода на базе ИИ
☛ Поддержка любых языков программирования
༄ Генерация ZIP-архивов с результатом
✟ Свободный чат с ИИ</blockquote>

"
        "<b>☛ Как использовать:</b>
"
        "<blockquote>1. Нажмите «☛ Изменить код»
2. Отправьте файл с кодом
3. Опишите нужные изменения
4. Получите ZIP с готовым файлом</blockquote>

"
        f"<b>{S['copy']} Команды:</b>
<blockquote>{cmds}</blockquote>

"
        f"<blockquote>{S['copy']} Больше проектов: @dreinnh</blockquote>",
        parse_mode="HTML"
    )


@dp.message(F.text == "⬈ Поддержка")
async def show_support(message: Message):
    await message.answer(
        f"{S['arrow']} <b>Поддержка</b>

"
        "<blockquote>Если возникли вопросы или проблемы — обращайтесь напрямую:</blockquote>

"
        "☛ <b>Контакт поддержки:</b> @ke9ab
"
        f"{S['copy']} <b>Больше проектов:</b> @dreinnh

"
        "<blockquote>༄ Советы:

"
        "☛ ИИ не отвечает — попробуйте /test
"
        "☛ Переформулируйте запрос конкретнее
"
        "☛ Очистите кэш через «⬊ Очистить кэш»"
        "</blockquote>",
        parse_mode="HTML"
    )


async def main():
    init_db()
    logging.info(f"༄ dreinn.code запущен | модель: {AI_MODEL}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())