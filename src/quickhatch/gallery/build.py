"""Build the shipped gallery JSON from processed entry artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import (
    FILTERED_CACHE_DIR,
    GALLERY_IMAGE_DIR,
    GALLERY_JSON_PATH,
    ensure_pipeline_dirs,
    load_json,
    write_json,
)


def build_gallery(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort and normalize extracted entries for shipping."""
    preview_ready = [
        entry
        for entry in entries
        if (GALLERY_IMAGE_DIR.parent / entry.get("preview_image", "")).exists()
        and entry.get("de")
        and entry.get("de") != "unknown"
    ]
    return sorted(
        preview_ready,
        key=lambda entry: (
            not entry.get("verified", False),
            entry.get("source") != "r/unixporn",
            -int(entry.get("upvotes", 0)),
            entry.get("id", ""),
        ),
    )


def run_build(
    *,
    source_path: Path = FILTERED_CACHE_DIR / "deduped.json",
    output_path: Path = GALLERY_JSON_PATH,
) -> list[dict[str, Any]]:
    """Read deduped entry data and write the packaged gallery.json."""
    ensure_pipeline_dirs()
    entries = load_json(source_path, default=[])
    gallery = build_gallery(entries)
    write_json(output_path, gallery)
    return gallery


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    return argparse.ArgumentParser(description=__doc__)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    build_parser().parse_args(argv)
    run_build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
