import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! I am your schedule assistant. Use /schedule to get the current week's schedule.",
    )

def format_schedule(schedule: dict) -> str:
    """Formats the schedule dictionary into a user-friendly string."""
    if not schedule:
        return "Could not retrieve schedule."

    message = ""
    for date, day_info in schedule.items():
        message += f"📅 *{day_info['day_of_week']}, {date}*\n"
        message += "-" * 20 + "\n"

        if not day_info["lessons"]:
            message += "No lessons for this day.\n\n"
            continue

        for lesson in day_info["lessons"]:
            message += f"*{lesson['lesson_number']}. ({lesson['time']})*\n"
            
            for info in lesson['lessons_info']:
                subject = info.get('subject', 'N/A')
                lesson_type = f"({info.get('type', '')})" if info.get('type') else ""
                message += f"  - {subject} {lesson_type}\n"

                if 'groups' in info:
                    message += f"    Groups: {', '.join(info['groups'])}\n"
                if 'teachers' in info:
                    message += f"    Teacher: {', '.join(info['teachers'])}\n"
                if 'links' in info:
                    for link in info['links']:
                        message += f"    [Link]({link})\n"
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
        await update.message.reply_text("Failed to download schedule data.")
        return

    parsed_schedule = parse_schedule(html)

    if not parsed_schedule:
        await update.message.reply_text("Failed to parse schedule data.")
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
    for i in range(0, len(formatted_message), max_length):
        chunk = formatted_message[i:i+max_length]
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
        
        await update.message.reply_text(f"Success! Your group is set to {group_name}.\n\nNow you can add subjects to monitor with /addsubject or view the full schedule with /schedule.")
    else:
        await update.message.reply_text(f"Could not find or validate group '{group_name}'. Please check the name and try again.")


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
        await update.message.reply_text(f"'{subject_name}' added to your subjects list.")
    else:
        await update.message.reply_text(f"'{subject_name}' is already in your subjects list.")
    
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
        await update.message.reply_text(f"'{subject_to_remove}' removed from your subjects list.")
    else:
        await update.message.reply_text(f"'{subject_to_remove}' was not found in your subjects list.")

    await mysubjects_command(update, context)


async def mysubjects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the user's list of monitored subjects."""
    user_id = update.effective_user.id
    subjects = storage.get_user_setting(user_id, 'subjects', [])
    if subjects:
        message = "You are currently monitoring the following subjects:\n"
        message += "\n".join([f"- {s}" for s in subjects])
        message += "\n\nYour /schedule will only show these subjects. Use /removesubject to remove one or /showall to see the full schedule."
    else:
        message = "You are not monitoring any specific subjects. Your /schedule will show all lessons.\nUse /addsubject to add one."
    
    await update.message.reply_text(message)


async def showall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears the subject filter and shows the full schedule."""
    user_id = update.effective_user.id
    storage.set_user_setting(user_id, 'subjects', [])
    await update.message.reply_text("Subject filter cleared. Fetching the full schedule...")
    await schedule_command(update, context)


def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("setgroup", setgroup_command))
    application.add_handler(CommandHandler("addsubject", addsubject_command))
    application.add_handler(CommandHandler("removesubject", removesubject_command))
    application.add_handler(CommandHandler("mysubjects", mysubjects_command))
    application.add_handler(CommandHandler("showall", showall_command))

    # Run the bot until the user presses Ctrl-C
    application.run_polling()


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in the .env file or environment variables.")
    main()
