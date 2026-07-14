import os
import sys
import ast
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
import telebot
from google import genai
from dotenv import load_dotenv

# Load environment variables
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=dotenv_path, override=True)

# 1. Health check hook for validating new code versions
if os.getenv("CHECK_HEALTH") == "1":
    print("Health check passed: Imports and initialization successful.")
    sys.exit(0)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# ADMIN ID verification
ADMIN_ID_ENV = os.getenv("ADMIN_ID")
try:
    ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None
except ValueError:
    ADMIN_ID = None

# Simple HTTP Request Handler for Render health checks (standard library, no Flask required)
class HealthCheckHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write("Бот активен и работает!".encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    # Disable logging to stdout to keep bot console output clean
    def log_message(self, format, *args):
        pass

def run_http_server():
    port = int(os.environ.get("PORT", 5000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
    print("Warning: TELEGRAM_TOKEN is not set correctly in the .env file.")

if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
    print("Warning: GEMINI_API_KEY is not set correctly in the .env file.")

if ADMIN_ID is None:
    print("Warning: ADMIN_ID is not configured or invalid. Self-modification will be disabled.")

# Configure Gemini
if GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here":
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    client = None

# Initialize Bot
if TELEGRAM_TOKEN and TELEGRAM_TOKEN != "your_telegram_bot_token_here":
    bot = telebot.TeleBot(TELEGRAM_TOKEN)
else:
    bot = None

# Dictionary to hold chat sessions (context memory)
chat_sessions = {}

# Admin verification helper decorator
def admin_only(func):
    def wrapper(message, *args, **kwargs):
        if ADMIN_ID is None:
            bot.reply_to(message, "Ошибка: ADMIN_ID не настроен на сервере.")
            return
        if message.from_user.id != ADMIN_ID:
            print(f"Unauthorized access attempt by user ID {message.from_user.id}")
            return
        return func(message, *args, **kwargs)
    return wrapper

if bot:
    # 2. Update Code handler
    @bot.message_handler(commands=['update_code'], content_types=['text', 'document'])
    @admin_only
    def handle_update_code(message):
        code_content = None
        
        # Check if code is sent as a document attachment
        if message.document:
            try:
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                code_content = downloaded_file.decode('utf-8')
            except Exception as e:
                bot.reply_to(message, f"Ошибка при загрузке файла: {e}")
                return
        else:
            # Extract code from text
            text = message.text or message.caption or ""
            # Strip command name
            if text.startswith("/update_code"):
                text = text[len("/update_code"):].strip()
            
            # Clean up markdown code blocks
            if text.startswith("```python"):
                text = text[9:]
            elif text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            
            code_content = text.strip()

        if not code_content:
            bot.reply_to(
                message, 
                "Пожалуйста, пришлите код после команды `/update_code` (или в виде прикрепленного файла `main.py`)."
            )
            return

        bot.reply_to(message, "Начинаю валидацию кода...")

        # Step A: Validate syntax using AST
        try:
            ast.parse(code_content)
        except SyntaxError as syntax_err:
            error_details = (
                f"❌ Ошибка синтаксиса (AST validation failed):\n"
                f"Строка: {syntax_err.lineno}, Смещение: {syntax_err.offset}\n"
                f"Ошибка: {syntax_err.msg}\n"
                f"Код: `{syntax_err.text.strip() if syntax_err.text else ''}`"
            )
            bot.reply_to(message, error_details)
            return
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка парсинга AST: {e}")
            return

        # Step B: Subprocess dry-run check
        temp_filename = "main.py.tmp"
        try:
            with open(temp_filename, "w", encoding="utf-8") as f:
                f.write(code_content)
            
            # Run the new script with CHECK_HEALTH environment variable set to "1"
            run_env = os.environ.copy()
            run_env["CHECK_HEALTH"] = "1"
            
            result = subprocess.run(
                [sys.executable, temp_filename],
                capture_output=True,
                text=True,
                env=run_env,
                timeout=10
            )
            
            if result.returncode != 0:
                error_details = (
                    f"❌ Тестовый запуск завершился с ошибкой (код {result.returncode}):\n\n"
                    f"**stderr:**\n```\n{result.stderr}\n```\n"
                    f"**stdout:**\n```\n{result.stdout}\n```"
                )
                bot.reply_to(message, error_details)
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
                return

        except Exception as e:
            bot.reply_to(message, f"❌ Не удалось провести проверку запуска: {e}")
            if os.path.exists(temp_filename):
                os.remove(temp_filename)
            return

        # Step C: Apply changes and restart
        try:
            # Backup current main.py
            shutil.copy("main.py", "main.py.bak")
            
            # Move temp file to main.py
            shutil.move(temp_filename, "main.py")
            
            bot.reply_to(message, "✅ Валидация успешна! Бэкап создан. Перезапускаю бота...")
            
            # Stop polling and execute self
            bot.stop_polling()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка во время горячей перезагрузки: {e}")
            if os.path.exists("main.py.bak") and not os.path.exists("main.py"):
                shutil.copy("main.py.bak", "main.py")

    # 3. Rollback command
    @bot.message_handler(commands=['rollback'])
    @admin_only
    def handle_rollback(message):
        if not os.path.exists("main.py.bak"):
            bot.reply_to(message, "❌ Файл бэкапа `main.py.bak` не найден.")
            return
        
        bot.reply_to(message, "🔄 Восстанавливаю предыдущую версию из бэкапа...")
        try:
            shutil.move("main.py.bak", "main.py")
            bot.reply_to(message, "✅ Бэкап восстановлен. Перезапускаю бота...")
            bot.stop_polling()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            bot.reply_to(message, f"❌ Не удалось восстановить бэкап: {e}")

    # Standard bot handlers
    @bot.message_handler(commands=['start', 'clear'])
    def handle_start_clear(message):
        chat_id = message.chat.id
        if client:
            chat_sessions[chat_id] = client.chats.create(model=GEMINI_MODEL)
        
        welcome_text = (
            f"Привет! Я твой личный ИИ-ассистент на базе {GEMINI_MODEL}.\n"
            "Я умею держать контекст нашей беседы. Чтобы очистить мою память, напиши /clear."
        )
        bot.reply_to(message, welcome_text)

    @bot.message_handler(commands=['help'])
    def handle_help(message):
        help_text = (
            "Доступные команды:\n"
            "/start - Начать беседу сначала\n"
            "/clear - Сбросить память бота\n"
            "/help - Показать справочное сообщение\n"
        )
        if ADMIN_ID is not None and message.from_user.id == ADMIN_ID:
            help_text += (
                "\n👑 Админ-команды:\n"
                "/update_code <код> - Обновить код бота (или прикрепите файл main.py с этой командой в подписи)\n"
                "/rollback - Откатить код до предыдущей версии"
            )
        bot.reply_to(message, help_text)

    @bot.message_handler(func=lambda message: True)
    def handle_message(message):
        chat_id = message.chat.id
        user_text = message.text
        
        if not client:
            bot.reply_to(message, "Ошибка: API-ключ Gemini не настроен.")
            return

        bot.send_chat_action(chat_id, 'typing')
        
        try:
            if chat_id not in chat_sessions:
                chat_sessions[chat_id] = client.chats.create(model=GEMINI_MODEL)
            
            chat = chat_sessions[chat_id]
            response = chat.send_message(user_text)
            bot.reply_to(message, response.text)
        except Exception as e:
            bot.reply_to(message, f"Произошла ошибка при генерации ответа: {e}")

if __name__ == "__main__":
    if bot:
        # Run HTTP Server in a daemon thread so it does not block telebot polling
        threading.Thread(target=run_http_server, daemon=True).start()
        print(f"Бот успешно запущен с моделью {GEMINI_MODEL}...")
        bot.infinity_polling()
    else:
        print("Ошибка запуска: Настройте TELEGRAM_TOKEN and GEMINI_API_KEY в файле .env")
