import asyncio
import logging
import threading
import time
from typing import Any

import docker
from rcon.source import Client as RconClient

logger = logging.getLogger(__name__)


class FactorioClient:
    def __init__(self, container_name: str, rcon_server: str, rcon_port: int, rcon_pwd: str):
        self.container_name = container_name
        self.rcon_server = rcon_server
        self.rcon_port = rcon_port
        self.rcon_pwd = rcon_pwd
        self.docker = docker.from_env()
        self._log_thread: threading.Thread | None = None
        self._log_stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def restart_container(self) -> str:
        status = await asyncio.to_thread(self._restart_sync)
        return status

    def _restart_sync(self) -> str:
        container = self.docker.containers.get(self.container_name)
        container.restart()
        time.sleep(10)
        return container.status

    async def rcon(self, *args: str) -> Any:
        return await asyncio.to_thread(self._rcon_sync, *args)

    def _rcon_sync(self, *args: str) -> Any:
        with RconClient(self.rcon_server, self.rcon_port, passwd=self.rcon_pwd) as client:
            return client.run(*args)

    def start_log_monitor(self, queue: asyncio.Queue):
        self._stop_log_monitor_sync()
        self._log_stop.clear()
        self._loop = asyncio.get_running_loop()
        self._log_thread = threading.Thread(
            target=self._log_reader, args=(queue,), daemon=True
        )
        self._log_thread.start()
        logger.info("Log monitor started")

    def _log_reader(self, queue: asyncio.Queue):
        try:
            container = self.docker.containers.get(self.container_name)
            since = int(time.time())
            logs = container.logs(stream=True, follow=True, since=since)
            for line in logs:
                if self._log_stop.is_set():
                    break
                text = line.decode("utf-8").strip()
                if text:
                    self._loop.call_soon_threadsafe(queue.put_nowait, text)
        except docker.errors.NotFound:
            logger.error(f"Container '{self.container_name}' not found")
        except Exception as e:
            logger.error(f"Log monitor error: {e}", exc_info=True)

    async def stop_log_monitor(self):
        await asyncio.to_thread(self._stop_log_monitor_sync)

    def _stop_log_monitor_sync(self):
        self._log_stop.set()
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=5)
        self._log_thread = None
        logger.info("Log monitor stopped")
