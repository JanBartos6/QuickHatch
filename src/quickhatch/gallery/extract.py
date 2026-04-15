"""Enrich filtered gallery candidates with repo parsing and image analysis."""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError

from .common import (
    COMMUNITY_ENTRY_WARNING,
    DOTFILES_CACHE_DIR,
    EXTRACTED_CACHE_DIR,
    FILTERED_CACHE_DIR,
    GALLERY_IMAGE_DIR,
    PACKAGE_DISTROS,
    REQUEST_TIMEOUT,
    USER_AGENT,
    detect_de,
    detect_font,
    detect_icons,
    detect_theme,
    ensure_pipeline_dirs,
    load_json,
    parse_date,
    write_json,
)

PACKAGE_FILE_PATTERNS = {
    "arch": ("pacman.txt", "packages.arch", "arch-packages.txt", "pkglist.txt"),
    "fedora": ("dnf.txt", "packages.fedora", "fedora-packages.txt"),
    "ubuntu": ("apt.txt", "packages.ubuntu", "ubuntu-packages.txt"),
}
PACKAGE_LINE_RE = re.compile(r"^[A-Za-z0-9@._+-]+$")
README_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
README_HTML_IMAGE_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
CONFIG_FILE_PATTERNS = ("*.conf", "*.css", "*.ini", "*.json", "*.lua", "*.toml", "*.yaml", "*.yml")
CONFIG_DIR_HINTS = (
    "alacritty",
    "awesome",
    "bspwm",
    "dunst",
    "eww",
    "foot",
    "ghostty",
    "gtk",
    "hypr",
    "i3",
    "kitty",
    "quickshell",
    "rofi",
    "sddm",
    "sway",
    "sxhkd",
    "waybar",
    "wezterm",
    "wofi",
    "xfce",
)
CONFIG_SKIP_HINTS = (
    "node_modules",
    ".git",
    "nvim",
    "vim",
    "tmux",
    "zsh",
    "emacs",
    "tests",
    "vendor",
)
MAX_EXTRA_TEXT_FILES = 40


def clone_dotfiles_repo(repo_url: str, destination: Path) -> Path:
    """Clone a dotfiles repository shallowly unless it is already cached."""
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    return destination


def detect_install_method(repo_dir: Path) -> str:
    """Infer the repo's preferred dotfile install strategy."""
    if (repo_dir / ".stow-local-ignore").exists():
        return "stow"
    if (repo_dir / ".chezmoi.toml").exists():
        return "chezmoi"
    if _safe_rglob(repo_dir, "playbook*.yml") or _safe_rglob(repo_dir, "playbook*.yaml"):
        return "ansible"
    if _safe_rglob(repo_dir, "install.sh"):
        return "script"
    return "manual"


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _candidate_text_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for name in ("README.md", "README", "readme.md", "install.sh", "setup.sh"):
        path = repo_dir / name
        if path.exists():
            files.append(path)
    for filename in ("Brewfile", "packages.txt", "packages", "pacman.txt", "apt.txt", "dnf.txt"):
        for path in _safe_rglob(repo_dir, filename):
            if path not in files:
                files.append(path)
    for path in _interesting_config_files(repo_dir):
        if path not in files:
            files.append(path)
    return files


def _safe_rglob(root: Path, pattern: str) -> list[Path]:
    matches: list[Path] = []
    for current_root, _dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
        base = Path(current_root)
        for filename in filenames:
            if fnmatch.fnmatch(filename, pattern):
                matches.append(base / filename)
    return matches


def _interesting_config_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in CONFIG_FILE_PATTERNS:
        for path in _safe_rglob(repo_dir, pattern):
            lower_parts = [part.lower() for part in path.parts]
            if any(skip in part for part in lower_parts for skip in CONFIG_SKIP_HINTS):
                continue
            if not any(hint in part for part in lower_parts for hint in CONFIG_DIR_HINTS):
                continue
            try:
                if path.stat().st_size > 128_000:
                    continue
            except OSError:
                continue
            files.append(path)
            if len(files) >= MAX_EXTRA_TEXT_FILES:
                return files
    return files


def extract_package_lists(repo_dir: Path) -> dict[str, list[str]]:
    """Gather distro package names from common dotfile repo manifests."""
    packages = {distro: [] for distro in PACKAGE_DISTROS}
    for distro, patterns in PACKAGE_FILE_PATTERNS.items():
        for pattern in patterns:
            for path in _safe_rglob(repo_dir, pattern):
                for line in _read_text_file(path).splitlines():
                    cleaned = line.split("#", 1)[0].strip().strip(",")
                    if cleaned and PACKAGE_LINE_RE.match(cleaned):
                        packages[distro].append(cleaned)

    for path in _safe_rglob(repo_dir, "install.sh"):
        text = _read_text_file(path)
        for distro, manager in (
            ("arch", "pacman -S"),
            ("fedora", "dnf install"),
            ("ubuntu", "apt install"),
        ):
            for match in re.findall(rf"{re.escape(manager)}[^\n]+", text):
                pieces = re.split(r"\s+", match)
                tokens = [
                    piece
                    for piece in pieces[2:]
                    if piece and not piece.startswith("-") and PACKAGE_LINE_RE.match(piece)
                ]
                packages[distro].extend(tokens)

    return {key: sorted(dict.fromkeys(value)) for key, value in packages.items()}


def parse_repo_metadata(repo_dir: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    """Fill in theme/icon/font/DE details from README and scripts."""
    combined_text = "\n".join(_read_text_file(path) for path in _candidate_text_files(repo_dir))
    combined_text = "\n".join(
        filter(None, [candidate.get("title", ""), candidate.get("body", ""), combined_text])
    )
    return {
        "de": candidate.get("de") or detect_de(combined_text),
        "theme": candidate.get("theme") or detect_theme(combined_text),
        "icons": candidate.get("icons") or detect_icons(combined_text),
        "font_ui": candidate.get("font_ui") or detect_font(combined_text),
        "font_mono": candidate.get("font_mono") or detect_font(combined_text),
    }


def find_preview_sources(repo_dir: Path, candidate: dict[str, Any]) -> list[str | Path]:
    """Locate candidate screenshot sources from metadata or the cloned repo."""
    sources: list[str | Path] = []

    def add_source(source: str | Path) -> None:
        if isinstance(source, Path):
            if source.suffix.lower() == ".svg":
                return
            if source not in sources:
                sources.append(source)
        elif source and source not in sources and not source.lower().endswith(".svg"):
            sources.append(source)

    if candidate.get("preview_url"):
        add_source(str(candidate["preview_url"]))

    for readme_name in ("README.md", "README", "readme.md"):
        readme_path = repo_dir / readme_name
        if not readme_path.exists():
            continue
        text = _read_text_file(readme_path)
        for image_ref in README_IMAGE_RE.findall(text):
            if image_ref.startswith("http://") or image_ref.startswith("https://"):
                add_source(image_ref)
            local_path = (readme_path.parent / image_ref).resolve()
            if local_path.exists():
                add_source(local_path)
        for image_ref in README_HTML_IMAGE_RE.findall(text):
            if image_ref.startswith("http://") or image_ref.startswith("https://"):
                add_source(image_ref)
            local_path = (readme_path.parent / image_ref).resolve()
            if local_path.exists():
                add_source(local_path)

    preferred_names = ("screenshot", "preview", "desktop", "rice")
    for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        for path in _safe_rglob(repo_dir, pattern):
            lower = path.name.lower()
            if any(name in lower for name in preferred_names):
                add_source(path)
    for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        match = next(iter(_safe_rglob(repo_dir, pattern)), None)
        if match is not None:
            add_source(match)
    return sources


def download_preview_image(source: str | Path, destination: Path) -> Path:
    """Download a preview image and transcode it to a thumbnail-sized WebP."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(source, Path):
        image = Image.open(source).convert("RGB")
    else:
        response = requests.get(
            source,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        response.raise_for_status()
        image = Image.open(response.raw).convert("RGB")
    image.thumbnail((960, 540))
    image.save(destination, format="WEBP", quality=82, method=6)
    return destination


def extract_palette_metrics(image_path: Path) -> tuple[list[str], str, float]:
    """Fallback image analysis: palette, light/dark mood, and visual busyness."""
    image = Image.open(image_path).convert("RGB")
    palette_image = image.copy()
    palette_image.thumbnail((320, 320))
    quantized = palette_image.quantize(colors=5, method=Image.MEDIANCUT)
    raw_palette = quantized.getpalette() or []
    counts = quantized.getcolors() or []
    counts.sort(reverse=True)
    colors: list[str] = []
    for _, index in counts[:3]:
        start = index * 3
        rgb = raw_palette[start : start + 3]
        if len(rgb) == 3:
            colors.append(f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")
    stat = ImageStat.Stat(image)
    luminance = sum(stat.mean) / (len(stat.mean) * 255)
    mood = "dark" if luminance < 0.5 else "light"
    edge_map = image.filter(ImageFilter.FIND_EDGES).convert("L")
    edge_stat = ImageStat.Stat(edge_map)
    busyness = max(0.0, min(1.0, (edge_stat.mean[0] or 0) / 255))
    return colors, mood, round(busyness, 2)


def normalize_description(candidate: dict[str, Any]) -> str:
    """Generate a one-sentence fallback description."""
    desktop_name = str(candidate.get("de") or "").upper()
    parts = [desktop_name or "Desktop recipe"]
    if candidate.get("theme"):
        parts.append(candidate["theme"].replace("-", " ").title())
    if candidate.get("icons"):
        parts.append(f"{candidate['icons'].replace('-', ' ').title()} icons")
    if candidate.get("font_ui"):
        parts.append(f"{candidate['font_ui'].replace('-', ' ').title()} UI font")
    sentence = " + ".join(parts)
    if candidate.get("title"):
        return f"{sentence}. Inspired by {candidate['title']}."
    return f"{sentence}."


def staleness_score(last_commit: str) -> float:
    """Convert repo age into a 0..1 freshness score."""
    commit_date = parse_date(last_commit)
    if commit_date is None:
        return 1.0
    age_days = (datetime.now(UTC).date() - commit_date).days
    return round(max(0.0, min(1.0, age_days / 540)), 2)


def extract_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Process one filtered candidate into the shipped gallery schema."""
    ensure_pipeline_dirs()
    repo_dir = DOTFILES_CACHE_DIR / candidate["id"]
    clone_dotfiles_repo(candidate["dotfiles_url"], repo_dir)

    parsed = parse_repo_metadata(repo_dir, candidate)
    if not parsed.get("de"):
        raise ValueError(f"{candidate['id']} does not expose an identifiable desktop environment")
    if not any(parsed.get(field) for field in ("theme", "icons", "font_ui", "font_mono")):
        raise ValueError(f"{candidate['id']} does not expose a named theme, icon set, or font")
    packages = extract_package_lists(repo_dir)
    preview_image = f"gallery/{candidate['id']}.webp"
    preview_path = GALLERY_IMAGE_DIR / f"{candidate['id']}.webp"
    preview_sources = find_preview_sources(repo_dir, candidate)
    for preview_source in preview_sources:
        try:
            download_preview_image(preview_source, preview_path)
            break
        except (requests.RequestException, UnidentifiedImageError, OSError):
            continue

    palette, mood, busyness = (
        extract_palette_metrics(preview_path)
        if preview_path.exists()
        else (["#1f2937", "#4b5563", "#d1d5db"], "dark", 0.4)
    )

    license_status = candidate.get(
        "license_status", "permissive" if candidate.get("dotfiles_license") else "unknown"
    )
    callout = (
        "Source repo has no license — this entry is shown as inspiration only. "
        "The agent will not auto-apply it."
        if license_status == "unknown"
        else COMMUNITY_ENTRY_WARNING
    )
    return {
        "id": candidate["id"],
        "source": candidate["source"],
        "source_url": candidate["source_url"],
        "author": candidate["author"],
        "upvotes": candidate["upvotes"],
        "post_date": candidate["post_date"],
        "title": candidate["title"],
        "preview_image": preview_image,
        "description_short": normalize_description({**candidate, **parsed}),
        "description_callout": callout,
        "de": parsed.get("de") or candidate.get("de") or "unknown",
        "theme": parsed.get("theme"),
        "icons": parsed.get("icons"),
        "font_ui": parsed.get("font_ui"),
        "font_mono": parsed.get("font_mono"),
        "wallpaper_hint": candidate.get("wallpaper_hint") or "",
        "palette": palette,
        "mood": mood,
        "busyness": busyness,
        "dotfiles_url": candidate["dotfiles_url"],
        "dotfiles_license": candidate["dotfiles_license"],
        "license_status": license_status,
        "dotfiles_last_commit": candidate["dotfiles_last_commit"],
        "install_method": detect_install_method(repo_dir),
        "packages": packages,
        "verified": False,
        "verified_distros": [],
        "staleness_score": staleness_score(candidate["dotfiles_last_commit"]),
        "indexed_at": candidate.get("indexed_at") or datetime.now(UTC).date().isoformat(),
    }


def run_extract(
    *,
    candidates_path: Path = FILTERED_CACHE_DIR / "candidates.json",
    output_dir: Path = EXTRACTED_CACHE_DIR,
) -> list[dict[str, Any]]:
    """Extract every filtered candidate into per-entry JSON files."""
    ensure_pipeline_dirs()
    candidates = load_json(candidates_path, default=[])
    extracted: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            entry = extract_candidate(candidate)
        except (requests.RequestException, subprocess.CalledProcessError, ValueError, OSError):
            continue
        write_json(output_dir / f"{entry['id']}.json", entry)
        extracted.append(entry)
    return extracted


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    return argparse.ArgumentParser(description=__doc__)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    build_parser().parse_args(argv)
    run_extract()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
