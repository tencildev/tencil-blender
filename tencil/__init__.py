"""Tencil for Blender: point at an object, say what should change, and your coding
agent does it in the open scene (through Blender Lab's MCP server)."""

from __future__ import annotations

import time

import bpy
from bpy.app.handlers import persistent

from . import logo, markers, panel, picker, ui
from .state import DEFAULT_PORT, LIVE_FRAME, session

TICK_SECONDS = 0.2
# The status bar shows elapsed seconds; it needn't redraw with every frame of the pin.
STATUS_SECONDS = 1.0

_last_status = 0.0

_keymaps: list[tuple[bpy.types.KeyMap, bpy.types.KeyMapItem]] = []


class TencilPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    port: bpy.props.IntProperty(  # type: ignore[valid-type]
        name="Daemon port",
        description="Port of the Tencil daemon (`tencil start`)",
        default=DEFAULT_PORT,
        min=1024,
        max=65535,
        update=lambda self, context: session.start(self.port),
    )

    def draw(self, context: bpy.types.Context) -> None:
        self.layout.prop(self, "port")


def _port() -> int:
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences.port if addon else DEFAULT_PORT


def _tick() -> float:
    global _last_status
    if session.tick():
        now = time.monotonic()
        status = now - _last_status >= STATUS_SECONDS
        if status:
            _last_status = now
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
            # The status bar is a global area, outside screen.areas. Clearing the status
            # text redraws it, but a running modal tool keeps its hints there, so not then.
            if status and not window.modal_operators:
                window.workspace.status_text_set_internal(None)
    return LIVE_FRAME if session.active_threads() else TICK_SECONDS


@persistent
def _on_file_change(*_args: object) -> None:
    if session.refresh_file():
        session.reconnect()


def _start() -> None:
    session.start(_port())


classes = (TencilPreferences, picker.TENCIL_OT_pick, *ui.classes, *panel.classes)


def register() -> None:
    logo.register()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.tencil_show_resolved = bpy.props.BoolProperty(
        name="Show resolved pins", default=False
    )
    bpy.types.WindowManager.tencil_open_thread = bpy.props.StringProperty(name="Open thread")
    bpy.types.VIEW3D_MT_object_context_menu.append(ui.menu_entry)
    bpy.types.VIEW3D_MT_object.append(ui.menu_entry)
    bpy.types.STATUSBAR_HT_header.append(ui.draw_statusbar)
    bpy.types.VIEW3D_HT_header.append(panel.draw_header_button)
    markers.register()

    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
        item = keymap.keymap_items.new("tencil.pick", "T", "PRESS", alt=True)
        _keymaps.append((keymap, item))
        # A click on a pin opens its thread; elsewhere the operator passes the click on.
        item = keymap.keymap_items.new("tencil.click_pin", "LEFTMOUSE", "PRESS")
        _keymaps.append((keymap, item))
        item = keymap.keymap_items.new("wm.call_panel", "T", "PRESS", alt=True, shift=True)
        item.properties.name = "TENCIL_PT_window"
        item.properties.keep_open = True
        _keymaps.append((keymap, item))

    bpy.app.handlers.load_post.append(_on_file_change)
    bpy.app.handlers.save_post.append(_on_file_change)
    bpy.app.timers.register(_tick, first_interval=TICK_SECONDS, persistent=True)
    # Preferences aren't readable while registering at startup; connect on the first tick.
    # Persistent, or opening a file from the command line drops it before it runs.
    bpy.app.timers.register(_start, first_interval=0.1, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.save_post):
        if _on_file_change in handlers:
            handlers.remove(_on_file_change)
    session.stop()
    for keymap, item in _keymaps:
        keymap.keymap_items.remove(item)
    _keymaps.clear()
    markers.unregister()
    bpy.types.VIEW3D_HT_header.remove(panel.draw_header_button)
    bpy.types.STATUSBAR_HT_header.remove(ui.draw_statusbar)
    bpy.types.VIEW3D_MT_object.remove(ui.menu_entry)
    bpy.types.VIEW3D_MT_object_context_menu.remove(ui.menu_entry)
    del bpy.types.WindowManager.tencil_show_resolved
    del bpy.types.WindowManager.tencil_open_thread
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    logo.unregister()
