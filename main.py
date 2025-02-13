import asyncio
import logging
import os
import sqlite3
import time

import docker
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# Load environment variables
bot_token = os.environ['APIKEY']
container_name = os.environ.get('CONTAINER_NAME')

# Ensure the database folder exists
if not os.path.exists("db"):
    os.mkdir("db")
client = docker.from_env()

# Setup SQLite database
settings_con = sqlite3.connect("db/settings.db", check_same_thread=False)
settings_cur = settings_con.cursor()
settings_cur.execute("CREATE TABLE IF NOT EXISTS chat_settings(chat_id NUMERIC PRIMARY KEY, messages_enabled BOOLEAN)")

# SQL Queries
add_chat = "INSERT OR REPLACE INTO chat_settings VALUES(?, ?);"
get_chats = "SELECT * FROM chat_settings;"
delete_chat = "DELETE FROM chat_settings WHERE chat_id = ?;"

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)


# === TELEGRAM COMMAND HANDLERS === #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, False])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id,
                                   text="Chat added. Type /enable_messages to receive Factorio messages.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    settings_cur.execute(delete_chat, [chat_id])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id, text="Chat deleted.")


async def enable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, True])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id, text="Messages enabled.")


async def disable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, False])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id, text="Messages disabled.")


# === CONTINUOUS LOG FILE MONITORING === #
async def monitor_logs():
    bot = Bot(token=bot_token)
    try:
        container = client.containers.get(container_name)

        # Fetch logs since script start time (only new logs)
        logs=   container.logs(stream=True, follow=True, since=int(time.time()))

        for log in logs:  # Standard for-loop since logs is NOT async
            message_text = log.decode("utf-8").strip()
            logging.info(f"Log line: {message_text}")

            if "[JOIN]" in message_text:
                message_text = message_text.split("[JOIN]")[1].strip()
            elif "[LEAVE]" in message_text:
                message_text = message_text.split("[LEAVE]")[1].strip()

            if message_text:
                chats = settings_cur.execute(get_chats).fetchall()
                for chat in chats:
                    await bot.send_message(chat_id=chat[0], text=message_text)

    except docker.errors.NotFound:
        logging.error(f"Container '{container_name}' not found.")
    except Exception as e:
        logging.error(f"Error: {e}")


# === MAIN FUNCTION === #
async def main():
    application = ApplicationBuilder().token(bot_token).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('enable_messages', enable_messages))
    application.add_handler(CommandHandler('disable_messages', disable_messages))
    application.add_handler(CommandHandler('stop', stop))

    async with application:
        await application.start()
        await application.updater.start_polling()

        # Start log monitoring in parallel
        log_task = asyncio.create_task(monitor_logs())

        try:
            # Keep the event loop running
            while True:
                await asyncio.sleep(0.1)
        except KeyboardInterrupt:
            logging.info("Shutting down bot...")

        # Stop everything gracefully
        log_task.cancel()
        await application.updater.stop()
        await application.stop()


# === RUNNING THE BOT === #
if __name__ == "__main__":
    asyncio.run(main())  # Ensures a clean event loop
