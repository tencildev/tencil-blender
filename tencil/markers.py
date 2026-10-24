"""Pins in the 3D viewport, on the spot each object was picked.

They're drawn like the browser overlay's: a rounded tag whose sharp corner points at
the spot, white-rimmed, coloured by status. While the agent works the pin turns into
the logo (no rim) and a light travels round its edge.
"""

from __future__ import annotations

import math
import time

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from .context import object_from_path
from .state import ACTIVE, current_step, session

Color = tuple[float, float, float, float]

# The overlay's colours (styles.ts).
ACCENT: Color = (0.18, 0.47, 0.84, 1.0)
COLORS: dict[str, Color] = {
    "draft": ACCENT,
    "starting": ACCENT,
    "working": ACCENT,
    "waiting": (0.90, 0.60, 0.06, 1.0),
    "idle": (0.10, 0.54, 0.42, 1.0),
    "error": (0.90, 0.28, 0.30, 1.0),
}
WHITE: Color = (1.0, 1.0, 1.0, 1.0)
CORNER_STEPS = 8
# One lap of the travelling light, in seconds; the overlay's is 2.
LAP = 2.0

_handle = None
# Where each pin was drawn, per viewport region, so a click can find it: (x0, y0, x1, y1, thread id).
hits: dict[int, list[tuple[float, float, float, float, str]]] = {}


def pin_at(region: bpy.types.Region, x: float, y: float) -> str | None:
    """The thread whose pin is under (x, y) in region coordinates; the topmost wins."""
    for x0, y0, x1, y1, thread_id in reversed(hits.get(region.as_pointer(), [])):
        if x0 <= x <= x1 and y0 <= y <= y1:
            return thread_id
    return None


def draw() -> None:
    context = bpy.context
    region, rv3d = context.region, context.region_data
    if not region or not rv3d:
        return
    drawn: list[tuple[float, float, float, float, str]] = []
    hits[region.as_pointer()] = drawn
    if not session.ready:
        return
    scale = context.preferences.system.ui_scale
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
        drawn.append((*_pin(screen, str(number), thread, scale), thread["id"]))


def _pin(spot: Vector, label: str, thread: dict, scale: float) -> tuple[float, float, float, float]:
    """Draws the pin; returns the rectangle a click on it lands in."""
    status = thread.get("status", "idle")
    color = COLORS.get(status, COLORS["idle"])
    working = status in ("starting", "working")
    height = 22 * scale
    font = 0
    blf.size(font, 11 * scale)
    text_width, text_height = blf.dimensions(font, label)
    width = max(height, text_width + 14 * scale)
    # The sharp corner sits on the spot, the tag up and to the right of it.
    x, y = spot.x, spot.y
    radii = (3 * scale, height / 2, height / 2, height / 2)  # bottom-left, bottom-right, top-right, top-left
    rim = 2 * scale

    # A soft shadow underneath, like the overlay's box-shadow.
    for spread, alpha in ((4, 0.08), (2, 0.12)):
        grow = spread * scale
        _fill(_outline(x - grow, y - grow - 2 * scale, width + 2 * grow, height + 2 * grow, _grown(radii, grow)), (0, 0, 0, alpha))

    if working:
        phase = (time.monotonic() / LAP) % 1.0
        glow = 0.5 + 0.5 * math.sin(time.monotonic() * math.tau / LAP)
        for spread, alpha in ((6, 0.10), (3, 0.18)):
            grow = spread * scale
            _fill(_outline(x - grow, y - grow, width + 2 * grow, height + 2 * grow, _grown(radii, grow)), (*color[:3], alpha * (0.6 + 0.4 * glow)))
        _fill(_outline(x, y, width, height, radii), color)
        _travelling_light(_outline(x, y, width, height, radii), color, phase, rim)
    else:
        _fill(_outline(x - rim, y - rim, width + 2 * rim, height + 2 * rim, _grown(radii, rim)), WHITE)
        _fill(_outline(x, y, width, height, radii), color)

    blf.color(font, 1, 1, 1, 1)
    blf.position(font, x + (width - text_width) / 2, y + (height - text_height) / 2 + scale, 0)
    blf.draw(font, label)

    right = x + width + rim
    if status in ACTIVE:
        right = _activity_label(thread, Vector((x + width + rim, y + height / 2)), scale)
    return (x - rim, y - rim, right, y + height + rim)


def _activity_label(thread: dict, left_middle: Vector, scale: float) -> float:
    """What the agent is doing, next to its pin. Returns where it ends on the right."""
    if thread.get("status") == "waiting":
        text = "Waiting for you"
    else:
        step = current_step(thread)
        text = step["label"] if step and step.get("status") == "running" else "Working…"
    if len(text) > 40:
        text = text[:39] + "…"
    font = 0
    blf.size(font, 11 * scale)
    width, height = blf.dimensions(font, text)
    pad_x, pad_y = 7 * scale, 4 * scale
    box_height = height + 2 * pad_y
    x, y = left_middle.x + 6 * scale, left_middle.y - box_height / 2
    radius = box_height / 2
    _fill(_outline(x, y, width + 2 * pad_x, box_height, (radius,) * 4), (0.09, 0.09, 0.11, 0.88))
    blf.color(font, 1, 1, 1, 0.95)
    blf.position(font, x + pad_x, y + pad_y + scale, 0)
    blf.draw(font, text)
    return x + width + 2 * pad_x


def _outline(x: float, y: float, width: float, height: float, radii: tuple[float, ...]) -> list[tuple[float, float]]:
    """A rounded rectangle, counter-clockwise from the bottom-left corner."""
    bottom_left, bottom_right, top_right, top_left = (min(r, width / 2, height / 2) for r in radii)
    corners = (
        (x + bottom_left, y + bottom_left, bottom_left, math.pi),
        (x + width - bottom_right, y + bottom_right, bottom_right, 1.5 * math.pi),
        (x + width - top_right, y + height - top_right, top_right, 0.0),
        (x + top_left, y + height - top_left, top_left, 0.5 * math.pi),
    )
    points = []
    for cx, cy, r, start in corners:
        for i in range(CORNER_STEPS + 1):
            a = start + 0.5 * math.pi * i / CORNER_STEPS
            points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


def _grown(radii: tuple[float, ...], by: float) -> tuple[float, ...]:
    return tuple(r + by for r in radii)


def _fill(points: list[tuple[float, float]], color: Color) -> None:
    # The shape is convex, so a fan from its centre covers it.
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    vertices = [(cx, cy), *points]
    count = len(points)
    indices = [(0, i, i % count + 1) for i in range(1, count + 1)]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": vertices}, indices=indices)
    shader.uniform_float("color", color)
    gpu.state.blend_set("ALPHA")
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def _travelling_light(points: list[tuple[float, float]], color: Color, phase: float, width: float) -> None:
    """The edge: white at the head, fading into a light tint of the pin's colour behind it."""
    loop = [*points, points[0]]
    lengths = [0.0]
    for a, b in zip(loop, loop[1:]):
        lengths.append(lengths[-1] + math.dist(a, b))
    total = lengths[-1] or 1.0
    tint = tuple(c + (1 - c) * 0.25 for c in color[:3])
    colors = []
    for length in lengths:
        behind = (phase - length / total) % 1.0  # 0 at the head, growing along the tail
        light = max(0.0, 1.0 - behind / 0.3) ** 1.5
        colors.append((*(t + (1 - t) * light for t in tint), 1.0))
    shader = gpu.shader.from_builtin("POLYLINE_SMOOTH_COLOR")
    batch = batch_for_shader(shader, "LINE_STRIP", {"pos": loop, "color": colors})
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", width)
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
    hits.clear()
