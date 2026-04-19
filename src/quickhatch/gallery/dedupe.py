"""Remove near-identical gallery screenshots using perceptual hashes."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import imagehash
from PIL import Image

from .common import (
    EXTRACTED_CACHE_DIR,
    FILTERED_CACHE_DIR,
    GALLERY_IMAGE_DIR,
    ensure_pipeline_dirs,
    load_json,
    write_json,
)


def dedupe_entries(
    entries: list[dict[str, Any]],
    *,
    image_dir: Path = GALLERY_IMAGE_DIR,
    distance_threshold: int = 8,
) -> list[dict[str, Any]]:
    """Drop entries whose screenshots are visually near-identical."""
    kept: list[dict[str, Any]] = []
    hashes: list[tuple[imagehash.ImageHash, dict[str, Any]]] = []

    for entry in sorted(entries, key=lambda item: item.get("upvotes", 0), reverse=True):
        preview_name = Path(entry.get("preview_image", "")).name
        image_path = image_dir / preview_name
        if not image_path.exists():
            kept.append(entry)
            continue
        # `with Image.open(...)` — PIL holds file handles for lazy load.
        # Without this context manager, Windows can't delete/rename the
        # PNG until GC runs, and a corrupt PNG crashes the whole dedupe
        # pass with no useful error.
        try:
            with Image.open(image_path) as _img:
                hash_value = imagehash.phash(_img)
        except Exception:
            # Corrupt / unsupported image — keep the entry as unique rather
            # than dropping it entirely.
            kept.append(entry)
            continue
        duplicate = False
        for existing_hash, existing_entry in list(hashes):
            if abs(hash_value - existing_hash) <= distance_threshold:
                duplicate = True
                if entry.get("upvotes", 0) > existing_entry.get("upvotes", 0):
                    kept.remove(existing_entry)
                    kept.append(entry)
                    hashes.remove((existing_hash, existing_entry))
                    hashes.append((hash_value, entry))
                break
        if not duplicate:
            kept.append(entry)
            hashes.append((hash_value, entry))

    return sorted(kept, key=lambda item: item.get("upvotes", 0), reverse=True)


def run_dedupe(
    *,
    extracted_dir: Path = EXTRACTED_CACHE_DIR,
    output_path: Path = FILTERED_CACHE_DIR / "deduped.json",
) -> list[dict[str, Any]]:
    """Load extracted entry JSON files, dedupe them, and write a merged list."""
    ensure_pipeline_dirs()
    entries = [load_json(path, default={}) for path in sorted(extracted_dir.glob("*.json"))]
    deduped = dedupe_entries(entries)
    write_json(output_path, deduped)
    return deduped


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    return argparse.ArgumentParser(description=__doc__)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    build_parser().parse_args(argv)
    run_dedupe()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
