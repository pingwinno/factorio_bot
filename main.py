import asyncio
import json
import logging
import os
import sqlite3
import time
from threading import Thread

import docker
from telegram import Update, Bot
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Load environment variables
bot_token = os.environ['APIKEY']
container_name = os.environ['CONTAINER_NAME']
chat_list = json.loads(os.environ['CHAT_LIST'])

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


async def restrict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} from chat {update.effective_chat.id} sends {update.message.text}")


# === TELEGRAM COMMAND HANDLERS === #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /start command.")
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, False])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id,
                                   text="Chat added. Type /enable_messages to receive Factorio messages.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /stop command.")
    chat_id = update.message.chat_id
    settings_cur.execute(delete_chat, [chat_id])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id, text="Chat deleted.")


async def enable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /enable_messages command.")
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, True])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id, text="Messages enabled.")


async def disable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /disable_messages command.")
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, False])
    settings_con.commit()
    await context.bot.send_message(chat_id=chat_id, text="Messages disabled.")


async def restart_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /restart_server command.")
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Restarting...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    container = client.containers.get(container_name)
    try:
        container.restart()
        await asyncio.sleep(10)
    except docker.errors.NotFound as error:
        logging.error(f"Can't restart server '{error}'.")
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=f"Error during server restart: {error}")

    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Server restarted. Status {container.status}")


# === CONTINUOUS LOG FILE MONITORING === #
def monitor_logs() -> None:
    logging.info("Starting log monitoring...")
    try:
        container = client.containers.get(container_name)

        logs = container.logs(stream=True, follow=True, since=int(time.time()))
        for log in logs:
            line = log.decode("utf-8").strip()
            logging.info(f"Log line: {line}")

            message_text = ""
            if "[JOIN]" in line:
                send_message(line.split("[JOIN]")[1].strip())
            elif "[LEAVE]" in line:
                send_message(line.split("[LEAVE]")[1].strip())
            elif "[CHAT]" in line:
                send_message(line, True)

    except docker.errors.NotFound:
        logging.error(f"Container '{container_name}' not found.")
    except Exception as e:
        logging.error(f"Error in log monitoring: {e}")


def send_message(message, is_chat=False):
    logging.info(f"Send message: {message}")
    chats = settings_cur.execute(get_chats).fetchall()
    for chat in chats:
        bot = Bot(token=bot_token)
        logging.info(f"Sending message to chat: {chat}")
        if is_chat and chat[1] == 0:
            return
        asyncio.run(bot.send_message(chat_id=chat[0], text=message))


if __name__ == '__main__':
    logging.info("Starting Telegram bot...")
    logging.info(f"Allowed chat ids: {chat_list}")
    print(chat_list)
    print(type(chat_list[0]))
    application = ApplicationBuilder().token(bot_token).build()
    application.add_handler(CommandHandler('start', start, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('restart_server', restart_server, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('enable_messages', enable_messages, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('disable_messages', disable_messages, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('stop', stop, filters=filters.Chat(chat_list)))
    application.add_handler(MessageHandler(None, callback=restrict))

    # Start log monitoring in a separate thread
    thread1 = Thread(target=monitor_logs, daemon=True)
    thread1.start()
    # Run bot polling in the main thread
    application.run_polling()
