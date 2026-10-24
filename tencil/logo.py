"""Tencil's mark as a Blender icon: the overlay's pin with a cursor in it."""

from __future__ import annotations

import os

import bpy.utils.previews

_previews: bpy.utils.previews.ImagePreviewCollection | None = None


def icon() -> int:
    """The logo's icon_value, or 0 (no icon) if it couldn't be loaded."""
    return _previews["logo"].icon_id if _previews and "logo" in _previews else 0


def register() -> None:
    global _previews
    _previews = bpy.utils.previews.new()
    _previews.load("logo", os.path.join(os.path.dirname(__file__), "assets", "logo.png"), "IMAGE")


def unregister() -> None:
    global _previews
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
    _previews = None
