import asyncio
import json
import logging
import os
import re
import signal

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import Database
from factorio_client import FactorioClient

logger = logging.getLogger(__name__)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.environ["APIKEY"]
CONTAINER_NAME = os.environ["CONTAINER_NAME"]
RCON_SERVER = os.environ["RCON_SERVER"]
RCON_PORT = int(os.environ["RCON_PORT"])
RCON_PWD = os.environ["RCON_PWD"]
CHAT_LIST = json.loads(os.environ["CHAT_LIST"])

CODE_TO_EMOJI = {
    "[entity=tile-ghost]": "\U0001f47b",
    "[entity=entity-ghost]": "\U0001f47b",
    "[entity=behemoth-biter]": "\U0001fab2",
    "[virtual-signal=signal-skull]": "\U0001f480",
    "[virtual-signal=signal-ghost]": "\U0001f47b",
    "[virtual-signal=signal-check]": "\u2705",
    "[virtual-signal=signal-deny]": "\u274c",
}


def format_factorio_message(log_text: str) -> str:
    match = re.search(r"\[CHAT\] (.*?): (.*)", log_text)
    if match:
        username = match.group(1)
        message = match.group(2)
        for code, emoji in CODE_TO_EMOJI.items():
            if code in message:
                message = message.replace(code, emoji)
        return f"<b>\U0001f472[{username}]</b>: {message}"
    return log_text


def get_attachment_type(message) -> str:
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
        return "[POLL]"
    return ""


# --- Command Handlers ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot_data["db"].add_chat(chat_id, False)
    await context.bot.send_message(
        chat_id, "Chat added. Type /enable_messages to receive Factorio messages."
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot_data["db"].remove_chat(chat_id)
    await context.bot.send_message(chat_id, "Chat removed.")


async def cmd_enable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot_data["db"].add_chat(chat_id, True)
    await context.bot.send_message(chat_id, "Messages enabled.")


async def cmd_disable_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot_data["db"].add_chat(chat_id, False)
    await context.bot.send_message(chat_id, "Messages disabled.")


async def cmd_enable_autopause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    result = await context.bot_data["factorio"].rcon("/config", "set", "auto_pause", "true")
    logger.info(f"Autopause enabled: {result}")
    await context.bot.send_message(chat_id, "Autopause enabled.")


async def cmd_disable_autopause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    result = await context.bot_data["factorio"].rcon("/config", "set", "auto_pause", "false")
    logger.info(f"Autopause disabled: {result}")
    await context.bot.send_message(chat_id, "Autopause disabled.")


async def cmd_restart_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    factorio: FactorioClient = context.bot_data["factorio"]
    await context.bot.send_message(chat_id, "Restarting server...")
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        status = await factorio.restart_container()
    except Exception as e:
        logger.error(f"Restart failed: {e}", exc_info=True)
        await context.bot.send_message(chat_id, f"Error during restart: {e}")
        return

    # Re-attach log monitor to the new container
    queue: asyncio.Queue = context.bot_data["log_queue"]
    factorio.start_log_monitor(queue)

    await context.bot.send_message(chat_id, f"Server restarted. Status: {status}")


async def cmd_set_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    parts = update.message.text.split(" ", 2)
    if len(parts) < 3:
        await context.bot.send_message(
            update.effective_chat.id,
            "Usage: /set_user <name> <color>\nExample: /set_user Engineer #FF0000",
        )
        return
    username, color = parts[1], parts[2]
    await context.bot_data["db"].set_user(user_id, username, color)
    await context.bot.send_message(
        update.effective_chat.id,
        f"Username set to '{username}', color to '{color}'.",
    )


async def forward_to_factorio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    factorio: FactorioClient = context.bot_data["factorio"]
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id

    user = await db.get_user(user_id)
    if user:
        user_name, color = user
    else:
        user_name = update.effective_user.username or update.effective_user.first_name
        color = "#FFFFFF"

    text = update.message.text or ""
    attachment = get_attachment_type(update.message)
    message = f"{attachment} {text}".strip() if attachment else text

    if message:
        await factorio.rcon(f"[color={color}]{user_name}: {message}[/color]")
        logger.info(f"Forwarded to Factorio: {user_name}: {message}")


async def restrict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        f"Blocked: user={update.effective_user.id} "
        f"chat={update.effective_chat.id} msg={update.message.text if update.message else None}"
    )


# --- Log Monitor Consumer ---


async def consume_logs(queue: asyncio.Queue, db: Database, bot):
    while True:
        line = await queue.get()
        if "[JOIN]" in line:
            text = line.split("[JOIN]", 1)[1].strip()
            formatted = f"\U0001f469\ufe0f <b>JOIN:</b> {text}"
        elif "[LEAVE]" in line:
            text = line.split("[LEAVE]", 1)[1].strip()
            formatted = f"\U0001f449 <b>LEAVE:</b> {text}"
        elif "[CHAT]" in line and "<server>" not in line:
            formatted = format_factorio_message(line)
        else:
            continue

        try:
            chats = await db.get_chats()
            for chat_id, messages_enabled in chats:
                if not messages_enabled:
                    continue
                await bot.send_message(
                    chat_id=chat_id,
                    text=formatted,
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Error sending log to TG: {e}", exc_info=True)


# --- Main ---


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


async def main():
    db = Database()
    await db.init()

    factorio = FactorioClient(CONTAINER_NAME, RCON_SERVER, RCON_PORT, RCON_PWD)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )
    app.bot_data["db"] = db
    app.bot_data["factorio"] = factorio

    log_queue: asyncio.Queue = asyncio.Queue()
    app.bot_data["log_queue"] = log_queue

    chat_filter = filters.Chat(CHAT_LIST)
    app.add_handler(CommandHandler("start", cmd_start, filters=chat_filter))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=chat_filter))
    app.add_handler(CommandHandler("set_user", cmd_set_user, filters=chat_filter))
    app.add_handler(CommandHandler("restart_server", cmd_restart_server, filters=chat_filter))
    app.add_handler(CommandHandler("enable_messages", cmd_enable_messages, filters=chat_filter))
    app.add_handler(CommandHandler("disable_messages", cmd_disable_messages, filters=chat_filter))
    app.add_handler(CommandHandler("enable_autopause", cmd_enable_autopause, filters=chat_filter))
    app.add_handler(CommandHandler("disable_autopause", cmd_disable_autopause, filters=chat_filter))
    app.add_handler(MessageHandler(chat_filter, forward_to_factorio))
    app.add_handler(MessageHandler(None, restrict))
    app.add_error_handler(error_handler)

    await app.initialize()
    await app.start()

    factorio.start_log_monitor(log_queue)
    log_task = asyncio.create_task(consume_logs(log_queue, db, app.bot))

    await app.updater.start_polling()
    logger.info(f"Bot started. Allowed chats: {CHAT_LIST}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    logger.info("Shutting down...")

    log_task.cancel()
    await factorio.stop_log_monitor()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await db.close()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
