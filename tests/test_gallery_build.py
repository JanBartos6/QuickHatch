"""Tests for building the shipped gallery payload."""

from __future__ import annotations

from quickhatch.gallery import build as gallery_build


def test_build_gallery_skips_entries_without_preview_or_detected_de(tmp_path, monkeypatch):
    image_dir = tmp_path / "gallery"
    image_dir.mkdir()
    (image_dir / "good.webp").write_bytes(b"webp")
    (image_dir / "unknown.webp").write_bytes(b"webp")
    monkeypatch.setattr(gallery_build, "GALLERY_IMAGE_DIR", image_dir)

    entries = [
        {
            "id": "good",
            "preview_image": "gallery/good.webp",
            "de": "hyprland",
            "verified": False,
            "source": "github",
            "upvotes": 10,
        },
        {
            "id": "unknown",
            "preview_image": "gallery/unknown.webp",
            "de": "unknown",
            "verified": False,
            "source": "github",
            "upvotes": 20,
        },
        {
            "id": "missing-preview",
            "preview_image": "gallery/missing.webp",
            "de": "gnome",
            "verified": False,
            "source": "github",
            "upvotes": 30,
        },
    ]

    built = gallery_build.build_gallery(entries)

    assert [entry["id"] for entry in built] == ["good"]
