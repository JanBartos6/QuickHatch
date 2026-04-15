"""Runtime helpers for reading shipped gallery data."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from .common import GALLERY_IMAGE_DIR, GALLERY_JSON_PATH, load_json


@lru_cache(maxsize=1)
def load_gallery_index() -> list[dict[str, Any]]:
    """Load the shipped gallery JSON once per process."""
    data = load_json(GALLERY_JSON_PATH, default=[])
    return data if isinstance(data, list) else []


def load_gallery_entries() -> list[dict[str, Any]]:
    """Return a shallow copy of the gallery list."""
    return [dict(entry) for entry in load_gallery_index()]


def find_gallery_entries(recipe_ids: list[str]) -> list[dict[str, Any]]:
    """Return gallery entries matching the requested IDs, preserving order."""
    if not recipe_ids:
        return []
    index = {entry.get("id"): dict(entry) for entry in load_gallery_index()}
    return [index[recipe_id] for recipe_id in recipe_ids if recipe_id in index]


def gallery_asset_path(relative_path: str) -> Path:
    """Resolve a relative gallery asset path under the packaged data directory."""
    path = (GALLERY_IMAGE_DIR.parent / relative_path).resolve()
    return path
