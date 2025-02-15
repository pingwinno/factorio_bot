import asyncio
import json
import logging
import multiprocessing
import os
import re
import sqlite3
import time

import docker
from rcon.source import Client
from telegram import Update, Bot
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

code_to_emoji = {
    "[entity=tile-ghost]": "👻",  # Ghost (Tile)
    "[entity=entity-ghost]": "👻",  # Ghost (Entity)
    "[entity=behemoth-biter]": "🪲",  # Behemoth Biter (Closest match: T-Rex)
    "[virtual-signal=signal-skull]": "💀",  # Skull
    "[virtual-signal=signal-ghost]": "👻",  # Ghost
    "[virtual-signal=signal-check]": "✅",  # Check mark
    "[virtual-signal=signal-deny]": "❌"  # Cross mark
}

# Load environment variables
bot_token = os.environ['APIKEY']
container_name = os.environ['CONTAINER_NAME']
rcon_server = os.environ['RCON_SERVER']
rcon_port = int(os.environ['RCON_PORT'])
rcon_pwd = os.environ['RCON_PWD']

chat_list = json.loads(os.environ['CHAT_LIST'])

# Ensure the database folder exists
if not os.path.exists("db"):
    os.mkdir("db")
client = docker.from_env()

# Setup SQLite database
settings_con = sqlite3.connect("db/settings.db", check_same_thread=False)
settings_cur = settings_con.cursor()
settings_cur.execute("CREATE TABLE IF NOT EXISTS chat_settings(chat_id NUMERIC PRIMARY KEY, messages_enabled BOOLEAN)")

user_con = sqlite3.connect("db/user_settings.db", check_same_thread=False)
user_cur = user_con.cursor()
user_cur.execute("CREATE TABLE IF NOT EXISTS user_settings(user_id NUMERIC PRIMARY KEY, username TEXT, color TEXT)")
# SQL Queries
add_chat = "INSERT OR REPLACE INTO chat_settings VALUES(?, ?);"
get_chats = "SELECT * FROM chat_settings;"
delete_chat = "DELETE FROM chat_settings WHERE chat_id = ?;"

add_user = "INSERT OR REPLACE INTO user_settings VALUES(?, ?, ?);"
get_user = "SELECT username, color FROM user_settings WHERE user_id = ?;"

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

monitor_thread = None


async def restrict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} from chat {update.effective_chat.id} sends {update.message.text}")


# === TELEGRAM COMMAND HANDLERS === #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /start command.")
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, False])
    settings_con.commit()
    await context.bot.send_message_to_tg(chat_id=chat_id,
                                         text="Chat added. Type /enable_messages to receive Factorio messages.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /stop command.")
    chat_id = update.message.chat_id
    settings_cur.execute(delete_chat, [chat_id])
    settings_con.commit()
    await context.bot.send_message_to_tg(chat_id=chat_id, text="Chat deleted.")


async def enable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /enable_messages command.")
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, True])
    settings_con.commit()
    await context.bot.send_message_to_tg(chat_id=chat_id, text="Messages enabled.")


async def disable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /disable_messages command.")
    chat_id = update.message.chat_id
    settings_cur.execute(add_chat, [chat_id, False])
    settings_con.commit()
    await context.bot.send_message_to_tg(chat_id=chat_id, text="Messages disabled.")


async def restart_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Received /restart_server command.")
    await context.bot.send_message_to_tg(chat_id=update.effective_chat.id, text=f"Restarting...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    container = client.containers.get(container_name)
    try:
        container.restart()
        await asyncio.sleep(10)
    except docker.errors.NotFound as error:
        logging.error(f"Can't restart server '{error}'.")
        await context.bot.send_message_to_tg(chat_id=update.effective_chat.id,
                                             text=f"Error during server restart: {error}")

    await context.bot.send_message_to_tg(chat_id=update.effective_chat.id,
                                         text=f"Server restarted. Status {container.status}")
    stop_monitor_process()
    start_monitor_process()


async def forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    if message:
        message = f"{get_message_type(update.message)} {message}"
    else:
        message = get_message_type(update.message)
    user_id = update.message.from_user.id
    user_metadata = user_cur.execute(get_user, [user_id]).fetchone()
    user_name = user_metadata[0] if user_metadata else update.message.from_user.username
    color = user_metadata[1] if user_metadata else "#FFFFFF"
    send_message_to_factorio(f"{user_name}: {message}", color)


async def set_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    data = update.message.text.split(" ")
    user_name = data[1]
    color = data[2]
    user_cur.execute(add_user, [user_id, user_name, color])
    user_con.commit()
    user_metadata = user_cur.execute(get_user, [user_id]).fetchone()
    user_name = user_metadata[0] if user_metadata else update.message.from_user.username
    color = user_metadata[1] if user_metadata else "#FFFFFF"
    await context.bot.send_message_to_tg(chat_id=update.effective_chat.id,
                                         text=f"Username is set to '{user_name}'.\n Color is set to '{color}'.")


def monitor_logs() -> None:
    logging.info("Starting log monitoring...")
    try:
        container = client.containers.get(container_name)

        logs = container.logs(stream=True, follow=True, since=int(time.time()))
        for log in logs:
            line = log.decode("utf-8").strip()
            logging.info(f"Log line: {line}")

            if "[JOIN]" in line:
                send_message_to_tg(line.split("[JOIN]")[1].strip())
            elif "[LEAVE]" in line:
                send_message_to_tg(line.split("[LEAVE]")[1].strip())
            elif "[CHAT]" in line and not "<server>" in line:
                send_message_to_tg(line, True)

    except docker.errors.NotFound:
        logging.error(f"Container '{container_name}' not found.")
    except Exception as e:
        logging.error(f"Error in log monitoring: {e}")


def send_message_to_tg(message, is_chat=False):
    logging.info(f"Send message to TG: {message}")
    message = format_tg_message(message)
    chats = settings_cur.execute(get_chats).fetchall()
    for chat in chats:
        bot = Bot(token=bot_token)
        logging.info(f"Sending message to chat: {chat}")
        if is_chat and chat[1] == 0:
            return
        asyncio.run(bot.send_message(chat_id=chat[0], text=message, parse_mode="HTML"))


def send_message_to_factorio(message, color=None):
    logging.info(f"Send message to Factorio: {message}")

    with Client(rcon_server, rcon_port, passwd=rcon_pwd) as client:
        client.run(f"[color={color}]{message}[/color]")


def start_monitor_process():
    global monitor_thread
    monitor_thread = multiprocessing.Process(target=monitor_logs, daemon=True)
    monitor_thread.start()


def stop_monitor_process():
    monitor_thread.terminate()

def get_message_type(message):
    if message.photo:
        return "[IMAGE]"
    if message.video:
        return "[VIDEO]"
    if message.document:
        return "[FILE]"
    if message.sticker:
        return "[STICKER]"
    if message.voice:
        return "[VOICE]"
    if message.audio:
        return "[AUDIO]"
    if message.contact:
        return "[CONTACT]"
    if message.location:
        return "[LOCATION]"
    if message.poll:
        return "[POOL]"

def format_tg_message(log_text):
    match = re.search(r"\[CHAT\] (.*?): (.*)", log_text)
    if match:
        username = match.group(1)  # Extracts 'unknown.device'
        message = match.group(2)  # Extracts the actual message
        for code in code_to_emoji.keys():
            if code in message:
                message.replace(code, code_to_emoji[code])
        logging.info("Username:", username)
        logging.info("Message:", message)
        return f"<b>{username}</b>: {message}"
    return log_text

if __name__ == '__main__':
    logging.info("Starting Telegram bot...")
    logging.info(f"Allowed chat ids: {chat_list}")
    print(chat_list)
    print(type(chat_list[0]))
    application = ApplicationBuilder().token(bot_token).build()
    application.add_handler(CommandHandler('start', start, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('set_user', set_user, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('restart_server', restart_server, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('enable_messages', enable_messages, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('disable_messages', disable_messages, filters=filters.Chat(chat_list)))
    application.add_handler(CommandHandler('stop', stop, filters=filters.Chat(chat_list)))
    application.add_handler(MessageHandler(filters=filters.Chat(chat_list), callback=forward))
    application.add_handler(MessageHandler(None, callback=restrict))

    # Start log monitoring in a separate thread
    start_monitor_process()
    # Run bot polling in the main thread
    application.run_polling()
