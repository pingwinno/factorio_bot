import asyncio
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_DIR = Path("db")


class Database:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._settings_con: sqlite3.Connection | None = None
        self._user_con: sqlite3.Connection | None = None

    def _init_sync(self):
        DB_DIR.mkdir(exist_ok=True)
        self._settings_con = sqlite3.connect(DB_DIR / "settings.db", check_same_thread=False)
        self._settings_con.execute(
            "CREATE TABLE IF NOT EXISTS chat_settings(chat_id NUMERIC PRIMARY KEY, messages_enabled BOOLEAN)"
        )
        self._settings_con.commit()
        self._user_con = sqlite3.connect(DB_DIR / "user_settings.db", check_same_thread=False)
        self._user_con.execute(
            "CREATE TABLE IF NOT EXISTS user_settings(user_id NUMERIC PRIMARY KEY, username TEXT, color TEXT)"
        )
        self._user_con.commit()

    async def init(self):
        await asyncio.to_thread(self._init_sync)
        logger.info("Database initialized")

    async def close(self):
        async with self._lock:
            await asyncio.to_thread(self._close_sync)

    def _close_sync(self):
        if self._settings_con:
            self._settings_con.close()
        if self._user_con:
            self._user_con.close()

    async def add_chat(self, chat_id: int, enabled: bool = False):
        async with self._lock:
            await asyncio.to_thread(
                self._exec_sync,
                self._settings_con,
                "INSERT OR REPLACE INTO chat_settings VALUES(?, ?)",
                (chat_id, int(enabled)),
            )

    async def remove_chat(self, chat_id: int):
        async with self._lock:
            await asyncio.to_thread(
                self._exec_sync,
                self._settings_con,
                "DELETE FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            )

    async def get_chats(self) -> list[tuple[int, bool]]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._query_sync,
                self._settings_con,
                "SELECT * FROM chat_settings",
            )
            return [(r[0], bool(r[1])) for r in rows]

    async def set_user(self, user_id: int, username: str, color: str):
        async with self._lock:
            await asyncio.to_thread(
                self._exec_sync,
                self._user_con,
                "INSERT OR REPLACE INTO user_settings VALUES(?, ?, ?)",
                (user_id, username, color),
            )

    async def get_user(self, user_id: int) -> tuple[str, str] | None:
        async with self._lock:
            row = await asyncio.to_thread(
                self._query_one_sync,
                self._user_con,
                "SELECT username, color FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            return row

    @staticmethod
    def _exec_sync(con: sqlite3.Connection, query: str, params: tuple):
        con.execute(query, params)
        con.commit()

    @staticmethod
    def _query_sync(con: sqlite3.Connection, query: str) -> list:
        return con.execute(query).fetchall()

    @staticmethod
    def _query_one_sync(con: sqlite3.Connection, query: str, params: tuple):
        return con.execute(query, params).fetchone()
