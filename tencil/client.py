"""Connection to the Tencil daemon on a background thread.

bpy isn't thread-safe, so the thread only moves JSON: received messages go into
a queue that a Blender timer drains on the main thread (`poll`).
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Callable

from .ws import WebSocket

PROTOCOL_VERSION = 1


class Client:
    def __init__(self, host: str, port: int, hello: Callable[[], dict[str, Any]]) -> None:
        self.host = host
        self.port = port
        # Called on every (re)connect, so a newly saved file says hello with its own path.
        self._hello = hello
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._ws: WebSocket | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tencil-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def reconnect(self) -> None:
        """Drop the connection; the thread says hello again right away."""
        self._wake.set()
        if self._ws:
            self._ws.close()

    def send(self, message: dict[str, Any]) -> bool:
        ws = self._ws
        if not ws or not self.connected:
            return False
        try:
            ws.send_text(json.dumps(message))
            return True
        except OSError:
            return False

    def poll(self) -> list[dict[str, Any]]:
        messages = []
        while True:
            try:
                messages.append(self._inbox.get_nowait())
            except queue.Empty:
                return messages

    def _run(self) -> None:
        delay = 0.5
        while not self._stop.is_set():
            try:
                ws = WebSocket.connect(self.host, self.port, "/ws")
            except OSError as error:
                self._set_state(False, f"Tencil isn't running on {self.host}:{self.port} ({error.strerror or error})")
                self._sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            except Exception as error:
                self._set_state(False, str(error))
                self._sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            delay = 0.5
            self._ws = ws
            self._set_state(True, None)
            try:
                ws.send_text(json.dumps(self._hello()))
                while True:
                    text = ws.recv()
                    if text is None:
                        break
                    try:
                        self._inbox.put(json.loads(text))
                    except ValueError:
                        pass
            except OSError:
                pass
            finally:
                ws.close()
                self._ws = None
                self._set_state(False, "Disconnected")
            self._sleep(0.3)

    def _set_state(self, connected: bool, error: str | None) -> None:
        changed = connected != self.connected or error != self.error
        self.connected = connected
        self.error = error
        if changed:
            self._inbox.put({"type": "_connection", "connected": connected, "error": error})

    def _sleep(self, seconds: float) -> None:
        # `reconnect` and `stop` set the event to cut the wait short.
        self._wake.wait(seconds)
        self._wake.clear()
