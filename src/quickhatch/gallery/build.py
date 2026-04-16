"""Build the shipped gallery JSON from processed entry artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import (
    EXTRACTED_CACHE_DIR,
    GALLERY_IMAGE_DIR,
    GALLERY_JSON_PATH,
    ensure_pipeline_dirs,
    load_json,
    write_json,
)
from .curate import cap_with_diversity, dedupe_by_repo, merge_descriptions, normalize_entry

# Where curator-authored descriptions + best_for tags live. One entry per
# recipe id: {"recipe-id": {"description": "...", "best_for": [...]}, ...}.
DESCRIPTIONS_JSON_PATH = GALLERY_JSON_PATH.parent / "gallery_descriptions.json"


def build_gallery(entries: list[dict[str, Any]], *, cap: int = 250) -> list[dict[str, Any]]:
    """Normalize, dedupe, cap, and sort extracted entries for shipping."""
    preview_ready = [
        entry
        for entry in entries
        if (GALLERY_IMAGE_DIR.parent / entry.get("preview_image", "")).exists()
        and entry.get("de")
        and entry.get("de") != "unknown"
    ]
    normalized = [normalize_entry(dict(e)) for e in preview_ready]
    deduped = dedupe_by_repo(normalized)
    capped = cap_with_diversity(deduped, cap=cap)
    if DESCRIPTIONS_JSON_PATH.exists():
        descriptions = load_json(DESCRIPTIONS_JSON_PATH, default={})
        capped = merge_descriptions(capped, descriptions)
    return sorted(
        capped,
        key=lambda entry: (
            not entry.get("verified", False),
            entry.get("source") != "r/unixporn",
            -int(entry.get("upvotes", 0)),
            entry.get("id", ""),
        ),
    )


def run_build(
    *,
    source_path: Path | None = None,
    output_path: Path = GALLERY_JSON_PATH,
    cap: int = 250,
) -> list[dict[str, Any]]:
    """Read extracted entry data and write the packaged gallery.json.

    When ``source_path`` is ``None``, reads every ``.json`` file from
    ``EXTRACTED_CACHE_DIR``. A path may be supplied to read from a legacy
    deduped file instead.
    """
    ensure_pipeline_dirs()
    if source_path is None:
        entries: list[dict[str, Any]] = []
        for path in sorted(EXTRACTED_CACHE_DIR.glob("*.json")):
            try:
                entries.append(load_json(path, default=None))
            except Exception:
                continue
        entries = [e for e in entries if e]
    else:
        entries = load_json(source_path, default=[])
    gallery = build_gallery(entries, cap=cap)
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
