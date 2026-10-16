"""Numbered pins in the 3D viewport, on the spot each object was picked."""

from __future__ import annotations

import math

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from .context import object_from_path
from .state import session

COLORS = {
    "draft": (0.55, 0.55, 0.6, 1.0),
    "starting": (0.36, 0.55, 1.0, 1.0),
    "working": (0.36, 0.55, 1.0, 1.0),
    "waiting": (0.95, 0.65, 0.2, 1.0),
    "idle": (0.25, 0.75, 0.45, 1.0),
    "error": (0.9, 0.3, 0.3, 1.0),
}
SEGMENTS = 24

_handle = None


def draw() -> None:
    context = bpy.context
    region, rv3d = context.region, context.region_data
    if not region or not rv3d or not session.ready:
        return
    scale = context.preferences.system.ui_scale
    radius = 10 * scale
    for number, thread in session.numbered():
        if thread.get("resolved"):
            continue
        obj = object_from_path(thread["anchor"]["selector"])
        if not obj or not obj.visible_get():
            continue
        local = session.points.get(thread["id"])
        if local is not None:
            world = obj.matrix_world @ Vector(local)
        else:
            corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
            world = sum(corners, Vector()) / 8
            world.z = max(c.z for c in corners)
        screen = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
        if screen is None:
            continue
        # Offset up-right so the pin points at the spot instead of covering it.
        center = screen + Vector((radius * 0.8, radius * 0.8))
        _circle(center, radius + 1.5 * scale, (1, 1, 1, 0.95))
        _circle(center, radius, COLORS.get(thread.get("status", "idle"), COLORS["idle"]))
        font = 0
        blf.size(font, 11 * scale)
        label = str(number)
        width, height = blf.dimensions(font, label)
        blf.color(font, 1, 1, 1, 1)
        blf.position(font, center.x - width / 2, center.y - height / 2, 0)
        blf.draw(font, label)


def _circle(center: Vector, radius: float, color: tuple[float, float, float, float]) -> None:
    points = [(center.x, center.y)] + [
        (center.x + radius * math.cos(a), center.y + radius * math.sin(a))
        for a in (2 * math.pi * i / SEGMENTS for i in range(SEGMENTS))
    ]
    indices = [(0, i, i % SEGMENTS + 1) for i in range(1, SEGMENTS + 1)]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": points}, indices=indices)
    shader.uniform_float("color", color)
    gpu.state.blend_set("ALPHA")
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def register() -> None:
    global _handle
    _handle = bpy.types.SpaceView3D.draw_handler_add(draw, (), "WINDOW", "POST_PIXEL")


def unregister() -> None:
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
    _handle = None
