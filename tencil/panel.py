"""The Tencil panel: every thread in the project, and the one you open in full.

Opens from the Tencil button in the 3D view's header, or Alt+Shift+T.
"""

from __future__ import annotations

from typing import Any

import bpy

from . import state
from .state import ACTIVE, session
from .ui import STATUS_ICONS, _draw_step, _not_ready_reason, _wrap

PANEL_WIDTH = 30  # UI units
TEXT_WIDTH = 82  # characters that fit on a line at that width, roughly
MAX_MESSAGES = 12


class TENCIL_OT_select_thread(bpy.types.Operator):
    """Open this thread in the panel"""

    bl_idname = "tencil.select_thread"
    bl_label = "Open Thread"
    bl_options = {"INTERNAL"}

    thread_id: bpy.props.StringProperty(options={"HIDDEN"})  # type: ignore[valid-type]

    def execute(self, context: bpy.types.Context) -> set[str]:
        wm = context.window_manager
        wm.tencil_open_thread = "" if wm.tencil_open_thread == self.thread_id else self.thread_id
        return {"FINISHED"}


class TENCIL_PT_window(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "HEADER"
    bl_label = "Tencil"
    bl_ui_units_x = PANEL_WIDTH

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        wm = context.window_manager
        _draw_header(layout)
        if not session.ready:
            return

        actions = layout.row(align=True)
        actions.operator_context = "INVOKE_DEFAULT"
        actions.operator("tencil.pick", icon="EYEDROPPER")
        actions.operator("tencil.ask", text="Ask About Selection", icon="OUTLINER_OB_MESH")

        threads = _ordered(session.threads.values())
        resolved = [t for t in threads if t.get("resolved")]
        shown = threads if wm.tencil_show_resolved else [t for t in threads if not t.get("resolved")]

        row = layout.row()
        row.label(text=f"Threads ({len(shown)})" if shown else "No threads yet")
        if resolved:
            row.prop(wm, "tencil_show_resolved", text=f"Resolved ({len(resolved)})", toggle=True)

        column = layout.column(align=True)
        for thread in shown:
            _draw_row(column, thread, wm.tencil_open_thread == thread["id"])
            if wm.tencil_open_thread == thread["id"]:
                _draw_open(column, thread)


def _draw_header(layout: bpy.types.UILayout) -> None:
    box = layout.box()
    row = box.row()
    if not session.connected or not session.ready:
        row.label(text=_not_ready_reason(), icon="UNLINKED")
        row.operator("tencil.reconnect", text="", icon="FILE_REFRESH")
        return
    project = session.project or {}
    provider = session.provider or {}
    row.label(text=project.get("name") or "Project", icon="FILE_FOLDER")
    label = provider.get("label", "Agent")
    row.label(text=label, icon="CHECKMARK" if provider.get("state") == "ready" else "ERROR")
    active = session.active_threads()
    if active:
        box.label(text=f"Working on {len(active)}", icon="SORTTIME")


def _ordered(threads: Any) -> list[dict[str, Any]]:
    """Working first, then the rest by when they last changed."""
    threads = list(threads)
    active = sorted((t for t in threads if t.get("status") in ACTIVE), key=lambda t: t.get("createdAt", ""))
    rest = sorted(
        (t for t in threads if t.get("status") not in ACTIVE),
        key=lambda t: t.get("updatedAt", ""),
        reverse=True,
    )
    return active + rest


def _draw_row(layout: bpy.types.UILayout, thread: dict[str, Any], is_open: bool) -> None:
    status = thread.get("status", "idle")
    number = session.number_of(thread["id"])
    # Titles read left to right; buttons centre their text, so the row is split and left-aligned.
    split = layout.split(factor=0.72, align=True)
    left = split.row(align=True)
    left.alignment = "LEFT"
    title = thread.get("title", "")
    text = f"{number}  {title}" if number else title
    icon = "DISCLOSURE_TRI_DOWN" if is_open else "DISCLOSURE_TRI_RIGHT"
    open_ = left.operator("tencil.select_thread", text=_clip(text, 62), icon=icon, emboss=False)
    open_.thread_id = thread["id"]
    side = split.row(align=True)
    side.alignment = "RIGHT"
    if status in ACTIVE:
        step = state.current_step(thread) if status == "working" else None
        what = step["label"] if step and step.get("status") == "running" else status.capitalize()
        side.label(text=_clip(what, 28), icon=STATUS_ICONS.get(status, "SORTTIME"))
    else:
        where = "" if number else state.file_label(thread)
        when = state.relative_time(thread.get("updatedAt", ""))
        side.label(text=" · ".join(p for p in (_clip(where, 24), when) if p), icon=STATUS_ICONS.get(status, "DOT"))


def _draw_open(layout: bpy.types.UILayout, thread: dict[str, Any]) -> None:
    """The whole thread: conversation, what the agent is doing, and what you can do."""
    box = layout.box()
    status = thread.get("status", "idle")
    file = state.file_label(thread)
    in_this_file = session.number_of(thread["id"]) is not None
    meta = box.row()
    meta.scale_y = 0.8
    since = state.elapsed(state.turn_started(thread)) if status in ACTIVE else ""
    meta.label(
        text=" · ".join(
            p
            for p in (
                {"starting": "Starting", "working": "Working", "waiting": "Waiting for you", "error": "Failed"}.get(
                    status, "Done"
                ),
                since,
                file if not in_this_file else "",
                (thread.get("provider") or {}).get("id", ""),
            )
            if p
        )
    )

    messages = thread.get("messages", [])
    if len(messages) > MAX_MESSAGES:
        box.label(text=f"{len(messages) - MAX_MESSAGES} earlier messages", icon="THREE_DOTS")
    for message in messages[-MAX_MESSAGES:]:
        text = (message.get("text") or "").strip()
        if not text:
            continue
        you = message.get("role") == "user"
        column = box.column(align=True)
        column.label(text="You" if you else "Agent", icon="USER" if you else "LIGHT")
        lines = column.column(align=True)
        lines.scale_y = 0.8
        for line in _wrap(text, TEXT_WIDTH):
            lines.label(text=line)
        if message.get("streaming"):
            lines.label(text="…")

    if status in ACTIVE or thread.get("statusDetail"):
        if thread.get("statusDetail"):
            for line in _wrap(thread["statusDetail"], TEXT_WIDTH)[:3]:
                box.label(text=line, icon="ERROR" if status == "error" else "INFO")
    steps = thread.get("steps") or []
    if steps:
        column = box.column(align=True)
        column.label(text="Steps", icon="SORTTIME" if status in ACTIVE else "CHECKMARK")
        top = [s for s in steps if not s.get("parentId")]
        for step in top[-8:]:
            _draw_step(column, step, TEXT_WIDTH, 0)
            if step.get("status") == "running":
                for child in [s for s in steps if s.get("parentId") == step["id"]][-3:]:
                    _draw_step(column, child, TEXT_WIDTH, 1)

    thread_id = thread["id"]
    actions = box.row(align=True)
    actions.operator_context = "INVOKE_DEFAULT"
    actions.operator("tencil.reply", text="Reply", icon="FORWARD").thread_id = thread_id
    if status in ("starting", "working"):
        stop = actions.operator("tencil.thread_action", text="Stop", icon="CANCEL")
        stop.thread_id, stop.action = thread_id, "STOP"
    resolve = actions.operator(
        "tencil.thread_action",
        text="Reopen" if thread.get("resolved") else "Resolve",
        icon="LOOP_BACK" if thread.get("resolved") else "CHECKMARK",
    )
    resolve.thread_id, resolve.action = thread_id, "REOPEN" if thread.get("resolved") else "RESOLVE"
    if in_this_file:
        actions.operator("tencil.focus", text="Show", icon="RESTRICT_SELECT_OFF").thread_id = thread_id
    url = (thread.get("provider") or {}).get("url")
    if url:
        actions.operator("tencil.open", text="Open", icon="URL").url = url
    delete = actions.operator("tencil.thread_action", text="", icon="TRASH")
    delete.thread_id, delete.action = thread_id, "DELETE"


def draw_header_button(self: Any, context: bpy.types.Context) -> None:
    """Tencil in the 3D view's header: opens the panel, and says when the agent is busy."""
    active = session.active_threads() if session.ready else []
    if not session.connected:
        text, icon = "Tencil", "UNLINKED"
    elif active:
        text, icon = f"Tencil · {len(active)} working", "SORTTIME"
    else:
        count = len([t for t in session.threads.values() if not t.get("resolved")])
        text, icon = (f"Tencil · {count}" if count else "Tencil"), "OUTLINER_OB_LIGHT"
    self.layout.popover(panel="TENCIL_PT_window", text=text, icon=icon)


def _clip(text: str, size: int) -> str:
    return text if len(text) <= size else text[: size - 1] + "…"


classes = (TENCIL_OT_select_thread, TENCIL_PT_window)
