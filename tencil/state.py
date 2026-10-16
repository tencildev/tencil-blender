"""Session state on Blender's main thread: the daemon connection, the project
this file belongs to, and the pins (threads) made from it."""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import bpy

from .client import PROTOCOL_VERSION, Client

ACTIVE = ("starting", "working", "waiting")

HOST = "localhost"
# Like Tencil's Vite plugin, TENCIL_PORT points at a daemon on another port.
DEFAULT_PORT = int(os.environ.get("TENCIL_PORT") or 4747)


class Session:
    def __init__(self) -> None:
        self.client: Client | None = None
        self.connected = False
        self.error: str | None = None
        self.ready = False
        self.project: dict[str, Any] | None = None
        self.provider: dict[str, Any] | None = None
        self.threads: dict[str, dict[str, Any]] = {}
        # Where on the object each pin was clicked (object space), for the viewport marker.
        # The daemon doesn't store it, so pins from an earlier session sit on top of the object.
        self.points: dict[str, tuple[float, float, float]] = {}
        self.last_error: str | None = None
        # Objects the picker handed to the ask dialog, by name; otherwise it uses the selection.
        self.pending: list[str] = []
        # Read on the client thread when it says hello, so they're kept here, not read from bpy there.
        self.page_url = page_url_for("", None)
        self.root: str | None = None
        # One project.select per connection, so a daemon that disagrees can't make us loop.
        self._selected_root = False
        # While the agent works, the pulse and elapsed times move even when no message arrives.
        self._last_live_redraw = 0.0

    # ------------------------------------------------------------------ lifecycle

    def start(self, port: int) -> None:
        self.refresh_file()
        if self.client and self.client.port == port:
            self.client.start()
            return
        self.stop()
        self.client = Client(HOST, port, self._hello)
        self.client.start()

    def stop(self) -> None:
        if self.client:
            self.client.stop()
        self.client = None
        self._reset()
        self.connected = False

    def refresh_file(self) -> bool:
        """Pick up the open file's path; True when it changed (the connection must say hello again)."""
        path = bpy.data.filepath
        root = project_root(path) if path else None
        page_url = page_url_for(path, root)
        changed = page_url != self.page_url or root != self.root
        self.page_url, self.root = page_url, root
        return changed

    def reconnect(self) -> None:
        self._reset()
        if self.client:
            self.client.reconnect()

    def _reset(self) -> None:
        self.ready = False
        self.project = None
        self.threads = {}
        self._selected_root = False

    def _hello(self) -> dict[str, Any]:
        message: dict[str, Any] = {"type": "hello", "protocol": PROTOCOL_VERSION, "pageUrl": self.page_url}
        # An unsaved file has no folder: the daemon runs the agent in its scratch folder.
        if not self.root:
            message["scratch"] = True
        return message

    # ------------------------------------------------------------------ inbound

    def tick(self) -> bool:
        """Apply queued messages; True when something changed and the UI should redraw."""
        if not self.client:
            return False
        messages = self.client.poll()
        for message in messages:
            self._handle(message)
        now = time.monotonic()
        # 4 a second keeps the pin's pulse moving without busying Blender.
        if self.active_threads() and now - self._last_live_redraw >= 0.25:
            self._last_live_redraw = now
            return True
        return bool(messages)

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "_connection":
            self.connected = bool(message.get("connected"))
            self.error = message.get("error")
            if not self.connected:
                self._reset()
            else:
                self._selected_root = False
        elif kind == "ready":
            project = message.get("project") or {}
            # The daemon may remember another folder for this address; the file's own folder wins.
            if self.root and _norm(project.get("root")) != _norm(self.root) and not self._selected_root:
                self._select_root()
                return
            self.ready = True
            self.project = project
            self.provider = message.get("provider")
            self.threads = {t["id"]: t for t in message.get("threads", [])}
        elif kind == "project.unknown":
            if self.root and not self._selected_root:
                self._select_root()
            else:
                self.last_error = "Save the file to connect it to a project"
        elif kind == "provider.status":
            self.provider = message.get("provider")
        elif kind == "thread.upsert":
            thread = message["thread"]
            self.threads[thread["id"]] = thread
        elif kind == "thread.removed":
            self.threads.pop(message.get("threadId"), None)
        elif kind == "error":
            self.last_error = message.get("message")

    def _select_root(self) -> None:
        self._selected_root = True
        self.client and self.client.send({"type": "project.select", "root": self.root})

    # ------------------------------------------------------------------ outbound

    def create_thread(
        self,
        prompt: str,
        context: dict[str, Any],
        anchor: dict[str, Any],
        attachments: list[dict[str, Any]],
        point: tuple[float, float, float] | None,
    ) -> str | None:
        if not self.ready or not self.client:
            return None
        thread_id = uuid.uuid4().hex[:16]
        sent = self.client.send(
            {
                "type": "thread.create",
                "id": thread_id,
                "pageUrl": self.page_url,
                "prompt": prompt,
                "anchor": anchor,
                "context": context,
                "attachments": attachments,
            }
        )
        if not sent:
            return None
        if point:
            self.points[thread_id] = point
        return thread_id

    def send(self, message: dict[str, Any]) -> bool:
        return bool(self.client and self.client.send(message))

    # ------------------------------------------------------------------ queries

    def file_threads(self, include_resolved: bool = False) -> list[dict[str, Any]]:
        """Pins made from this file, oldest first (the order they're numbered in)."""
        threads = [
            t for t in self.threads.values() if t.get("pageUrl") == self.page_url and t.get("anchor")
        ]
        threads.sort(key=lambda t: t.get("createdAt", ""))
        if not include_resolved:
            threads = [t for t in threads if not t.get("resolved")]
        return threads

    def numbered(self) -> list[tuple[int, dict[str, Any]]]:
        return list(enumerate(self.file_threads(include_resolved=True), start=1))

    def number_of(self, thread_id: str) -> int | None:
        return next((n for n, t in self.numbered() if t["id"] == thread_id), None)

    def active_threads(self) -> list[dict[str, Any]]:
        """What the agent is on right now, in this project (any file), oldest first."""
        threads = [t for t in self.threads.values() if t.get("status") in ACTIVE]
        threads.sort(key=lambda t: t.get("createdAt", ""))
        return threads


def current_step(thread: dict[str, Any]) -> dict[str, Any] | None:
    """The step the agent is running now (the innermost), else the last one."""
    steps = thread.get("steps") or []
    running = [s for s in steps if s.get("status") == "running"]
    return (running or steps or [None])[-1]


def turn_started(thread: dict[str, Any]) -> str | None:
    """When the current turn began: the last thing the user sent."""
    user = [m for m in thread.get("messages", []) if m.get("role") == "user"]
    return user[-1].get("createdAt") if user else thread.get("createdAt")


def file_label(thread: dict[str, Any]) -> str:
    """The file a pin was made from, as `scenes/x.blend`."""
    url = thread.get("pageUrl") or ""
    return unquote(url.split(".blender.invalid/", 1)[-1]) if ".blender.invalid/" in url else ""


def page_url_for(blend_path: str, root: str | None) -> str:
    """The address the daemon knows this file by.

    The daemon remembers a project per origin. blender:// and file:// URLs all have the
    same origin, "null", so one remembered folder would go to every file (and to local
    web pages). A made-up host per project keeps them apart. ws:// rather than http://,
    so the daemon doesn't look for a dev server on port 80: the first time it asks
    (project.unknown) and the add-on answers with the folder, which it then remembers.
    """
    if not blend_path or not root:
        return "ws://untitled.blender.invalid/"
    digest = hashlib.sha1(os.path.realpath(root).encode("utf-8")).hexdigest()[:10]
    name = re.sub(r"[^a-z0-9]+", "-", Path(root).name.lower()).strip("-")[:40] or "project"
    relative = Path(os.path.relpath(os.path.realpath(blend_path), os.path.realpath(root))).as_posix()
    return f"ws://{name}-{digest}.blender.invalid/{quote(relative)}"


def project_root(blend_path: str) -> str:
    """The git repository the file is in, or its folder."""
    folder = Path(blend_path).resolve().parent
    for candidate in (folder, *folder.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(folder)


def _norm(path: str | None) -> str | None:
    return os.path.realpath(path) if path else None


def elapsed(iso: str | None) -> str:
    """`42s`, `3m 05s`: how long the agent has been at it."""
    seconds = _seconds_since(iso)
    if seconds is None:
        return ""
    seconds = max(int(seconds), 0)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def _seconds_since(iso: str | None) -> float | None:
    try:
        then = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(then.tzinfo or timezone.utc) - then).total_seconds()


def relative_time(iso: str) -> str:
    seconds = _seconds_since(iso)
    if seconds is None:
        return ""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


session = Session()
