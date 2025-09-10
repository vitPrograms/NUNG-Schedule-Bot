import logging
import os
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)
from telegram.constants import ParseMode
import threading
import asyncio
from flask import Flask
import math

from scraper import get_schedule_html, parse_schedule, parse_unique_subjects
import storage
import re
from bs4 import BeautifulSoup


# Load environment variables from .env file
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set in the .env file or environment variables.")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Define command constants for buttons
SCHEDULE_CMD = "🗓️ Розклад"
MANAGE_SUBJECTS_CMD = "📚 Керувати предметами"
HELP_CMD = "ℹ️ Допомога"

MAIN_KEYBOARD = [
    [SCHEDULE_CMD, MANAGE_SUBJECTS_CMD],
    [HELP_CMD]
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and shows the main menu."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Привіт, {user.mention_html()}! Я ваш асистент з розкладу. Оберіть дію на клавіатурі або скористайтесь /help для перегляду команд.",
        reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True),
    )

def format_schedule(schedule: dict) -> str:
    """Formats the schedule dictionary into a user-friendly HTML string."""
    if not schedule:
        return "Не вдалося отримати розклад або для обраних предметів немає пар."

    message = ""
    for date, day_info in schedule.items():
        message += f"<b>🗓️ {day_info['day_of_week']}, {date}</b>\n"
        message += "─" * 20 + "\n"

        if not day_info["lessons"]:
            message += "  🎉 <i>Пар немає</i>\n\n"
            continue

        for lesson in day_info["lessons"]:
            message += f"<code>{lesson['lesson_number']}. {lesson['time']}</code>\n"
            
            for i, info in enumerate(lesson['lessons_info']):
                if i > 0:
                    message += "  ---\n" # Separator for multiple lessons in one slot

                subject = info.get('subject', 'Невідомо')
                lesson_type = f" ({info.get('type', '')})" if info.get('type') else ""
                message += f"  • <b>{subject}{lesson_type}</b>\n"

                if 'groups' in info:
                    groups_str = ', '.join(info['groups'])
                    message += f"    <i>Групи: {groups_str}</i>\n"
                if 'teachers' in info:
                    message += f"    <i>Викладач: {', '.join(info['teachers'])}</i>\n"
                if 'links' in info:
                    for link in info['links']:
                        # Make links clickable
                        message += f'    <a href="{link}">Посилання на пару</a>\n'
            message += "\n"
        message += "\n"
    
    return message


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetches and displays the schedule for the user's group."""
    user_id = update.effective_user.id
    group_id = storage.get_user_setting(user_id, 'group_id')
    group_name = storage.get_user_setting(user_id, 'group_name')

    if not group_name: # We primarily need the name for the POST request
        await update.message.reply_text(
            "Please set your group first using the /setgroup command.\n"
            "Example: /setgroup ІПм-24-1"
        )
        return

    await update.message.reply_text(f"Fetching schedule for group {group_name}...")
    
    # Prefer using group_id if available (more stable), otherwise use group_name
    fetch_identifier = group_id if group_id else group_name
    html = get_schedule_html(fetch_identifier)
    
    if not html:
        await update.message.reply_text("Не вдалося завантажити дані розкладу.")
        return

    parsed_schedule = parse_schedule(html)

    if not parsed_schedule:
        await update.message.reply_text("Не вдалося розпізнати дані розкладу.", reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True))
        return

    # Filter schedule based on user's subjects
    user_subjects = storage.get_user_setting(user_id, 'subjects', [])
    if user_subjects:
        filtered_schedule = {}
        for date, day_info in parsed_schedule.items():
            filtered_day = {
                "day_of_week": day_info["day_of_week"],
                "lessons": []
            }
            for lesson in day_info["lessons"]:
                filtered_lessons_info = []
                for lesson_info in lesson["lessons_info"]:
                    for user_subject in user_subjects:
                        if user_subject.lower() in lesson_info.get('subject', '').lower():
                            filtered_lessons_info.append(lesson_info)
                            break # Go to next lesson info block
                
                if filtered_lessons_info:
                    filtered_lesson = lesson.copy()
                    filtered_lesson["lessons_info"] = filtered_lessons_info
                    filtered_day["lessons"].append(filtered_lesson)
            
            if filtered_day["lessons"]:
                filtered_schedule[date] = filtered_day
        
        parsed_schedule = filtered_schedule


    formatted_message = format_schedule(parsed_schedule)
    
    # Split message into chunks if it's too long for a single Telegram message
    max_length = 4096
    if len(formatted_message) < max_length:
        await update.message.reply_text(formatted_message, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True))
    else:
        for i in range(0, len(formatted_message), max_length):
            chunk = formatted_message[i:i+max_length]
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True))


async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets the user's group."""
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text("Please provide a group name.\nExample: /setgroup ІПм-24-1")
        return

    group_name = " ".join(context.args)
    
    # We will now try to fetch the schedule with the group name directly.
    # This serves as validation that the group exists.
    await update.message.reply_text(f"Validating group '{group_name}'...")
    html = get_schedule_html(group_name)

    if html and "Розклад групи" in html:
        # The page seems valid, let's try to get the group ID from it for future GET requests
        soup = BeautifulSoup(html, 'lxml')
        link_tag = soup.select_one('h4.hidden-xs a[href*="group="]')
        group_id = None
        if link_tag:
            href = link_tag['href']
            match = re.search(r'group=(-?\d+)', href)
            if match:
                group_id = match.group(1)

        storage.set_user_setting(user_id, 'group_name', group_name)
        if group_id:
            storage.set_user_setting(user_id, 'group_id', group_id)
        
        await update.message.reply_text(f"✅ Групу успішно встановлено на {group_name}.\n\nТепер можна додати предмети для відстеження (/addsubject) або переглянути повний розклад.",
                                      reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True))
    else:
        await update.message.reply_text(f"Не вдалося знайти або перевірити групу '{group_name}'. Перевірте назву та спробуйте ще раз.", reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True))


# --- Interactive Subject Selection ---

SUBJECTS_PER_PAGE = 8

def build_subjects_keyboard(user_id: int, subjects: list[str], page: int = 0) -> InlineKeyboardMarkup:
    """Builds the inline keyboard for subject selection with pagination."""
    user_subjects = storage.get_user_setting(user_id, 'subjects', [])
    
    start_offset = page * SUBJECTS_PER_PAGE
    end_offset = start_offset + SUBJECTS_PER_PAGE
    paginated_subjects = subjects[start_offset:end_offset]

    keyboard = []
    for subject in paginated_subjects:
        status_icon = "✅" if subject in user_subjects else "⬜️"
        button = InlineKeyboardButton(
            f"{status_icon} {subject}",
            callback_data=f"toggle_subject_{page}_{subject}"
        )
        keyboard.append([button])

    # Pagination controls
    total_pages = math.ceil(len(subjects) / SUBJECTS_PER_PAGE)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"subjects_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"subjects_page_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("💾 Зберегти і закрити", callback_data="save_subjects")])
    return InlineKeyboardMarkup(keyboard)


async def manage_subjects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the interactive subject selection menu."""
    user_id = update.effective_user.id
    group_name = storage.get_user_setting(user_id, 'group_name')

    if not group_name:
        await update.message.reply_text("Будь ласка, спочатку встановіть вашу групу за допомогою /setgroup.")
        return

    await update.message.reply_text("Завантажую список предметів...", reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True))
    
    html = get_schedule_html(group_name)
    if not html:
        await update.message.reply_text("Не вдалося завантажити розклад для отримання списку предметів.")
        return
        
    all_subjects = parse_unique_subjects(html)
    if not all_subjects:
        await update.message.reply_text("Не вдалося знайти предмети у вашому розкладі.")
        return

    # Store subjects in context for pagination
    context.user_data['all_subjects'] = all_subjects

    keyboard = build_subjects_keyboard(user_id, all_subjects, page=0)
    await update.message.reply_text(
        "Оберіть предмети, розклад яких ви хочете бачити.\n"
        "Натисніть на предмет, щоб додати/видалити його з фільтра.",
        reply_markup=keyboard
    )


async def subjects_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button clicks from the subject selection menu."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data
    all_subjects = context.user_data.get('all_subjects', [])

    if data.startswith("subjects_page_"):
        page = int(data.split('_')[-1])
        keyboard = build_subjects_keyboard(user_id, all_subjects, page=page)
        await query.edit_message_text("Оберіть предмети:", reply_markup=keyboard)

    elif data.startswith("toggle_subject_"):
        _, page_str, subject = data.split('_', 2)
        page = int(page_str)
        
        user_subjects = storage.get_user_setting(user_id, 'subjects', [])
        if subject in user_subjects:
            user_subjects.remove(subject)
        else:
            user_subjects.append(subject)
        storage.set_user_setting(user_id, 'subjects', user_subjects)

        keyboard = build_subjects_keyboard(user_id, all_subjects, page=page)
        await query.edit_message_reply_markup(keyboard)

    elif data == "save_subjects":
        user_subjects = storage.get_user_setting(user_id, 'subjects', [])
        if user_subjects:
            message = "✅ Ваші налаштування збережено. Ви відстежуєте:\n" + "\n".join(f" - {s}" for s in user_subjects)
        else:
            message = "✅ Налаштування збережено. Ви будете бачити повний розклад."
        await query.edit_message_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a help message with all available commands."""
    # Updated help text to remove old commands
    help_text = (
         "<b>🤖 Доступні команди:</b>\n\n"
        "/start - Почати роботу та показати меню.\n"
        "/schedule - Показати розклад для вашої групи.\n"
        "/setgroup <code>&lt;назва_групи&gt;</code> - Встановити вашу групу. <b>Це потрібно зробити в першу чергу!</b>\n"
        "   <i>Приклад: /setgroup ІПм-24-1</i>\n\n"
        "/managesubjects - Відкрити інтерактивне меню для налаштування фільтрів предметів.\n\n"
        "Кнопки меню дублюють основні команди."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True))


# --- Bot Setup and Threading ---
def setup_bot():
    """Creates and configures the bot application, then returns it."""
    application = Application.builder().token(BOT_TOKEN).build()

    # Add all command and message handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("setgroup", setgroup_command))
    application.add_handler(CommandHandler("managesubjects", manage_subjects_command))

    application.add_handler(MessageHandler(filters.TEXT & (filters.Regex(f"^{SCHEDULE_CMD}$")), schedule_command))
    application.add_handler(MessageHandler(filters.TEXT & (filters.Regex(f"^{MANAGE_SUBJECTS_CMD}$")), manage_subjects_command))
    application.add_handler(MessageHandler(filters.TEXT & (filters.Regex(f"^{HELP_CMD}$")), help_command))
    
    # Add callback handler for inline keyboards
    application.add_handler(CallbackQueryHandler(subjects_callback_handler))

    return application

def bot_thread_target(application: Application):
    """The target function for the bot's thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application.run_polling(stop_signals=None)

# Create the bot application instance
bot_app = setup_bot()

# Start the bot in a separate, non-daemon thread
thread = threading.Thread(target=bot_thread_target, args=(bot_app,))
thread.start()


# --- Flask Web Server ---
# This part keeps the Render instance alive
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

# This block is for local execution only, to easily run the web server.
# When deploying with Gunicorn, Gunicorn runs 'app', and this block is not executed.
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
