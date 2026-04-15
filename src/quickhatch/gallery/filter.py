"""Normalize and filter raw gallery candidates into recipe-worthy entries."""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from .common import (
    FILTERED_CACHE_DIR,
    RAW_CACHE_DIR,
    api_repo_metadata,
    detect_de,
    detect_named_design_tokens,
    ensure_pipeline_dirs,
    extract_license_spdx,
    extract_repo_activity_date,
    extract_repo_stars,
    find_repo_urls,
    is_recent,
    load_json,
    normalize_recipe_id,
    parse_date,
    parse_repo_url,
    reddit_threshold,
    write_json,
)

RepoMetadataFetcher = Callable[[str], dict[str, Any] | None]
REPO_HOST_QUERIES = ("github.com", "gitlab.com", "codeberg.org")
DOTFILE_HINTS = ("dotfiles", "dots", "rice", "config", "theme", "icons", "wallpaper", "font")


def _flatten_reddit_post(post: dict[str, Any]) -> dict[str, Any]:
    data = post.get("data", post)
    title = data.get("title", "")
    body = data.get("selftext", "") or ""
    preview = data.get("preview", {})
    preview_image = ""
    images = preview.get("images", []) if isinstance(preview, dict) else []
    if images:
        source = images[0].get("resolutions") or [images[0].get("source", {})]
        if source:
            preview_image = source[-1].get("url", "").replace("&amp;", "&")
    repo_urls = find_repo_urls(" ".join([title, body, data.get("url", "")]))
    created = parse_date(data.get("created_utc"))
    return {
        "kind": "reddit",
        "id": normalize_recipe_id(data.get("subreddit", "reddit"), data.get("id", "unknown")),
        "source": f"r/{data.get('subreddit', 'reddit')}",
        "source_id": data.get("id", ""),
        "source_url": f"https://reddit.com{data.get('permalink', '')}",
        "author": f"u/{data.get('author', 'unknown')}",
        "upvotes": int(data.get("score", 0) or 0),
        "post_date": created.isoformat() if created else "",
        "title": title,
        "body": body,
        "preview_url": preview_image,
        "dotfiles_url": repo_urls[0] if repo_urls else "",
        "all_repo_urls": repo_urls,
        "de": detect_de(" ".join(filter(None, [title, body, data.get("link_flair_text", "")]))),
        "signals": detect_named_design_tokens(" ".join([title, body])),
        "year": created.year if created else None,
        "subreddit": data.get("subreddit", "").lower(),
    }


def _flatten_github_repo(repo: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        filter(
            None,
            [
                repo.get("name", ""),
                repo.get("description", ""),
                " ".join(repo.get("topics", []) or []),
            ],
        )
    )
    pushed = parse_date(repo.get("pushed_at"))
    owner = (repo.get("owner") or {}).get("login", "unknown")
    return {
        "kind": "github",
        "id": normalize_recipe_id("github", f"{owner}-{repo.get('name', 'repo')}"),
        "source": "github",
        "source_id": repo.get("full_name", repo.get("name", "")),
        "source_url": repo.get("html_url", ""),
        "author": owner,
        "upvotes": int(repo.get("stargazers_count", 0) or 0),
        "post_date": pushed.isoformat() if pushed else "",
        "title": repo.get("full_name", repo.get("name", "")),
        "body": repo.get("description", "") or "",
        "preview_url": repo.get("open_graph_image_url", ""),
        "dotfiles_url": repo.get("html_url", ""),
        "all_repo_urls": [repo.get("html_url", "")] if repo.get("html_url") else [],
        "de": detect_de(text),
        "signals": detect_named_design_tokens(text),
        "year": pushed.year if pushed else None,
        "subreddit": "",
        "repo_metadata": repo,
    }


def collect_raw_candidates(raw_dir: Path = RAW_CACHE_DIR) -> list[dict[str, Any]]:
    """Load normalized candidates from cached raw Reddit and GitHub payloads."""
    candidates: list[dict[str, Any]] = []
    for path in sorted(raw_dir.glob("reddit-*.json")):
        payload = load_json(path, default={})
        children = payload.get("data", {}).get("children", []) if isinstance(payload, dict) else []
        candidates.extend(_flatten_reddit_post(child) for child in children)
    for path in sorted(raw_dir.glob("github-topic-*.json")):
        payload = load_json(path, default={})
        items = payload.get("items", []) if isinstance(payload, dict) else []
        candidates.extend(_flatten_github_repo(item) for item in items)
    for path in sorted(raw_dir.glob("github-search-*.json")):
        payload = load_json(path, default={})
        items = payload.get("items", []) if isinstance(payload, dict) else []
        candidates.extend(_flatten_github_repo(item) for item in items)
    return candidates


def compute_reddit_thresholds(candidates: list[dict[str, Any]]) -> dict[tuple[str, int], float]:
    """Calculate subreddit/year score thresholds for Reddit candidates."""
    buckets: dict[tuple[str, int], list[int]] = defaultdict(list)
    for candidate in candidates:
        if candidate.get("kind") != "reddit":
            continue
        subreddit = candidate.get("subreddit")
        year = candidate.get("year")
        score = candidate.get("upvotes", 0)
        if subreddit and isinstance(year, int):
            buckets[(subreddit, year)].append(int(score))
    return {key: reddit_threshold(scores) for key, scores in buckets.items()}


def _reddit_comment_cache_path(post_id: str) -> Path:
    return RAW_CACHE_DIR / "comments" / f"{post_id}.json"


def _lookup_reddit_repo_url(post_id: str) -> str:
    """Best-effort lookup of a dotfiles repo link from Reddit comments via PullPush."""
    if not post_id:
        return ""
    cache_path = _reddit_comment_cache_path(post_id)
    if cache_path.exists():
        payload = load_json(cache_path, default={})
    else:
        try:
            response = requests.get(
                "https://api.pullpush.io/reddit/comment/search/",
                params={"link_id": post_id, "size": 100},
                headers={"User-Agent": "QuickHatchGalleryBot/0.1"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            write_json(cache_path, payload)
        except requests.RequestException:
            return ""
    for comment in payload.get("data", []):
        urls = find_repo_urls(str(comment.get("body", "")))
        if urls:
            return urls[0]
    return ""


def _subreddit_repo_map_cache_path(subreddit: str) -> Path:
    return RAW_CACHE_DIR / "comments" / f"{subreddit}-repo-map.json"


def _build_subreddit_repo_map(subreddit: str) -> dict[str, str]:
    """Fetch top subreddit comments that mention Git forges and map them to post IDs."""
    if not subreddit:
        return {}
    cache_path = _subreddit_repo_map_cache_path(subreddit)
    if cache_path.exists():
        return load_json(cache_path, default={})

    mapping: dict[str, tuple[int, str]] = {}
    for host in REPO_HOST_QUERIES:
        try:
            response = requests.get(
                "https://api.pullpush.io/reddit/comment/search/",
                params={
                    "subreddit": subreddit,
                    "q": host,
                    "size": 500,
                    "sort_type": "score",
                    "sort": "desc",
                },
                headers={"User-Agent": "QuickHatchGalleryBot/0.1"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException:
            continue
        for comment in response.json().get("data", []):
            post_id = str(comment.get("link_id", "")).removeprefix("t3_")
            if not post_id:
                continue
            body = str(comment.get("body", ""))
            urls = find_repo_urls(body)
            if not urls:
                continue
            lower_body = body.lower()
            for url in urls:
                lower_url = url.lower()
                score = 0
                if any(hint in lower_body for hint in DOTFILE_HINTS):
                    score += 3
                if any(hint in lower_url for hint in DOTFILE_HINTS):
                    score += 5
                if comment.get("author") == "AutoModerator":
                    score -= 5
                if any(
                    token in lower_url
                    for token in ("/issues", "/pull", "/commit", "/releases", "/wiki")
                ):
                    score -= 4
                if score <= 0:
                    continue
                current = mapping.get(post_id)
                if current is None or score > current[0]:
                    mapping[post_id] = (score, url)
    flattened = {post_id: url for post_id, (_, url) in mapping.items()}
    write_json(cache_path, flattened)
    return flattened


def build_reddit_repo_map(candidates: list[dict[str, Any]]) -> dict[str, str]:
    """Build a post-id -> repo-url map for the Reddit subreddits present in the raw data."""
    mapping: dict[str, str] = {}
    subreddits = {
        candidate.get("subreddit", "")
        for candidate in candidates
        if candidate.get("kind") == "reddit"
    }
    for subreddit in sorted(subreddits):
        mapping.update(_build_subreddit_repo_map(subreddit))
    return mapping


def filter_candidate(
    candidate: dict[str, Any],
    *,
    reddit_thresholds: dict[tuple[str, int], float],
    repo_metadata_fetcher: RepoMetadataFetcher,
    reddit_repo_map: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Apply the must-have filter to one normalized candidate."""
    if candidate.get("kind") == "reddit":
        subreddit = candidate.get("subreddit")
        year = candidate.get("year")
        threshold = reddit_thresholds.get((subreddit, year), float("inf"))
        if candidate.get("upvotes", 0) < threshold:
            return None
        repo_url = candidate.get("dotfiles_url", "")
        if not parse_repo_url(repo_url):
            repo_url = (reddit_repo_map or {}).get(candidate.get("source_id", ""), "")
        if not parse_repo_url(repo_url):
            repo_url = _lookup_reddit_repo_url(candidate.get("source_id", ""))
        candidate["dotfiles_url"] = repo_url
    else:
        repo_url = candidate.get("dotfiles_url", "")

    if not parse_repo_url(repo_url):
        return None

    metadata = candidate.get("repo_metadata") if candidate.get("kind") != "reddit" else None
    if metadata is None:
        metadata = repo_metadata_fetcher(repo_url)
    license_spdx = extract_license_spdx(metadata)
    last_commit = extract_repo_activity_date(metadata)
    if not license_spdx or not last_commit or not is_recent(last_commit):
        return None

    if candidate.get("kind") != "reddit":
        if extract_repo_stars(metadata) < 50 or not is_recent(last_commit):
            return None

    text = " ".join(
        filter(
            None,
            [
                candidate.get("title", ""),
                candidate.get("body", ""),
                candidate.get("de", ""),
            ],
        )
    )
    if not candidate.get("de"):
        candidate["de"] = detect_de(text)
    if not candidate.get("de") and candidate.get("kind") == "reddit":
        return None

    signals = dict(candidate.get("signals", {}))
    if not signals:
        signals = detect_named_design_tokens(text)
    if not signals and candidate.get("kind") == "reddit":
        return None

    return {
        **candidate,
        "dotfiles_license": license_spdx,
        "dotfiles_last_commit": last_commit,
        "signals": signals,
        "packages": {"arch": [], "fedora": [], "ubuntu": []},
        "verified": False,
        "verified_distros": [],
        "indexed_at": datetime.now(UTC).date().isoformat(),
    }


def filter_candidates(
    candidates: list[dict[str, Any]],
    *,
    repo_metadata_fetcher: RepoMetadataFetcher = api_repo_metadata,
) -> list[dict[str, Any]]:
    """Apply gallery filters to every normalized candidate."""
    reddit_thresholds = compute_reddit_thresholds(candidates)
    reddit_repo_map = build_reddit_repo_map(candidates)
    filtered: list[dict[str, Any]] = []
    for candidate in candidates:
        accepted = filter_candidate(
            dict(candidate),
            reddit_thresholds=reddit_thresholds,
            repo_metadata_fetcher=repo_metadata_fetcher,
            reddit_repo_map=reddit_repo_map,
        )
        if accepted:
            filtered.append(accepted)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in filtered:
        recipe_id = candidate.get("id", "")
        if recipe_id in seen:
            continue
        seen.add(recipe_id)
        deduped.append(candidate)
    return deduped


def run_filter(
    *,
    raw_dir: Path = RAW_CACHE_DIR,
    output_path: Path = FILTERED_CACHE_DIR / "candidates.json",
) -> list[dict[str, Any]]:
    """Load raw caches, apply the must-have filter, and persist the result."""
    ensure_pipeline_dirs()
    candidates = collect_raw_candidates(raw_dir)
    filtered = filter_candidates(candidates)
    write_json(output_path, filtered)
    return filtered


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    return argparse.ArgumentParser(description=__doc__)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    build_parser().parse_args(argv)
    run_filter()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
