"""Tests for gallery image deduplication."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from quickhatch.gallery.dedupe import dedupe_entries


def _save_image(path: Path, color: str, *, accent: str | None = None) -> None:
    image = Image.new("RGB", (320, 200), color)
    if accent:
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 120, 120), fill=accent)
    image.save(path, format="WEBP")


def test_dedupe_keeps_higher_scored_near_duplicate(tmp_path):
    _save_image(tmp_path / "a.webp", "#1e1e2e", accent="#cba6f7")
    _save_image(tmp_path / "b.webp", "#1e1e2e", accent="#cba6f7")
    _save_image(tmp_path / "c.webp", "#f5f5f5", accent="#89b4fa")

    entries = [
        {"id": "a", "preview_image": "gallery/a.webp", "upvotes": 200},
        {"id": "b", "preview_image": "gallery/b.webp", "upvotes": 80},
        {"id": "c", "preview_image": "gallery/c.webp", "upvotes": 150},
    ]

    deduped = dedupe_entries(entries, image_dir=tmp_path, distance_threshold=2)

    ids = [entry["id"] for entry in deduped]
    assert "a" in ids
    assert "c" in ids
    assert "b" not in ids
