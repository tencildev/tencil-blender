"""Tencil for Blender: point at an object, say what should change, and your coding
agent does it in the open scene (through Blender Lab's MCP server)."""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent

from . import markers, picker, ui
from .state import DEFAULT_PORT, session

TICK_SECONDS = 0.2

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
    if session.tick():
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
            # The status bar is a global area, outside screen.areas. Clearing the status
            # text redraws it, but a running modal tool keeps its hints there, so not then.
            if not window.modal_operators:
                window.workspace.status_text_set_internal(None)
    return TICK_SECONDS


@persistent
def _on_file_change(*_args: object) -> None:
    if session.refresh_file():
        session.reconnect()


def _start() -> None:
    session.start(_port())


classes = (TencilPreferences, picker.TENCIL_OT_pick, *ui.classes)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.tencil_show_resolved = bpy.props.BoolProperty(
        name="Show resolved pins", default=False
    )
    bpy.types.VIEW3D_MT_object_context_menu.append(ui.menu_entry)
    bpy.types.VIEW3D_MT_object.append(ui.menu_entry)
    bpy.types.STATUSBAR_HT_header.append(ui.draw_statusbar)
    markers.register()

    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
        item = keymap.keymap_items.new("tencil.pick", "T", "PRESS", alt=True)
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
    bpy.types.STATUSBAR_HT_header.remove(ui.draw_statusbar)
    bpy.types.VIEW3D_MT_object.remove(ui.menu_entry)
    bpy.types.VIEW3D_MT_object_context_menu.remove(ui.menu_entry)
    del bpy.types.WindowManager.tencil_show_resolved
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
