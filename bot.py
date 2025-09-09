import logging
import os
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
import threading
import asyncio
from flask import Flask

from scraper import get_schedule_html, parse_schedule
import storage
import re
from bs4 import BeautifulSoup


# Load environment variables from .env file
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Define command constants for buttons
SCHEDULE_CMD = "🗓️ Розклад"
MY_SUBJECTS_CMD = "📚 Мої предмети"
HELP_CMD = "ℹ️ Допомога"

MAIN_KEYBOARD = [
    [SCHEDULE_CMD, MY_SUBJECTS_CMD],
    [HELP_CMD]
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and shows the main menu."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Привіт, {user.mention_html()}! Я ваш асистент з розкладу. Оберіть дію на клавіатурі.",
        reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True),
    )

def format_schedule(schedule: dict) -> str:
    """Formats the schedule dictionary into a user-friendly fixed-width string."""
    if not schedule:
        return "Не вдалося отримати розклад або для обраних предметів немає пар."

    message = "```\n" # Start of monospace block
    for date, day_info in schedule.items():
        header = f"🗓️ {day_info['day_of_week']}, {date} "
        message += f"{header}\n"
        message += "─" * (len(header) - 1) + "\n\n"

        if not day_info["lessons"]:
            message += "  🎉 Пар немає\n\n"
            continue

        for lesson in day_info["lessons"]:
            time_str = f"🕘 {lesson['time']} | Пара: {lesson['lesson_number']}"
            message += f"{time_str}\n"
            
            for i, info in enumerate(lesson['lessons_info']):
                if i > 0:
                    message += "  ---\n" # Separator for multiple lessons in one slot

                subject = info.get('subject', 'Невідомо')
                lesson_type = f" ({info.get('type', '')})" if info.get('type') else ""
                message += f"  Предмет: {subject}{lesson_type}\n"

                if 'groups' in info:
                    groups_str = ', '.join(info['groups'])
                    message += f"  Групи: {groups_str}\n"
                if 'teachers' in info:
                    message += f"  Викладач: {', '.join(info['teachers'])}\n"
                if 'links' in info:
                    for link in info['links']:
                        message += f"  Посилання: {link}\n"
            message += "\n"
        message += "=" * 25 + "\n\n"
    
    message += "```" # End of monospace block
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
        await update.message.reply_text("Не вдалося розпізнати дані розкладу.")
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
        await update.message.reply_text(formatted_message, parse_mode=ParseMode.MARKDOWN)
    else:
        for i in range(0, len(formatted_message), max_length):
            # Ensure code blocks are properly closed in each chunk
            chunk = formatted_message[i:i+max_length]
            if chunk.startswith("```") and not chunk.endswith("```"):
                chunk += "```"
            if not chunk.startswith("```") and chunk.endswith("```"):
                chunk = "```" + chunk
            if not chunk.startswith("```") and not chunk.endswith("```"):
                chunk = "```" + chunk + "```"

            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)


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
        await update.message.reply_text(f"Не вдалося знайти або перевірити групу '{group_name}'. Перевірте назву та спробуйте ще раз.")


async def addsubject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Adds a subject to the user's monitored list."""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Please provide a subject name (or part of it).\nExample: /addsubject Креативна економіка")
        return
    
    subject_name = " ".join(context.args)
    subjects = storage.get_user_setting(user_id, 'subjects', [])
    
    if subject_name.lower() not in [s.lower() for s in subjects]:
        subjects.append(subject_name)
        storage.set_user_setting(user_id, 'subjects', subjects)
        await update.message.reply_text(f"✅ Предмет '{subject_name}' додано до вашого списку.")
    else:
        await update.message.reply_text(f"Предмет '{subject_name}' вже є у вашому списку.")
    
    await mysubjects_command(update, context)


async def removesubject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes a subject from the user's monitored list."""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Please provide a subject name to remove.\nExample: /removesubject Креативна економіка")
        return

    subject_to_remove = " ".join(context.args)
    subjects = storage.get_user_setting(user_id, 'subjects', [])
    
    original_count = len(subjects)
    subjects = [s for s in subjects if s.lower() != subject_to_remove.lower()]

    if len(subjects) < original_count:
        storage.set_user_setting(user_id, 'subjects', subjects)
        await update.message.reply_text(f"🗑️ Предмет '{subject_to_remove}' видалено зі списку.")
    else:
        await update.message.reply_text(f"Предмет '{subject_to_remove}' не знайдено у вашому списку.")

    await mysubjects_command(update, context)


async def mysubjects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the user's list of monitored subjects."""
    user_id = update.effective_user.id
    subjects = storage.get_user_setting(user_id, 'subjects', [])
    if subjects:
        message = "Ви відстежуєте розклад для таких предметів:\n"
        message += "\n".join([f" - {s}" for s in subjects])
        message += "\n\nКоманда /schedule показуватиме пари лише для них. Використовуйте /removesubject, щоб видалити предмет, або /showall, щоб побачити повний розклад."
    else:
        message = "Ви не відстежуєте жодного предмету. /schedule показуватиме всі пари.\n\nВикористовуйте /addsubject, щоб додати предмет до списку."
    
    await update.message.reply_text(message)


async def showall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears the subject filter and shows the full schedule."""
    user_id = update.effective_user.id
    storage.set_user_setting(user_id, 'subjects', [])
    await update.message.reply_text("Фільтр предметів очищено. Завантажую повний розклад...")
    await schedule_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays a help message with all available commands."""
    help_text = (
        "🤖 *Доступні команди:*\n\n"
        "*/start* - Почати роботу та показати меню.\n"
        "*/schedule* - Показати розклад для вашої групи (з урахуванням фільтрів).\n"
        "*/setgroup <назва_групи>* - Встановити вашу групу. *Це потрібно зробити в першу чергу!*\n"
        "   _Приклад: /setgroup ІПм-24-1_\n\n"
        "*/addsubject <назва>* - Додати предмет до фільтра. Можна вводити часткову назву.\n"
        "   _Приклад: /addsubject Креативна економіка_\n\n"
        "*/removesubject <назва>* - Видалити предмет з фільтра.\n"
        "*/mysubjects* - Показати список предметів, які ви відстежуєте.\n"
        "*/showall* - Очистити фільтр предметів і показати повний розклад.\n\n"
        "Також можна використовувати кнопки в меню для швидкого доступу."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("setgroup", setgroup_command))
    application.add_handler(CommandHandler("addsubject", addsubject_command))
    application.add_handler(CommandHandler("removesubject", removesubject_command))
    application.add_handler(CommandHandler("mysubjects", mysubjects_command))
    application.add_handler(CommandHandler("showall", showall_command))

    # Add handlers for menu buttons
    application.add_handler(MessageHandler(filters.TEXT & (filters.Regex(f"^{SCHEDULE_CMD}$")), schedule_command))
    application.add_handler(MessageHandler(filters.TEXT & (filters.Regex(f"^{MY_SUBJECTS_CMD}$")), mysubjects_command))
    application.add_handler(MessageHandler(filters.TEXT & (filters.Regex(f"^{HELP_CMD}$")), help_command))


    # This function will run in a separate thread
    def bot_thread_target():
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # run_polling is a blocking call that will run forever
        application.run_polling()

    # Start the bot in a separate thread
    thread = threading.Thread(target=bot_thread_target)
    thread.start()


# Flask web server to keep the bot alive on Render
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in the .env file or environment variables.")
    main()
    # The Flask app is run by gunicorn as defined in the Procfile,
    # so we don't need app.run() here when deploying.
    # For local testing, you might add it like this:
    # if os.environ.get('FLASK_ENV') == 'development':
    #     app.run(port=int(os.environ.get('PORT', 8080)))
