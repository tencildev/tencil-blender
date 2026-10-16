"""What the agent learns about the picked objects.

Tencil's wire format describes DOM elements (tag, selector, HTML, rect). Until it
has a kind for other apps, the object is described in those fields: the
description goes where the HTML would, the data path where the selector would.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
from typing import Any

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

MAX_DESCRIPTION = 1900
MAX_SCREENSHOT_WIDTH = 1600

INSTRUCTIONS = (
    "This is an object in the user's open Blender scene, not an element of a web page. "
    "Inspect and change it in the running Blender with the Blender MCP tools (Python through bpy); "
    "only edit files in this workspace if the change belongs in a script."
)


def data_path(obj: bpy.types.Object) -> str:
    return 'bpy.data.objects["{}"]'.format(obj.name.replace("\\", "\\\\").replace('"', '\\"'))


def object_from_path(path: str) -> bpy.types.Object | None:
    match = re.fullmatch(r'bpy\.data\.objects\["((?:[^"\\]|\\.)*)"\]', path)
    if not match:
        return None
    name = re.sub(r"\\(.)", r"\1", match.group(1))
    return bpy.data.objects.get(name)


def describe(objects: list[bpy.types.Object], point: Vector | None) -> str:
    active, *others = objects
    lines = [INSTRUCTIONS, describe_object(active)]
    if point is not None:
        lines.append("Picked at {} in the object's local space.".format(_vec(point)))
    for other in others[:5]:
        lines.append("Also selected: " + describe_object(other))
    if len(others) > 5:
        lines.append(f"…and {len(others) - 5} more selected objects.")
    scene = bpy.context.scene
    lines.append(
        'Scene "{}", frame {}, Blender {}, file {}.'.format(
            scene.name,
            scene.frame_current,
            bpy.app.version_string,
            bpy.path.basename(bpy.data.filepath) or "unsaved",
        )
    )
    text = " ".join(lines)
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 1] + "…"


def describe_object(obj: bpy.types.Object) -> str:
    parts = [
        f'{data_path(obj)} ({obj.type.lower()}):',
        f"location {_vec(obj.location)}",
        f"rotation {_vec(obj.rotation_euler)} rad",
        f"scale {_vec(obj.scale)}",
        f"dimensions {_vec(obj.dimensions)}",
    ]
    if obj.parent:
        parts.append(f'parent "{obj.parent.name}"')
    collections = [c.name for c in obj.users_collection]
    if collections:
        parts.append("in " + ", ".join(f'"{c}"' for c in collections))
    if obj.type == "MESH" and obj.data:
        mesh = obj.data
        parts.append(f'mesh "{mesh.name}" with {len(mesh.vertices)} vertices, {len(mesh.polygons)} faces')
    elif obj.data is not None:
        parts.append(f'data "{obj.data.name}"')
    if obj.modifiers:
        parts.append("modifiers " + ", ".join(f'{m.name} ({m.type.lower()})' for m in obj.modifiers))
    materials = [slot.material.name for slot in obj.material_slots if slot.material]
    if materials:
        parts.append("materials " + ", ".join(f'"{m}"' for m in materials))
    if obj.hide_viewport or obj.hide_get():
        parts.append("hidden in the viewport")
    return " ".join(parts[:1]) + " " + "; ".join(parts[1:]) + "."


def screen_rect(
    objects: list[bpy.types.Object], region: bpy.types.Region | None, rv3d: Any
) -> dict[str, float]:
    """The objects' bounds projected into the viewport, top-left origin like the web."""
    xs, ys = [], []
    if region and rv3d:
        for obj in objects:
            for corner in obj.bound_box:
                world = obj.matrix_world @ Vector(corner)
                screen = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
                if screen is not None:
                    xs.append(screen.x)
                    ys.append(region.height - screen.y)
    if not xs:
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    left, top = max(min(xs), 0), max(min(ys), 0)
    right = min(max(xs), region.width) if region else max(xs)
    bottom = min(max(ys), region.height) if region else max(ys)
    return {"x": left, "y": top, "width": max(right - left, 0), "height": max(bottom - top, 0)}


def build(
    objects: list[bpy.types.Object],
    region: bpy.types.Region | None,
    rv3d: Any,
    point: Vector | None,
    click: tuple[float, float] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The pin's ElementContext and Anchor."""
    active = objects[0]
    rect = screen_rect(objects, region, rv3d)
    chain = []
    walk: bpy.types.Object | None = active
    while walk and len(chain) < 5:
        chain.append({"name": walk.name, "library": "Blender object"})
        walk = walk.parent
    context = {
        "tagName": active.type.lower(),
        "selector": data_path(active),
        "html": describe(objects, point),
        "text": active.name,
        "components": chain,
        "rect": rect,
        "viewport": {
            "width": region.width if region else 0,
            "height": region.height if region else 0,
            "dpr": bpy.context.preferences.system.pixel_size,
        },
    }
    offset_x = offset_y = 0.5
    if click and rect["width"] > 0 and rect["height"] > 0 and region:
        offset_x = _clamp((click[0] - rect["x"]) / rect["width"])
        offset_y = _clamp((region.height - click[1] - rect["y"]) / rect["height"])
    anchor = {"selector": data_path(active), "offsetX": offset_x, "offsetY": offset_y}
    return context, anchor


def screenshot(context: bpy.types.Context, area: bpy.types.Area | None) -> dict[str, Any] | None:
    """A PNG of the 3D view, scaled down if it's large. None if Blender can't make one.

    Rendered offscreen rather than read from the window: no operator runs, so it can't
    disturb a popup that's open, and the popup isn't in the picture.
    """
    region = next((r for r in area.regions if r.type == "WINDOW"), None) if area else None
    if not region or not region.data or region.width < 2 or region.height < 2:
        return None
    path = os.path.join(tempfile.gettempdir(), "tencil-blender-screenshot.png")
    image = None
    try:
        import gpu
        import numpy as np

        width, height = region.width, region.height
        offscreen = gpu.types.GPUOffScreen(width, height)
        try:
            offscreen.draw_view3d(
                context.scene,
                context.view_layer,
                area.spaces.active,
                region,
                region.data.view_matrix,
                region.data.window_matrix,
                do_color_management=True,
            )
            pixels = np.asarray(offscreen.texture_color.read(), dtype=np.float32).reshape(-1)
        finally:
            offscreen.free()
        pixels /= 255.0
        image = bpy.data.images.new("tencil-screenshot", width, height, alpha=True)
        image.pixels.foreach_set(pixels)
        if width > MAX_SCREENSHOT_WIDTH:
            image.scale(MAX_SCREENSHOT_WIDTH, round(height * MAX_SCREENSHOT_WIDTH / width))
        image.filepath_raw = path
        image.file_format = "PNG"
        image.save()
        with open(path, "rb") as file:
            data = base64.b64encode(file.read()).decode("ascii")
        return {"name": "viewport.png", "mimeType": "image/png", "dataUrl": "data:image/png;base64," + data}
    except Exception as error:
        print(f"Tencil: no screenshot ({error})")
        return None
    finally:
        if image is not None:
            bpy.data.images.remove(image)
        try:
            os.remove(path)
        except OSError:
            pass


def _vec(values: Any) -> str:
    return "(" + ", ".join(f"{v:.3g}" for v in values) + ")"


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)
