"""Tencil's picker in the 3D viewport: hover outlines the object under the cursor,
click picks it and asks what should change. Shift-click picks several."""

from __future__ import annotations

from typing import Any

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from .state import session

ACCENT = (0.36, 0.55, 1.0, 1.0)
ACCENT_SOFT = (0.36, 0.55, 1.0, 0.35)
# Objects without faces (cameras, lights, empties) can't be ray cast; they're hit near their origin.
ORIGIN_HIT_RADIUS = 24

_BOX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))

# Navigation keeps working while picking.
_PASS_THROUGH = {
    "MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
    "TRACKPADPAN", "TRACKPADZOOM", "MOUSEROTATE", "MOUSESMARTZOOM",
    "NDOF_MOTION", "NUMPAD_0", "NUMPAD_1", "NUMPAD_2", "NUMPAD_3", "NUMPAD_4", "NUMPAD_5",
    "NUMPAD_6", "NUMPAD_7", "NUMPAD_8", "NUMPAD_9", "NUMPAD_PERIOD",
}


def window_region(area: bpy.types.Area) -> bpy.types.Region | None:
    return next((r for r in area.regions if r.type == "WINDOW"), None)


def hit_test(
    context: bpy.types.Context, region: bpy.types.Region, rv3d: Any, coord: tuple[float, float]
) -> tuple[bpy.types.Object | None, Vector | None]:
    """The object under `coord` (region space) and the hit point in its local space."""
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    depsgraph = context.evaluated_depsgraph_get()
    hit, location, _normal, _index, obj, _matrix = context.scene.ray_cast(depsgraph, origin, direction)
    hit_obj = obj.original if hit and obj else None
    hit_distance = (location - origin).length if hit_obj else float("inf")

    # Cameras, lights, empties: nearest origin on screen, if it's in front of the surface hit.
    best, best_screen = None, ORIGIN_HIT_RADIUS
    for candidate in context.visible_objects:
        if candidate.type == "MESH":
            continue
        world = candidate.matrix_world.translation
        screen = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
        if screen is None:
            continue
        distance = (screen - Vector(coord)).length
        if distance < best_screen and (world - origin).length < hit_distance:
            best, best_screen = candidate, distance
    if best:
        return best, Vector((0.0, 0.0, 0.0))
    if hit_obj:
        return hit_obj, hit_obj.matrix_world.inverted() @ location
    return None, None


def draw_box(obj: bpy.types.Object, color: tuple[float, float, float, float], width: float) -> None:
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    if max((c - corners[0]).length for c in corners) < 1e-4:
        # Empties and lights have no bounds; draw a small cross at the origin.
        center, size = obj.matrix_world.translation, 0.25
        lines = [center + Vector(axis) * s for axis in ((size, 0, 0), (0, size, 0), (0, 0, size)) for s in (1, -1)]
    else:
        lines = [corners[i] for edge in _BOX_EDGES for i in edge]
    shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": lines})
    viewport = gpu.state.viewport_get()
    shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
    shader.uniform_float("lineWidth", width)
    shader.uniform_float("color", color)
    gpu.state.blend_set("ALPHA")
    batch.draw(shader)
    gpu.state.blend_set("NONE")


class TENCIL_OT_pick(bpy.types.Operator):
    """Point at an object and tell Tencil what should change"""

    bl_idname = "tencil.pick"
    bl_label = "Pick with Tencil"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        self.area = context.area
        self.region = window_region(context.area)
        if not self.region:
            return {"CANCELLED"}
        self.hovered: bpy.types.Object | None = None
        self.point: Vector | None = None
        self.picked: list[bpy.types.Object] = []
        self.mouse = (0.0, 0.0)
        self._view = bpy.types.SpaceView3D.draw_handler_add(self._draw_view, (), "WINDOW", "POST_VIEW")
        self._pixel = bpy.types.SpaceView3D.draw_handler_add(self._draw_pixel, (), "WINDOW", "POST_PIXEL")
        context.window.cursor_modal_set("EYEDROPPER")
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if event.type in _PASS_THROUGH:
            return {"PASS_THROUGH"}
        if event.type == "MOUSEMOVE":
            self._update(context, event)
            return {"RUNNING_MODAL"}
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._finish(context)
            return {"CANCELLED"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            if not self._inside(event):
                # Clicking another editor ends picking instead of trapping the user.
                self._finish(context)
                return {"CANCELLED"}
            if not self.hovered:
                return {"RUNNING_MODAL"}
            if event.shift:
                if self.hovered in self.picked:
                    self.picked.remove(self.hovered)
                else:
                    self.picked.append(self.hovered)
                self.area.tag_redraw()
                return {"RUNNING_MODAL"}
            objects = [self.hovered] + [o for o in self.picked if o != self.hovered]
            point, click = self.point, self.mouse
            self._finish(context)
            _select(context, objects)
            session.pending = [o.name for o in objects]
            with context.temp_override(area=self.area, region=self.region):
                bpy.ops.tencil.ask(
                    "INVOKE_DEFAULT",
                    has_point=point is not None,
                    point=tuple(point) if point is not None else (0.0, 0.0, 0.0),
                    click=click,
                )
            return {"FINISHED"}
        if event.type == "RET" and event.value == "PRESS" and self.picked:
            objects = list(self.picked)
            self._finish(context)
            _select(context, objects)
            session.pending = [o.name for o in objects]
            with context.temp_override(area=self.area, region=self.region):
                bpy.ops.tencil.ask("INVOKE_DEFAULT")
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def _inside(self, event: bpy.types.Event) -> bool:
        r = self.region
        return r.x <= event.mouse_x < r.x + r.width and r.y <= event.mouse_y < r.y + r.height

    def _update(self, context: bpy.types.Context, event: bpy.types.Event) -> None:
        self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
        hovered, point = None, None
        rv3d = self.region.data
        if self._inside(event) and rv3d:
            hovered, point = hit_test(context, self.region, rv3d, self.mouse)
        if hovered != self.hovered or point != self.point:
            self.hovered, self.point = hovered, point
        self.area.tag_redraw()

    def _finish(self, context: bpy.types.Context) -> None:
        bpy.types.SpaceView3D.draw_handler_remove(self._view, "WINDOW")
        bpy.types.SpaceView3D.draw_handler_remove(self._pixel, "WINDOW")
        context.window.cursor_modal_restore()
        self.area.tag_redraw()

    def _draw_view(self) -> None:
        if bpy.context.area != self.area:
            return
        for obj in self.picked:
            draw_box(obj, ACCENT, 3.0)
        if self.hovered and self.hovered not in self.picked:
            draw_box(self.hovered, ACCENT_SOFT if self.picked else ACCENT, 2.0)

    def _draw_pixel(self) -> None:
        if bpy.context.area != self.area:
            return
        font, scale = 0, bpy.context.preferences.system.ui_scale
        if self.hovered:
            blf.size(font, 13 * scale)
            label = self.hovered.name
            width, height = blf.dimensions(font, label)
            x, y = self.mouse[0] + 16 * scale, self.mouse[1] - 22 * scale
            _rect(x - 6 * scale, y - 5 * scale, width + 12 * scale, height + 10 * scale, (0.1, 0.1, 0.12, 0.9))
            blf.color(font, 1, 1, 1, 1)
            blf.position(font, x, y, 0)
            blf.draw(font, label)
        hint = (
            f"{len(self.picked)} picked · Click to ask · Enter to ask about these · Esc to cancel"
            if self.picked
            else "Click an object to ask Tencil · Shift-click to pick several · Esc to cancel"
        )
        blf.size(font, 12 * scale)
        width, height = blf.dimensions(font, hint)
        x = (self.region.width - width) / 2
        y = 24 * scale
        _rect(x - 10 * scale, y - 7 * scale, width + 20 * scale, height + 14 * scale, (0.1, 0.1, 0.12, 0.85))
        blf.color(font, 1, 1, 1, 0.9)
        blf.position(font, x, y, 0)
        blf.draw(font, hint)


def _select(context: bpy.types.Context, objects: list[bpy.types.Object]) -> None:
    """Make the picked objects Blender's selection, so the result reads the same either way."""
    if context.mode != "OBJECT":
        return
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in objects:
        if obj.visible_get():
            obj.select_set(True)
    context.view_layer.objects.active = objects[0]


def _rect(x: float, y: float, width: float, height: float, color: tuple[float, float, float, float]) -> None:
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    vertices = ((x, y), (x + width, y), (x + width, y + height), (x, y + height))
    batch = batch_for_shader(shader, "TRIS", {"pos": vertices}, indices=((0, 1, 2), (0, 2, 3)))
    shader.uniform_float("color", color)
    gpu.state.blend_set("ALPHA")
    batch.draw(shader)
    gpu.state.blend_set("NONE")
