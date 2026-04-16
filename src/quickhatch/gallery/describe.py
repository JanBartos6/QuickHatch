"""Produce a human-readable digest of each extracted gallery entry.

Used as scaffolding for the description-writing step: the curator (Claude)
reads these digests and writes ``description`` + ``best_for`` into
``gallery_descriptions.json``. The digest contains everything needed to
describe the look and feel of a recipe without cloning the repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import EXTRACTED_CACHE_DIR
from .curate import cap_with_diversity, dedupe_by_repo, load_extracted, normalize_entry


def _short_readme(repo_dir: Path, max_chars: int = 1200) -> str:
    """Read a README excerpt for the curator, if the cloned repo exists."""
    for name in ("README.md", "README", "readme.md"):
        path = repo_dir / name
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
            # Strip markdown images and image HTML to keep the excerpt compact.
            import re

            text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
            text = re.sub(r"<img[^>]*>", "", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:max_chars]
    return ""


def format_entry_for_curator(entry: dict[str, Any], *, include_readme: bool = False) -> str:
    """Format one entry as a Markdown block a human curator can read quickly."""
    lines = [f"## {entry.get('id')}"]
    upv = entry.get("upvotes")
    if upv:
        lines.append(f"*{upv} upvotes — {entry.get('source', '')}*")
    if entry.get("title"):
        lines.append(f"**Title**: {entry['title']}")
    if entry.get("source_url"):
        lines.append(f"**Source**: {entry['source_url']}")
    if entry.get("dotfiles_url"):
        lines.append(f"**Dotfiles**: {entry['dotfiles_url']}")

    lines.append("")
    lines.append("### Core")
    for key in ("de", "theme", "icons", "cursor", "font_ui", "font_mono", "accent_color", "mood"):
        val = entry.get(key)
        if val:
            lines.append(f"- {key}: `{val}`")

    comps = entry.get("components") or {}
    if any(comps.values()):
        lines.append("")
        lines.append("### Layout")
        for key in (
            "panel",
            "dock",
            "launcher",
            "notifications",
            "lockscreen",
            "terminal",
            "shell",
            "prompt",
            "file_manager",
            "editor",
        ):
            val = comps.get(key)
            if val:
                lines.append(f"- {key}: `{val}`")

    dec = entry.get("decorations") or {}
    dec_bits = {k: v for k, v in dec.items() if v is not None}
    if dec_bits:
        lines.append("")
        lines.append("### Decorations")
        for key, val in dec_bits.items():
            lines.append(f"- {key}: `{val}`")

    sig = entry.get("signature_features") or []
    if sig:
        lines.append("")
        lines.append("### Signature features")
        for s in sig:
            lines.append(f"- {s}")

    if entry.get("wallpaper_hint"):
        lines.append("")
        lines.append(f"*Wallpaper hint*: {entry['wallpaper_hint']}")

    if include_readme:
        from pathlib import Path as _P

        repo_dir = _P(".scrape-cache/dotfiles") / entry.get("id", "")
        readme = _short_readme(repo_dir)
        if readme:
            lines.append("")
            lines.append("### README excerpt")
            lines.append("```")
            lines.append(readme)
            lines.append("```")

    return "\n".join(lines)


def render_curator_batch(
    entries: list[dict[str, Any]],
    *,
    start: int = 0,
    end: int | None = None,
    include_readme: bool = False,
) -> str:
    """Render a slice of entries for one curator batch."""
    slice_ = entries[start:end]
    chunks = [format_entry_for_curator(e, include_readme=include_readme) for e in slice_]
    return "\n\n---\n\n".join(chunks)


def prepare_gallery(
    *,
    extracted_dir: Path = EXTRACTED_CACHE_DIR,
    cap: int = 250,
) -> list[dict[str, Any]]:
    """Load + normalize + dedupe + cap. The curator writes descriptions for this list."""
    entries = load_extracted(extracted_dir)
    entries = [normalize_entry(dict(e)) for e in entries]
    entries = dedupe_by_repo(entries)
    # Only keep entries with a usable preview image since the UI + harness
    # both want something to show.
    from .common import GALLERY_IMAGE_DIR

    preview_ready = [
        e
        for e in entries
        if (GALLERY_IMAGE_DIR.parent / e.get("preview_image", "")).exists()
        and e.get("de")
        and e.get("de") != "unknown"
    ]
    return cap_with_diversity(preview_ready, cap=cap)


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start", type=int, default=0, help="Index of first entry to render (inclusive)."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=30,
        help="Number of entries to render in this batch. Default 30.",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=250,
        help="Cap on total curated entries. Default 250.",
    )
    parser.add_argument(
        "--include-readme",
        action="store_true",
        help="Include a short README excerpt with each entry. Slower, but more context.",
    )
    parser.add_argument("--out", type=Path, help="Write to file instead of stdout.")
    args = parser.parse_args()

    entries = prepare_gallery(cap=args.cap)
    end = args.start + args.count
    rendered = render_curator_batch(
        entries, start=args.start, end=end, include_readme=args.include_readme
    )
    header = (
        f"# Gallery batch {args.start}..{min(end, len(entries))} of {len(entries)} total\n\n"
        f"Write a description (2-3 sentences, what living with this desktop feels like) and "
        f"`best_for` (3-5 user intents: coding, writing, gaming, minimal distraction, aesthetic "
        f"showcase, developer tools, tiling enthusiast, macos lover, etc.) for each entry.\n\n"
        f"Output one JSON object with entry ids as keys:\n"
        f'```json\n{{"recipe-id": {{"description": "...", "best_for": ["..."]}}}}\n```\n\n---\n\n'
    )
    output = header + rendered
    if args.out:
        args.out.write_text(output, encoding="utf-8")
        print(f"Wrote {args.out} ({len(output)} chars, {args.count} entries)")
    else:
        print(output)
    return 0


__all__ = [
    "format_entry_for_curator",
    "prepare_gallery",
    "render_curator_batch",
]


if __name__ == "__main__":
    raise SystemExit(_cli())
