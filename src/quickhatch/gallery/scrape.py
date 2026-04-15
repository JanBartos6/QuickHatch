"""Fetch community desktop recipe candidates from Reddit and GitHub."""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .common import RAW_CACHE_DIR, ensure_pipeline_dirs, request_json_cached, slugify, write_json

REDDIT_TOP_SOURCES = [
    ("unixporn", "top", "all", None),
    ("unixporn", "top", "year", None),
    ("unixporn", "search", "year", "KDE"),
    ("unixporn", "search", "year", "GNOME"),
    ("unixporn", "search", "year", "Hyprland"),
    ("unixporn", "search", "year", "i3"),
    ("unixporn", "search", "year", "Sway"),
    ("unixporn", "search", "year", "XFCE"),
    ("unixporn", "search", "year", "Cosmic"),
    ("hyprland", "top", "all", None),
    ("hyprland", "top", "year", None),
    ("kde", "top", "year", None),
    ("gnome", "top", "year", None),
    ("i3wm", "top", "year", None),
    ("swaywm", "top", "year", None),
]

GITHUB_TOPIC_SPECS = (
    ("dotfiles", 2),
    ("rice", 2),
    ("hyprland-dots", 1),
    ("gnome-dotfiles", 1),
    ("kde-dotfiles", 1),
)
GITHUB_SEARCH_SPECS = (
    ("hyprland config stars:>=50 pushed:>=2024-10-01 archived:false", 2),
    ("linux rice stars:>=50 pushed:>=2024-10-01 archived:false", 2),
    ("gnome dotfiles stars:>=20 pushed:>=2024-10-01 archived:false", 1),
    ("plasma dotfiles stars:>=20 pushed:>=2024-10-01 archived:false", 1),
    ("awesomewm dotfiles stars:>=15 pushed:>=2024-10-01 archived:false", 1),
    ("bspwm dotfiles stars:>=15 pushed:>=2024-10-01 archived:false", 1),
    ("dwm dotfiles stars:>=15 pushed:>=2024-10-01 archived:false", 1),
    ("i3 dotfiles stars:>=15 pushed:>=2024-10-01 archived:false", 1),
    ("sway dotfiles stars:>=15 pushed:>=2024-10-01 archived:false", 1),
    ("xfce dotfiles stars:>=10 pushed:>=2024-10-01 archived:false", 1),
    ("catppuccin hyprland dotfiles stars:>=10 pushed:>=2024-10-01 archived:false", 1),
    ("nixos hyprland dotfiles stars:>=10 pushed:>=2024-10-01 archived:false", 1),
)


def _cache_name(prefix: str, *parts: str) -> Path:
    name = "-".join(slugify(part) for part in parts if part)
    return RAW_CACHE_DIR / f"{prefix}-{name}.json"


def _fetch_reddit_listing(
    subreddit: str,
    *,
    mode: str,
    time_filter: str,
    flair: str | None,
    refresh: bool,
) -> dict[str, Any]:
    if mode == "search" and flair:
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {
            "q": f'flair_name:"{flair}"',
            "restrict_sr": 1,
            "sort": "top",
            "t": time_filter,
            "type": "link",
            "limit": 100,
            "include_over_18": "on",
        }
    else:
        url = f"https://www.reddit.com/r/{subreddit}/{mode}.json"
        params = {"t": time_filter, "limit": 100, "raw_json": 1}
    cache_path = _cache_name("reddit", subreddit, mode, time_filter, flair or "")
    try:
        return request_json_cached(url, cache_path=cache_path, params=params, refresh=refresh)
    except requests.RequestException:
        return _fetch_reddit_pullpush(
            subreddit,
            mode=mode,
            time_filter=time_filter,
            flair=flair,
            cache_path=cache_path,
            refresh=refresh,
        )


def _fetch_reddit_pullpush(
    subreddit: str,
    *,
    mode: str,
    time_filter: str,
    flair: str | None,
    cache_path: Path,
    refresh: bool,
) -> dict[str, Any]:
    """Fallback Reddit fetch using PullPush when direct Reddit access is blocked."""
    if cache_path.exists() and not refresh:
        return request_json_cached(
            "https://api.pullpush.io/reddit/search/submission/", cache_path=cache_path
        )

    params: dict[str, Any] = {
        "subreddit": subreddit,
        "sort_type": "score",
        "sort": "desc",
        "size": 250,
    }
    if time_filter == "year":
        after = datetime.now(UTC) - timedelta(days=365)
        params["after"] = int(after.timestamp())
    response = requests.get(
        "https://api.pullpush.io/reddit/search/submission/",
        params=params,
        headers={"User-Agent": "QuickHatchGalleryBot/0.1"},
        timeout=30,
    )
    response.raise_for_status()
    items = response.json().get("data", [])
    if flair:
        flair_lower = flair.lower()
        items = [
            item
            for item in items
            if flair_lower in str(item.get("link_flair_text", "")).lower()
            or flair_lower in str(item.get("title", "")).lower()
            or flair_lower in str(item.get("selftext", "")).lower()
        ]
    payload = {"data": {"children": [{"data": item} for item in items]}}
    write_json(cache_path, payload)
    return payload


def _fetch_github_topic(topic: str, *, refresh: bool, page: int = 1) -> dict[str, Any]:
    url = "https://api.github.com/search/repositories"
    params = {
        "q": f"topic:{topic} stars:>=50 archived:false",
        "sort": "stars",
        "order": "desc",
        "per_page": 100,
        "page": page,
    }
    cache_path = _cache_name("github-topic", topic, f"page-{page}")
    try:
        return request_json_cached(url, cache_path=cache_path, params=params, refresh=refresh)
    except requests.RequestException as exc:
        return {"error": str(exc), "items": []}


def _fetch_github_query(query: str, *, refresh: bool, page: int = 1) -> dict[str, Any]:
    url = "https://api.github.com/search/repositories"
    params = {"q": query, "sort": "stars", "order": "desc", "per_page": 100, "page": page}
    cache_path = _cache_name("github-search", query, f"page-{page}")
    try:
        return request_json_cached(url, cache_path=cache_path, params=params, refresh=refresh)
    except requests.RequestException as exc:
        return {"error": str(exc), "items": []}


def run_scrape(*, refresh: bool = False, delay_seconds: float = 1.0) -> list[Path]:
    """Fetch all configured Reddit and GitHub source payloads."""
    ensure_pipeline_dirs()
    written: list[Path] = []

    for subreddit, mode, time_filter, flair in REDDIT_TOP_SOURCES:
        path = _cache_name("reddit", subreddit, mode, time_filter, flair or "")
        payload = _fetch_reddit_listing(
            subreddit,
            mode=mode,
            time_filter=time_filter,
            flair=flair,
            refresh=refresh,
        )
        if not path.exists():
            write_json(path, payload)
        written.append(path)
        time.sleep(delay_seconds)

    for topic, pages in GITHUB_TOPIC_SPECS:
        for page in range(1, pages + 1):
            path = _cache_name("github-topic", topic, f"page-{page}")
            payload = _fetch_github_topic(topic, refresh=refresh, page=page)
            if not path.exists():
                write_json(path, payload)
            written.append(path)
            time.sleep(delay_seconds)

    for query, pages in GITHUB_SEARCH_SPECS:
        for page in range(1, pages + 1):
            path = _cache_name("github-search", query, f"page-{page}")
            payload = _fetch_github_query(query, refresh=refresh, page=page)
            if not path.exists():
                write_json(path, payload)
            written.append(path)
            time.sleep(delay_seconds)

    return written


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Ignore cached source files.")
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to sleep between network requests.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = build_parser().parse_args(argv)
    run_scrape(refresh=args.refresh, delay_seconds=args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
