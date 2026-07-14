import os
import sys
import ast
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, HTTPServer

import telebot
import gspread
from google import genai
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=dotenv_path, override=True)

# Health check hook – used by the dry-run subprocess validator
if os.getenv("CHECK_HEALTH") == "1":
    print("Health check passed: Imports and initialization successful.")
    sys.exit(0)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "TelegramBotReminders")

# ADMIN ID
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", ""))
except (ValueError, TypeError):
    ADMIN_ID = None

# ---------------------------------------------------------------------------
# Google Sheets setup
# ---------------------------------------------------------------------------
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# Thread-safe lock for all Sheets access
sheets_lock = threading.Lock()
gs_sheet = None   # Will hold the gspread Worksheet after init

def init_google_sheets():
    """Parse credentials from env var JSON and open (or create) the worksheet."""
    global gs_sheet
    creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_json:
        print("Warning: GOOGLE_SHEETS_CREDENTIALS not set. Reminders disabled.")
        return None

    try:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open(GOOGLE_SHEET_NAME)
        worksheet = spreadsheet.sheet1

        # Ensure header row exists
        if worksheet.row_values(1) != ["chat_id", "task", "due_time", "status"]:
            worksheet.insert_row(["chat_id", "task", "due_time", "status"], index=1)

        gs_sheet = worksheet
        print("Google Sheets connected successfully.")
        return worksheet
    except Exception as e:
        print(f"Google Sheets init error: {e}")
        return None

def sheets_add_reminder(chat_id: int, task: str, due_time: str) -> int:
    """Append a new pending reminder row. Returns the new 1-based row index."""
    with sheets_lock:
        gs_sheet.append_row([str(chat_id), task, due_time, "pending"])
        return len(gs_sheet.get_all_values())  # row count including header

def sheets_get_pending(chat_id: int) -> list[dict]:
    """Return all pending reminders for a given chat_id."""
    with sheets_lock:
        rows = gs_sheet.get_all_records()  # list of dicts
    return [
        {"idx": i + 2, **r}          # idx = actual sheet row (1-based, +1 for header)
        for i, r in enumerate(rows)
        if str(r["chat_id"]) == str(chat_id) and r["status"] == "pending"
    ]

def sheets_set_status(row_idx: int, status: str):
    """Update the 'status' column (col 4) for a given row index."""
    with sheets_lock:
        gs_sheet.update_cell(row_idx, 4, status)

def sheets_get_all_pending() -> list[dict]:
    """Return every pending reminder across all users."""
    with sheets_lock:
        rows = gs_sheet.get_all_records()
    return [
        {"idx": i + 2, **r}
        for i, r in enumerate(rows)
        if r["status"] == "pending"
    ]

# ---------------------------------------------------------------------------
# Background reminder checker thread
# ---------------------------------------------------------------------------
def reminder_checker(bot_instance):
    """
    Runs in a daemon thread.
    Every 60 seconds checks the Sheet for overdue reminders and fires them.
    """
    while True:
        time.sleep(60)
        if gs_sheet is None:
            continue
        try:
            now = datetime.now()
            pending = sheets_get_all_pending()
            for row in pending:
                try:
                    due = datetime.strptime(row["due_time"], "%Y-%m-%d %H:%M")
                except ValueError:
                    continue  # skip badly-formatted rows
                if due <= now:
                    chat_id = int(row["chat_id"])
                    task    = row["task"]
                    try:
                        bot_instance.send_message(
                            chat_id,
                            f"⏰ Напоминание: *{task}*",
                            parse_mode="Markdown",
                        )
                        sheets_set_status(row["idx"], "sent")
                    except Exception as e:
                        print(f"Reminder send error for row {row['idx']}: {e}")
        except Exception as e:
            print(f"Reminder checker error: {e}")

# ---------------------------------------------------------------------------
# Render health-check HTTP server (built-in, no Flask)
# ---------------------------------------------------------------------------
class HealthCheckHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        body = "Бот активен и работает!".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence access logs

def run_http_server():
    port = int(os.environ.get("PORT", 5000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------
if not TELEGRAM_TOKEN:
    print("Warning: TELEGRAM_TOKEN is not set.")
if not GEMINI_API_KEY:
    print("Warning: GEMINI_API_KEY is not set.")
if ADMIN_ID is None:
    print("Warning: ADMIN_ID is not configured. Self-modification disabled.")

# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ---------------------------------------------------------------------------
# Telegram bot
# ---------------------------------------------------------------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# Per-user Gemini chat sessions (in-memory)
chat_sessions: dict[int, object] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def admin_only(func):
    """Decorator: allow only ADMIN_ID to call the handler."""
    def wrapper(message, *args, **kwargs):
        if ADMIN_ID is None:
            bot.reply_to(message, "❌ ADMIN_ID не настроен.")
            return
        if message.from_user.id != ADMIN_ID:
            print(f"Unauthorized attempt from {message.from_user.id}")
            return
        return func(message, *args, **kwargs)
    return wrapper

def parse_remind_args(text: str) -> tuple[str, str] | None:
    """
    Parse '/remind Buy milk 2024-07-15 14:30'
    Returns (task, due_time_str) or None on failure.
    Expected format: /remind <task text> <YYYY-MM-DD HH:MM>
    """
    parts = text.strip().split()
    if len(parts) < 4:
        return None
    # Last two tokens are date and time
    due_time = f"{parts[-2]} {parts[-1]}"
    task = " ".join(parts[1:-2])
    try:
        datetime.strptime(due_time, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return task, due_time

# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------
if bot:

    # ── /sheets_debug (admin) ─────────────────────────────────────────────────
    @bot.message_handler(commands=["sheets_debug"])
    @admin_only
    def handle_sheets_debug(message):
        """Run a full diagnostic of the Google Sheets connection and report."""
        lines = ["🔍 *Диагностика Google Sheets*\n"]

        # 1. Check env var presence
        creds_raw  = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        sheet_name = os.getenv("GOOGLE_SHEET_NAME", "TelegramBotReminders")

        if not creds_raw:
            lines.append("❌ *GOOGLE\\_SHEETS\\_CREDENTIALS* — переменная не найдена!")
            lines.append("👉 Проверь, что добавил её на Render в разделе Environment.")
            bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
            return
        lines.append(f"✅ *GOOGLE\\_SHEETS\\_CREDENTIALS* — найдена ({len(creds_raw)} символов)")
        lines.append(f"✅ *GOOGLE\\_SHEET\\_NAME* = `{sheet_name}`\n")

        # 2. JSON parse check
        try:
            creds_dict   = json.loads(creds_raw)
            client_email = creds_dict.get("client_email", "не найден")
            project_id   = creds_dict.get("project_id",  "не найден")
            lines.append("✅ *JSON парсинг* — успешно")
            lines.append(f"  • project\\_id: `{project_id}`")
            lines.append(f"  • client\\_email: `{client_email}`\n")
        except json.JSONDecodeError as e:
            lines.append(f"❌ *JSON парсинг провалился!*\n`{e}`")
            lines.append("👉 Скопируй содержимое JSON-файла заново — возможно, потерялась кавычка или скобка.")
            bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
            return

        # 3. Google Auth check
        try:
            from google.oauth2.service_account import Credentials as _Creds
            _creds = _Creds.from_service_account_info(
                creds_dict,
                scopes=[
                    "https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive",
                ],
            )
            lines.append("✅ *Google Auth* — сервисный аккаунт создан успешно\n")
        except Exception as e:
            lines.append(f"❌ *Google Auth провалился!*\n`{e}`")
            lines.append("👉 Возможно, повреждён `private_key` в JSON.")
            bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
            return

        # 4. gspread connect & open spreadsheet
        try:
            import gspread as _gs
            _gc = _gs.authorize(_creds)
            lines.append("✅ *gspread авторизация* — успешно\n")
            try:
                _spreadsheet = _gc.open(sheet_name)
                _ws = _spreadsheet.sheet1
                _ws.row_values(1)  # real read to confirm access
                lines.append(f"✅ *Таблица '{sheet_name}'* — открыта и доступна!")
                lines.append("\n🎉 *Всё настроено правильно!*")
            except _gs.exceptions.SpreadsheetNotFound:
                lines.append(f"❌ *Таблица '{sheet_name}' не найдена!*")
                lines.append("👉 Две самые частые причины:")
                lines.append(f"  1. Имя таблицы написано не так — проверь пробелы и регистр букв.")
                lines.append(f"  2. Таблица не расшарена сервисному аккаунту `{client_email}`.")
                lines.append(f"     Открой таблицу → Share → вставь этот email → Editor → Share.")
            except Exception as e:
                lines.append(f"❌ *Ошибка при открытии таблицы:*\n`{e}`")
        except Exception as e:
            lines.append(f"❌ *gspread авторизация провалилась:*\n`{e}`")
            lines.append("👉 Возможно, не включён Google Sheets API или Google Drive API в Cloud Console.")

        bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")



    # ── /start  /clear ──────────────────────────────────────────────────────
    @bot.message_handler(commands=["start", "clear"])
    def handle_start_clear(message):
        cid = message.chat.id
        if client:
            chat_sessions[cid] = client.chats.create(model=GEMINI_MODEL)
        bot.reply_to(
            message,
            f"👋 Привет! Я ИИ-ассистент на базе {GEMINI_MODEL}.\n"
            "Я умею держать контекст беседы и ставить напоминания.\n"
            "Напиши /help, чтобы узнать все команды.",
        )

    # ── /help ────────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["help"])
    def handle_help(message):
        text = (
            "📋 *Команды бота:*\n"
            "/start — начать / сбросить диалог\n"
            "/clear — очистить память диалога\n"
            "/remind `<задача> <ГГГГ-ММ-ДД ЧЧ:ММ>` — добавить напоминание\n"
            "  _Пример:_ `/remind Купить молоко 2024-07-15 14:30`\n"
            "/list — список активных напоминаний\n"
            "/done `<номер>` — отметить напоминание выполненным\n"
            "/help — эта справка\n"
        )
        if ADMIN_ID and message.from_user.id == ADMIN_ID:
            text += (
                "\n👑 *Админ-команды:*\n"
                "/update\\_code `<код>` — обновить код бота\n"
                "/rollback — откатить к предыдущей версии\n"
            )
        bot.reply_to(message, text, parse_mode="Markdown")

    # ── /remind ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["remind"])
    def handle_remind(message):
        if gs_sheet is None:
            bot.reply_to(message, "❌ Google Sheets не настроены. Напоминания недоступны.")
            return

        parsed = parse_remind_args(message.text)
        if not parsed:
            bot.reply_to(
                message,
                "❌ Неверный формат.\n"
                "Используй: `/remind <задача> <ГГГГ-ММ-ДД ЧЧ:ММ>`\n"
                "Пример: `/remind Позвонить врачу 2024-07-20 09:00`",
                parse_mode="Markdown",
            )
            return

        task, due_time = parsed
        try:
            sheets_add_reminder(message.chat.id, task, due_time)
            bot.reply_to(
                message,
                f"✅ Напоминание добавлено!\n"
                f"📝 *Задача:* {task}\n"
                f"🕐 *Время:* {due_time}",
                parse_mode="Markdown",
            )
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка при сохранении: {e}")

    # ── /list ─────────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["list"])
    def handle_list(message):
        if gs_sheet is None:
            bot.reply_to(message, "❌ Google Sheets не настроены.")
            return

        try:
            reminders = sheets_get_pending(message.chat.id)
            if not reminders:
                bot.reply_to(message, "📭 У тебя нет активных напоминаний.")
                return

            lines = ["📋 *Твои активные напоминания:*\n"]
            for i, r in enumerate(reminders, start=1):
                lines.append(f"{i}. 📝 *{r['task']}* — 🕐 {r['due_time']}  `(id: {r['idx']})`")

            bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка при получении списка: {e}")

    # ── /done ─────────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["done"])
    def handle_done(message):
        if gs_sheet is None:
            bot.reply_to(message, "❌ Google Sheets не настроены.")
            return

        parts = message.text.strip().split()
        if len(parts) != 2 or not parts[1].isdigit():
            bot.reply_to(
                message,
                "❌ Укажи ID напоминания.\n"
                "Пример: `/done 5`\n"
                "ID можно узнать из команды /list.",
                parse_mode="Markdown",
            )
            return

        row_idx = int(parts[1])
        try:
            # Verify this row belongs to the requesting user before marking done
            with sheets_lock:
                cell_chat_id = gs_sheet.cell(row_idx, 1).value
            if str(cell_chat_id) != str(message.chat.id):
                bot.reply_to(message, "❌ Напоминание с таким ID не найдено.")
                return

            sheets_set_status(row_idx, "done")
            bot.reply_to(message, f"✅ Напоминание #{row_idx} отмечено как выполненное!")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")

    # ── /update_code (self-modification) ─────────────────────────────────────
    @bot.message_handler(commands=["update_code"], content_types=["text", "document"])
    @admin_only
    def handle_update_code(message):
        code_content = None

        if message.document:
            try:
                fi = bot.get_file(message.document.file_id)
                code_content = bot.download_file(fi.file_path).decode("utf-8")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка загрузки файла: {e}")
                return
        else:
            text = (message.text or message.caption or "").strip()
            if text.startswith("/update_code"):
                text = text[len("/update_code"):].strip()
            # Strip markdown code fences
            for fence in ("```python", "```"):
                if text.startswith(fence):
                    text = text[len(fence):]
                    break
            if text.endswith("```"):
                text = text[:-3]
            code_content = text.strip()

        if not code_content:
            bot.reply_to(message, "Пришли код текстом или файлом вместе с командой.")
            return

        bot.reply_to(message, "🔍 Начинаю валидацию кода...")

        # Step A – AST syntax check
        try:
            ast.parse(code_content)
        except SyntaxError as e:
            bot.reply_to(
                message,
                f"❌ Синтаксическая ошибка:\n"
                f"Строка {e.lineno}, смещение {e.offset}\n"
                f"{e.msg}\n`{(e.text or '').strip()}`",
                parse_mode="Markdown",
            )
            return

        # Step B – subprocess dry-run
        tmp = "main.py.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(code_content)
            env = {**os.environ, "CHECK_HEALTH": "1"}
            result = subprocess.run(
                [sys.executable, tmp],
                capture_output=True, text=True, env=env, timeout=10,
            )
            if result.returncode != 0:
                bot.reply_to(
                    message,
                    f"❌ Ошибка при тестовом запуске (код {result.returncode}):\n"
                    f"```\n{result.stderr[:1500]}\n```",
                    parse_mode="Markdown",
                )
                os.remove(tmp)
                return
        except Exception as e:
            bot.reply_to(message, f"❌ Не удалось выполнить проверку: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
            return

        # Step C – apply & hot-reload
        try:
            shutil.copy("main.py", "main.py.bak")
            shutil.move(tmp, "main.py")
            bot.reply_to(message, "✅ Код обновлён. Бэкап создан. Перезапускаю бота...")
            bot.stop_polling()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка перезагрузки: {e}")
            if os.path.exists("main.py.bak") and not os.path.exists("main.py"):
                shutil.copy("main.py.bak", "main.py")

    # ── /rollback ─────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["rollback"])
    @admin_only
    def handle_rollback(message):
        if not os.path.exists("main.py.bak"):
            bot.reply_to(message, "❌ Файл бэкапа не найден.")
            return
        try:
            shutil.move("main.py.bak", "main.py")
            bot.reply_to(message, "✅ Бэкап восстановлен. Перезапускаю...")
            bot.stop_polling()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка отката: {e}")

    # ── Free-form messages → Gemini ──────────────────────────────────────────
    @bot.message_handler(func=lambda m: True)
    def handle_message(message):
        cid = message.chat.id
        if not client:
            bot.reply_to(message, "❌ Gemini API не настроен.")
            return
        bot.send_chat_action(cid, "typing")
        try:
            if cid not in chat_sessions:
                chat_sessions[cid] = client.chats.create(model=GEMINI_MODEL)
            response = chat_sessions[cid].send_message(message.text)
            # Try rich Markdown first; if Telegram rejects the formatting
            # (e.g. unclosed asterisk from Gemini), fall back to plain text.
            try:
                bot.reply_to(message, response.text, parse_mode="Markdown")
            except Exception:
                bot.reply_to(message, response.text)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка Gemini: {e}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not bot:
        print("Ошибка: TELEGRAM_TOKEN не настроен.")
        sys.exit(1)

    # 1. Connect to Google Sheets (non-fatal if creds absent)
    init_google_sheets()

    # 2. Reminder checker background thread (fires every 60 s)
    threading.Thread(target=reminder_checker, args=(bot,), daemon=True).start()

    # 3. Render health-check HTTP server
    threading.Thread(target=run_http_server, daemon=True).start()

    print(f"Бот запущен с моделью {GEMINI_MODEL}.")
    bot.infinity_polling()
