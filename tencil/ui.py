"""Operators, the sidebar panel and menu entries."""

from __future__ import annotations

import json
import os
import textwrap
from typing import Any

import bpy
from mathutils import Vector

from . import context as tencil_context
from . import logo, state
from .state import session

# What the ask dialog is about, from opening it until Send.
_capture: dict[str, Any] = {}

MCP_SERVER = {
    "command": "uvx",
    "args": ["--from", "git+https://projects.blender.org/lab/blender_mcp.git#subdirectory=mcp", "blender-mcp"],
}

STATUS_ICONS = {
    "draft": "GREASEPENCIL",
    "starting": "SORTTIME",
    "working": "SORTTIME",
    "waiting": "QUESTION",
    "idle": "CHECKMARK",
    "error": "ERROR",
}


def _redraw_all() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _not_ready_reason() -> str:
    if not session.connected:
        return session.error or "Tencil isn't running (run `tencil start`)"
    return "Still connecting to the project"


class TENCIL_OT_ask(bpy.types.Operator):
    """Tell Tencil what should change about the selected objects"""

    bl_idname = "tencil.ask"
    bl_label = "Ask Tencil"
    bl_options = {"REGISTER"}

    prompt: bpy.props.StringProperty(name="Prompt", options={"SKIP_SAVE"})  # type: ignore[valid-type]
    has_point: bpy.props.BoolProperty(options={"HIDDEN", "SKIP_SAVE"})  # type: ignore[valid-type]
    point: bpy.props.FloatVectorProperty(size=3, options={"HIDDEN", "SKIP_SAVE"})  # type: ignore[valid-type]
    click: bpy.props.FloatVectorProperty(size=2, default=(-1, -1), options={"HIDDEN", "SKIP_SAVE"})  # type: ignore[valid-type]

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        # Only open the dialog here: anything that redraws the window (like taking a
        # screenshot) can close it again before it shows. The screenshot happens on Send.
        objects = _targets(context)
        if not objects:
            self.report({"WARNING"}, "Select an object first, or pick one with Tencil")
            return {"CANCELLED"}
        if not session.ready:
            self.report({"ERROR"}, _not_ready_reason())
            return {"CANCELLED"}
        area = context.area if context.area and context.area.type == "VIEW_3D" else _find_view3d(context)
        _capture.clear()
        _capture.update(objects=[o.name for o in objects], area=area)
        names = ", ".join(o.name for o in objects[:3]) + (f" +{len(objects) - 3}" if len(objects) > 3 else "")
        return context.window_manager.invoke_props_dialog(
            self, width=460, title=f"Ask Tencil about {names}", confirm_text="Send"
        )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.activate_init = True
        layout.prop(self, "prompt", text="", placeholder="What should change?")
        problem = session.agent_problem()
        if problem:
            for index, line in enumerate(_wrap(problem, 70)):
                layout.label(text=line, icon="ERROR" if index == 0 else "BLANK1")

    def execute(self, context: bpy.types.Context) -> set[str]:
        prompt = self.prompt.strip()
        if not prompt:
            self.report({"WARNING"}, "Type what should change")
            return {"CANCELLED"}
        objects = [bpy.data.objects[n] for n in _capture.get("objects", []) if n in bpy.data.objects]
        if not objects:
            objects = _targets(context)
        if not objects:
            self.report({"WARNING"}, "The object is gone")
            return {"CANCELLED"}
        point = Vector(self.point) if self.has_point else None
        click = tuple(self.click) if self.click[0] >= 0 else None
        area = _capture.get("area") or _find_view3d(context)
        region = next((r for r in area.regions if r.type == "WINDOW"), None) if area else None
        element, anchor = tencil_context.build(objects, region, region.data if region else None, point, click)
        screenshot = tencil_context.screenshot(context, area)
        thread_id = session.create_thread(
            prompt, element, anchor, [screenshot] if screenshot else [], tuple(point) if point else None
        )
        _capture.clear()
        if not thread_id:
            self.report({"ERROR"}, _not_ready_reason())
            return {"CANCELLED"}
        self.report({"INFO"}, "Sent to Tencil")
        _redraw_all()
        return {"FINISHED"}


class TENCIL_OT_reply(bpy.types.Operator):
    """Reply in this thread"""

    bl_idname = "tencil.reply"
    bl_label = "Reply"

    thread_id: bpy.props.StringProperty(options={"HIDDEN"})  # type: ignore[valid-type]
    text: bpy.props.StringProperty(name="Reply", options={"SKIP_SAVE"})  # type: ignore[valid-type]

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        return context.window_manager.invoke_props_dialog(self, width=460, confirm_text="Send")

    def draw(self, context: bpy.types.Context) -> None:
        self.layout.activate_init = True
        self.layout.prop(self, "text", text="", placeholder="Reply…")

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self.text.strip():
            return {"CANCELLED"}
        if not session.send(
            {"type": "thread.reply", "threadId": self.thread_id, "text": self.text.strip(), "attachments": []}
        ):
            self.report({"ERROR"}, _not_ready_reason())
            return {"CANCELLED"}
        return {"FINISHED"}


class TENCIL_OT_thread_action(bpy.types.Operator):
    """Resolve, reopen, stop or delete a thread"""

    bl_idname = "tencil.thread_action"
    bl_label = "Thread action"

    thread_id: bpy.props.StringProperty(options={"HIDDEN"})  # type: ignore[valid-type]
    action: bpy.props.EnumProperty(  # type: ignore[valid-type]
        items=(
            ("RESOLVE", "Resolve", ""),
            ("REOPEN", "Reopen", ""),
            ("STOP", "Stop", ""),
            ("DELETE", "Delete", ""),
        )
    )

    @classmethod
    def description(cls, context: bpy.types.Context, properties: Any) -> str:
        return {
            "RESOLVE": "Mark this pin as done",
            "REOPEN": "Reopen this pin",
            "STOP": "Stop the agent",
            "DELETE": "Delete this pin",
        }[properties.action]

    def execute(self, context: bpy.types.Context) -> set[str]:
        message = {
            "RESOLVE": {"type": "thread.resolve", "threadId": self.thread_id, "resolved": True},
            "REOPEN": {"type": "thread.resolve", "threadId": self.thread_id, "resolved": False},
            "STOP": {"type": "thread.interrupt", "threadId": self.thread_id},
            "DELETE": {"type": "thread.delete", "threadId": self.thread_id},
        }[self.action]
        if not session.send(message):
            self.report({"ERROR"}, _not_ready_reason())
            return {"CANCELLED"}
        return {"FINISHED"}


class TENCIL_OT_focus(bpy.types.Operator):
    """Select the pinned object and frame it"""

    bl_idname = "tencil.focus"
    bl_label = "Show object"

    thread_id: bpy.props.StringProperty(options={"HIDDEN"})  # type: ignore[valid-type]

    def execute(self, context: bpy.types.Context) -> set[str]:
        thread = session.threads.get(self.thread_id)
        obj = tencil_context.object_from_path(thread["anchor"]["selector"]) if thread else None
        if not obj:
            self.report({"WARNING"}, "The object isn't in this file anymore")
            return {"CANCELLED"}
        if context.mode == "OBJECT":
            for other in context.selected_objects:
                other.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            area = _find_view3d(context)
            if area:
                region = next(r for r in area.regions if r.type == "WINDOW")
                with context.temp_override(area=area, region=region):
                    bpy.ops.view3d.view_selected()
        return {"FINISHED"}


class TENCIL_OT_open(bpy.types.Operator):
    """Open this thread in the agent's app"""

    bl_idname = "tencil.open"
    bl_label = "Open in agent"

    url: bpy.props.StringProperty(options={"HIDDEN"})  # type: ignore[valid-type]

    def execute(self, context: bpy.types.Context) -> set[str]:
        bpy.ops.wm.url_open(url=self.url)
        return {"FINISHED"}


class TENCIL_OT_reconnect(bpy.types.Operator):
    """Connect to the Tencil daemon again"""

    bl_idname = "tencil.reconnect"
    bl_label = "Reconnect"

    def execute(self, context: bpy.types.Context) -> set[str]:
        session.refresh_file()
        session.reconnect()
        return {"FINISHED"}


class TENCIL_OT_use_claude_code(bpy.types.Operator):
    """Send this project's new pins to Claude Code, which can use Blender's MCP tools"""

    bl_idname = "tencil.use_claude_code"
    bl_label = "Use Claude Code"

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not session.send({"type": "settings.update", "provider": "claude-code"}):
            self.report({"ERROR"}, _not_ready_reason())
            return {"CANCELLED"}
        return {"FINISHED"}


class TENCIL_OT_setup_mcp(bpy.types.Operator):
    """Add Blender's MCP server to this project's .mcp.json and approve it for Claude Code, so the agent can work in the open scene"""

    bl_idname = "tencil.setup_mcp"
    bl_label = "Add Blender MCP to .mcp.json"

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not session.root:
            self.report({"WARNING"}, "Save the file first")
            return {"CANCELLED"}
        path = os.path.join(session.root, ".mcp.json")
        config: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as file:
                    config = json.load(file)
            except (OSError, ValueError) as error:
                self.report({"ERROR"}, f"Couldn't read {path}: {error}")
                return {"CANCELLED"}
        config.setdefault("mcpServers", {})["blender"] = MCP_SERVER
        _write_json(path, config)
        # Claude Code only starts a project's .mcp.json servers once they're approved.
        settings_path = os.path.join(session.root, ".claude", "settings.json")
        settings: dict[str, Any] = {}
        try:
            with open(settings_path, encoding="utf-8") as file:
                settings = json.load(file)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            self.report({"WARNING"}, f"Wrote {path}, but couldn't read {settings_path}: {error}")
            return {"FINISHED"}
        approved = settings.setdefault("enabledMcpjsonServers", [])
        if "blender" not in approved and not settings.get("enableAllProjectMcpServers"):
            approved.append("blender")
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            _write_json(settings_path, settings)
        self.report({"INFO"}, f"Wrote {path}")
        return {"FINISHED"}


def _write_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
        file.write("\n")


class TENCIL_PT_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Tencil"
    bl_label = "Tencil"

    def draw_header(self, context: bpy.types.Context) -> None:
        self.layout.label(text="", icon_value=logo.icon())

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        _draw_status(layout)

        column = layout.column(align=True)
        column.scale_y = 1.3
        column.enabled = session.ready
        column.operator_context = "INVOKE_DEFAULT"
        column.operator("tencil.pick", icon="EYEDROPPER")
        column.operator("tencil.ask", text="Ask About Selection", icon="OUTLINER_OB_MESH")

        _draw_setup(layout)


class TENCIL_PT_working(bpy.types.Panel):
    """What the agent is doing right now, step by step"""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Tencil"
    bl_label = "Working On"
    bl_parent_id = "TENCIL_PT_panel"

    def draw_header(self, context: bpy.types.Context) -> None:
        active = session.active_threads()
        self.layout.label(text="", icon="SORTTIME" if active else "CHECKMARK")

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        active = session.active_threads()
        if not active:
            layout.label(text="Nothing right now")
            return
        width = _text_width(context)
        for thread in active:
            _draw_activity(layout, thread, width)


class TENCIL_PT_pins(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Tencil"
    bl_label = "Pins"
    bl_parent_id = "TENCIL_PT_panel"

    def draw_header(self, context: bpy.types.Context) -> None:
        count = len(session.file_threads())
        if count:
            self.layout.label(text=str(count))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        numbered = session.numbered()
        wm = context.window_manager
        visible = [(n, t) for n, t in numbered if wm.tencil_show_resolved or not t.get("resolved")]
        if not numbered:
            layout.label(text="No pins in this file yet")
            return
        if any(t.get("resolved") for _, t in numbered):
            layout.prop(wm, "tencil_show_resolved", text="Show Resolved", toggle=True)
        width = _text_width(context)
        for number, thread in reversed(visible):
            _draw_thread(layout, number, thread, width)


STEP_ICONS = {"running": "PLAY", "done": "CHECKMARK", "failed": "ERROR", "skipped": "PANEL_CLOSE"}
MAX_STEPS_SHOWN = 6


def _draw_activity(layout: bpy.types.UILayout, thread: dict[str, Any], width: int) -> None:
    """One thread the agent is on: what it's doing, the last steps, and what it's saying."""
    box = layout.box()
    status = thread.get("status", "working")
    number = session.number_of(thread["id"])
    head = box.row()
    title = thread.get("title", "")
    head.label(text=f"{number}  {title}" if number else title, icon=STATUS_ICONS.get(status, "SORTTIME"))
    if number:
        head.operator("tencil.focus", text="", icon="RESTRICT_SELECT_OFF", emboss=False).thread_id = thread["id"]

    meta = box.row()
    meta.scale_y = 0.7
    file = state.file_label(thread)
    since = state.elapsed(state.turn_started(thread))
    where = "" if number or not file else f" · {file}"
    meta.label(
        text={"starting": "Starting", "waiting": "Waiting for you"}.get(status, "Working") + (f" · {since}" if since else "") + where
    )
    if status == "waiting" or thread.get("statusDetail"):
        for line in _wrap(thread.get("statusDetail") or "Needs your input in the agent's app", width)[:3]:
            box.label(text=line, icon="INFO" if status != "waiting" else "QUESTION")

    steps = thread.get("steps") or []
    if steps:
        column = box.column(align=True)
        children: dict[str, list[dict[str, Any]]] = {}
        for step in steps:
            if step.get("parentId"):
                children.setdefault(step["parentId"], []).append(step)
        top = [s for s in steps if not s.get("parentId")]
        hidden = max(len(top) - MAX_STEPS_SHOWN, 0)
        if hidden:
            column.label(text=f"{hidden} earlier step{'s' if hidden != 1 else ''}")
        for step in top[-MAX_STEPS_SHOWN:]:
            _draw_step(column, step, width, 0)
            # A subagent's steps show while it runs.
            if step.get("status") == "running":
                for child in children.get(step["id"], [])[-3:]:
                    _draw_step(column, child, width, 1)
    elif status in ("starting", "working"):
        box.label(text="Thinking…")

    reply = next((m for m in reversed(thread.get("messages", [])) if m.get("role") == "assistant"), None)
    turn = state.turn_started(thread) or ""
    if reply and reply.get("text") and (reply.get("streaming") or reply.get("createdAt", "") >= turn):
        lines = _wrap(reply["text"].strip(), width)
        text = box.column(align=True)
        text.scale_y = 0.8
        for line in lines[-4:]:
            text.label(text=line)

    actions = box.row(align=True)
    stop = actions.operator("tencil.thread_action", text="Stop", icon="CANCEL")
    stop.thread_id, stop.action = thread["id"], "STOP"
    url = (thread.get("provider") or {}).get("url")
    if url:
        actions.operator("tencil.open", text="Open", icon="URL").url = url


def _draw_step(layout: bpy.types.UILayout, step: dict[str, Any], width: int, depth: int) -> None:
    row = layout.row(align=True)
    if depth:
        row.separator(factor=2.0 * depth)
    label = step.get("label", "")
    detail = step.get("detail")
    room = width - 4 - 3 * depth
    text = f"{label}: {detail}" if detail else label
    row.label(text=text if len(text) <= room else text[: max(room - 1, 8)] + "…", icon=STEP_ICONS.get(step.get("status"), "DOT"))


def _text_width(context: bpy.types.Context) -> int:
    """Characters that fit on a sidebar line, roughly."""
    return max(int(context.region.width / (6.5 * context.preferences.system.ui_scale)) - 6, 20)


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        lines.extend(textwrap.wrap(paragraph, width) or [""])
    return lines


def draw_statusbar(self: Any, context: bpy.types.Context) -> None:
    """Tencil in Blender's status bar while the agent works, so it's visible from any tab."""
    active = session.active_threads()
    if not session.ready or not active:
        return
    thread = active[-1]
    step = state.current_step(thread) if thread.get("status") == "working" else None
    if thread.get("status") == "waiting":
        text, icon = "Tencil is waiting for you", "QUESTION"
    else:
        what = step.get("label") if step and step.get("status") == "running" else "working"
        text, icon = f"Tencil: {what}", None
    if len(active) > 1:
        text += f" (+{len(active) - 1})"
    since = state.elapsed(state.turn_started(thread))
    text = f"{text} · {since}" if since else text
    if icon:
        self.layout.label(text=text, icon=icon)
    else:
        self.layout.label(text=text, icon_value=logo.icon())


def _draw_status(layout: bpy.types.UILayout) -> None:
    box = layout.box()
    if not session.connected:
        box.label(text="Not connected", icon="UNLINKED")
        for line in textwrap.wrap(session.error or "Run `tencil start` in a terminal.", 40):
            box.label(text=line)
        box.operator("tencil.reconnect", icon="FILE_REFRESH")
        return
    if not session.ready:
        if not session.root:
            box.label(text="Save the file in a project folder", icon="FILE_TICK")
            for line in textwrap.wrap("Tencil works in the folder (or git repository) the .blend is in.", 40):
                box.label(text=line)
            return
        box.label(text="Connecting to the project…", icon="SORTTIME")
        return
    project = session.project or {}
    box.label(text=project.get("name") or "Project", icon="FILE_FOLDER")
    provider = session.provider or {}
    state = provider.get("state")
    label = provider.get("label", "Agent")
    if provider.get("target"):
        label += f" · {provider['target']}"
    box.label(text=label, icon="CHECKMARK" if state == "ready" else "ERROR")
    if state != "ready" and provider.get("detail"):
        for line in textwrap.wrap(provider["detail"], 40):
            box.label(text=line)
    if session.last_error:
        box.label(text=session.last_error, icon="ERROR")
    draw_agent_problem(layout, 40)


def draw_agent_problem(layout: bpy.types.UILayout, width: int) -> None:
    """When the project's agent can't change the scene: why, and the fix."""
    problem = session.agent_problem()
    if not problem:
        return
    box = layout.box()
    text = box.column(align=True)
    text.alert = True
    for index, line in enumerate(_wrap(problem, width)):
        text.label(text=line, icon="ERROR" if index == 0 else "BLANK1")
    if session.can_use_claude_code():
        box.operator("tencil.use_claude_code", icon_value=logo.icon())


def _draw_setup(layout: bpy.types.UILayout) -> None:
    """What the agent needs to change the scene: Blender Lab's MCP add-on, and the server in .mcp.json."""
    # Installed from the Blender Lab repository it's `bl_ext.blender_lab.mcp`.
    has_addon = any(name.rsplit(".", 1)[-1] in ("mcp", "blender_mcp") for name in bpy.context.preferences.addons.keys())
    has_config = bool(session.root) and _mcp_configured(session.root)
    if has_addon and has_config:
        return
    box = layout.box()
    box.label(text="Let the agent change the scene", icon="INFO")
    if not has_addon:
        for line in textwrap.wrap("Install the MCP Server add-on from the Blender Lab extensions repository and start it.", 40):
            box.label(text=line)
        box.operator("wm.url_open", text="Blender MCP Server").url = "https://www.blender.org/lab/mcp-server/"
    if session.root and not has_config:
        box.operator("tencil.setup_mcp", icon="ADD")


def _mcp_configured(root: str) -> bool:
    try:
        with open(os.path.join(root, ".mcp.json"), encoding="utf-8") as file:
            return "blender" in json.load(file).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        return False


def _draw_thread(layout: bpy.types.UILayout, number: int, thread: dict[str, Any], width: int) -> None:
    box = layout.box()
    status = thread.get("status", "idle")
    head = box.row()
    head.label(text=f"{number}  {thread.get('title', '')}", icon=STATUS_ICONS.get(status, "DOT"))
    head.operator("tencil.focus", text="", icon="RESTRICT_SELECT_OFF", emboss=False).thread_id = thread["id"]

    body = box.column(align=True)
    body.scale_y = 0.8
    detail = thread.get("statusDetail")
    if status in ("starting", "working"):
        step = state.current_step(thread)
        body.label(text=(step or {}).get("label") or detail or "Working…", icon="SORTTIME")
    elif status == "error":
        body.label(text=detail or "Failed", icon="ERROR")
    elif status == "waiting":
        body.label(text=detail or "Needs your input in the agent's app")
    reply = next((m for m in reversed(thread.get("messages", [])) if m.get("role") == "assistant"), None)
    if reply and reply.get("text") and status not in ("starting", "working"):
        lines = _wrap(reply["text"].strip(), width)
        for line in lines[:8]:
            body.label(text=line)
        if len(lines) > 8:
            body.label(text="…")

    actions = box.row(align=True)
    thread_id = thread["id"]
    actions.operator("tencil.reply", text="Reply", icon="FORWARD").thread_id = thread_id
    if status in ("starting", "working"):
        stop = actions.operator("tencil.thread_action", text="", icon="CANCEL")
        stop.thread_id, stop.action = thread_id, "STOP"
    resolve = actions.operator(
        "tencil.thread_action", text="", icon="LOOP_BACK" if thread.get("resolved") else "CHECKMARK"
    )
    resolve.thread_id, resolve.action = thread_id, "REOPEN" if thread.get("resolved") else "RESOLVE"
    url = (thread.get("provider") or {}).get("url")
    if url:
        actions.operator("tencil.open", text="", icon="URL").url = url
    delete = actions.operator("tencil.thread_action", text="", icon="TRASH")
    delete.thread_id, delete.action = thread_id, "DELETE"


def _targets(context: bpy.types.Context) -> list[bpy.types.Object]:
    """The picker's objects if it just ran, otherwise the selection with the active object first."""
    if session.pending:
        objects = [bpy.data.objects[n] for n in session.pending if n in bpy.data.objects]
        session.pending = []
        if objects:
            return objects
    selected = list(context.selected_objects or [])
    active = context.active_object
    if active and active in selected:
        selected.remove(active)
        return [active, *selected]
    return selected


def _find_view3d(context: bpy.types.Context) -> bpy.types.Area | None:
    screen = context.window.screen if context.window else None
    if not screen:
        return None
    areas = [a for a in screen.areas if a.type == "VIEW_3D"]
    return max(areas, key=lambda a: a.width * a.height) if areas else None


def menu_entry(self: Any, context: bpy.types.Context) -> None:
    layout = self.layout
    layout.separator()
    # The object menus end in EXEC_REGION_WIN, which would skip the dialog (and the picker).
    layout.operator_context = "INVOKE_REGION_WIN"
    layout.operator("tencil.ask", icon_value=logo.icon())
    layout.operator("tencil.pick", icon="EYEDROPPER")


classes = (
    TENCIL_OT_ask,
    TENCIL_OT_reply,
    TENCIL_OT_thread_action,
    TENCIL_OT_focus,
    TENCIL_OT_open,
    TENCIL_OT_reconnect,
    TENCIL_OT_use_claude_code,
    TENCIL_OT_setup_mcp,
    TENCIL_PT_panel,
    TENCIL_PT_working,
    TENCIL_PT_pins,
)
