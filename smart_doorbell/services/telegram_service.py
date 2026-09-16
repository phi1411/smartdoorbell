from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import requests

from smart_doorbell.repositories.setting_repository import SettingRepository

logger = logging.getLogger(__name__)


class TelegramService:
    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        settings: SettingRepository,
        status_callback: Callable[[], str],
        photo_callback: Callable[[], tuple[bytes | None, str]],
        history_callback: Callable[[], str | dict],
    ):
        self._token = token
        self._chat_id = str(allowed_chat_id)
        self.settings = settings
        self.status_callback = status_callback
        self.photo_callback = photo_callback
        self.history_callback = history_callback
        self._base_url = f"https://api.telegram.org/bot{token}" if token else ""
        self._session = requests.Session()
        self._outbox: queue.Queue = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self._sender_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_success_at: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("telegram_enabled", True))

    def state(self) -> dict:
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "online": bool(
                self.configured and self.enabled and self._last_success_at is not None
                and self._last_error is None
            ),
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
        }

    def start(self) -> None:
        if self._sender_thread and self._sender_thread.is_alive():
            return
        self._stop.clear()
        self._sender_thread = threading.Thread(target=self._sender_loop, name="telegram-sender", daemon=True)
        self._poll_thread = threading.Thread(target=self._poll_loop, name="telegram-poller", daemon=True)
        self._sender_thread.start()
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._sender_thread, self._poll_thread):
            if thread and thread.is_alive():
                thread.join(timeout=3)
        self._session.close()

    def send_photo(self, image: bytes, caption: str, on_success: Callable[[], None] | None = None) -> bool:
        if not self.configured or not self.enabled:
            return False
        try:
            self._outbox.put_nowait(("photo", image, caption, on_success))
            return True
        except queue.Full:
            self._last_error = "Telegram send queue is full"
            logger.error(self._last_error)
            return False

    def send_message(self, text: str) -> bool:
        if not self.configured or not self.enabled:
            return False
        try:
            self._outbox.put_nowait(("message", None, text, None))
            return True
        except queue.Full:
            self._last_error = "Telegram send queue is full"
            return False

    def send_history(self, items: list[tuple[bytes | None, str]]) -> bool:
        """Queue history events one-by-one so every image keeps its own caption."""
        queued = True
        for image, caption in items[:5]:
            sent = (
                self.send_photo(image, caption)
                if image is not None
                else self.send_message(f"{caption}\n📷 Ảnh không còn được lưu")
            )
            queued = sent and queued
        return queued

    def _request(self, method: str, *, data=None, files=None, timeout=(4, 12)) -> dict:
        response = self._session.post(f"{self._base_url}/{method}", data=data, files=files, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram API error"))
        self._last_error = None
        self._last_success_at = time.time()
        return body

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            try:
                kind, image, text, callback = self._outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if kind == "photo":
                    self._request(
                        "sendPhoto",
                        data={"chat_id": self._chat_id, "caption": text},
                        files={"photo": ("doorbell.jpg", image, "image/jpeg")},
                    )
                else:
                    self._request("sendMessage", data={"chat_id": self._chat_id, "text": text})
                if callback:
                    callback()
                logger.info("Telegram %s sent", kind)
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Telegram send failed: %s", exc)
            finally:
                self._outbox.task_done()

    def _poll_loop(self) -> None:
        if not self.configured:
            return
        offset = 0
        while not self._stop.is_set():
            if not self.enabled:
                self._stop.wait(1)
                continue
            try:
                response = self._session.get(
                    f"{self._base_url}/getUpdates",
                    params={"offset": offset, "timeout": 20, "allowed_updates": '["message"]'},
                    timeout=(4, 25),
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise RuntimeError(payload.get("description", "Telegram polling error"))
                self._last_error = None
                self._last_success_at = time.time()
                for update in payload.get("result", []):
                    offset = max(offset, int(update["update_id"]) + 1)
                    self._handle_update(update)
            except Exception as exc:
                if not self._stop.is_set():
                    self._last_error = str(exc)
                    logger.warning("Telegram polling failed: %s", exc)
                    self._stop.wait(3)

    def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = str(message.get("text", "")).split()[0].lower()
        if chat_id != self._chat_id:
            logger.warning("Ignored Telegram command from unauthorized chat")
            return
        if text == "/status":
            self.send_message(self.status_callback())
        elif text == "/photo":
            image, caption = self.photo_callback()
            self.send_photo(image, caption) if image else self.send_message(caption)
        elif text == "/history":
            history = self.history_callback()
            if isinstance(history, dict):
                items = list(history.get("items") or [])
                self.send_history(items) if items else self.send_message(
                    str(history.get("text", "Chưa có sự kiện nào."))
                )
            else:
                self.send_message(str(history))
