"""Tests for gallery candidate filtering."""

from __future__ import annotations

from quickhatch.gallery.filter import compute_reddit_thresholds, filter_candidate


def _repo_meta(url: str):
    assert url == "https://github.com/example/dotfiles"
    return {
        "license": {"spdx_id": "MIT"},
        "pushed_at": "2026-03-01T10:00:00Z",
        "stargazers_count": 250,
    }


def test_accepts_reddit_candidate_with_required_fields():
    candidates = [
        {
            "kind": "reddit",
            "id": "unixporn-good",
            "source": "r/unixporn",
            "source_id": "good",
            "source_url": "https://reddit.com/r/unixporn/comments/good",
            "author": "u/example",
            "upvotes": 900,
            "post_date": "2026-01-05",
            "title": "[Hyprland] Catppuccin + Inter",
            "body": "Dotfiles: https://github.com/example/dotfiles",
            "dotfiles_url": "https://github.com/example/dotfiles",
            "de": "hyprland",
            "signals": {"theme": "catppuccin", "font": "inter"},
            "year": 2026,
            "subreddit": "unixporn",
        },
        {
            "kind": "reddit",
            "id": "unixporn-baseline",
            "source": "r/unixporn",
            "source_id": "baseline",
            "source_url": "",
            "author": "u/other",
            "upvotes": 10,
            "post_date": "2026-01-02",
            "title": "[GNOME] plain setup",
            "body": "",
            "dotfiles_url": "https://github.com/example/dotfiles",
            "de": "gnome",
            "signals": {"theme": "adwaita"},
            "year": 2026,
            "subreddit": "unixporn",
        },
    ]

    thresholds = compute_reddit_thresholds(candidates)
    accepted = filter_candidate(
        candidates[0],
        reddit_thresholds=thresholds,
        repo_metadata_fetcher=_repo_meta,
    )

    assert accepted is not None
    assert accepted["dotfiles_license"] == "MIT"
    assert accepted["dotfiles_last_commit"] == "2026-03-01"
    assert accepted["verified"] is False


def test_rejects_candidates_without_named_theme_icon_or_font():
    candidate = {
        "kind": "reddit",
        "id": "unixporn-missing-style",
        "source": "r/unixporn",
        "source_id": "missing-style",
        "source_url": "https://reddit.com/r/unixporn/comments/missing",
        "author": "u/example",
        "upvotes": 1200,
        "post_date": "2026-01-05",
        "title": "[Hyprland] clean desktop",
        "body": "Dotfiles: https://github.com/example/dotfiles",
        "dotfiles_url": "https://github.com/example/dotfiles",
        "de": "hyprland",
        "signals": {},
        "year": 2026,
        "subreddit": "unixporn",
    }

    rejected = filter_candidate(
        candidate,
        reddit_thresholds={("unixporn", 2026): 100},
        repo_metadata_fetcher=_repo_meta,
    )

    assert rejected is None


def test_rejects_stale_or_unlicensed_repos():
    candidate = {
        "kind": "github",
        "id": "github-stale",
        "source": "github",
        "source_id": "example/dotfiles",
        "source_url": "https://github.com/example/dotfiles",
        "author": "example",
        "upvotes": 500,
        "post_date": "2024-01-05",
        "title": "example/dotfiles",
        "body": "GNOME with Inter",
        "dotfiles_url": "https://github.com/example/dotfiles",
        "de": "gnome",
        "signals": {"font": "inter"},
        "year": 2024,
        "subreddit": "",
    }

    def stale_repo(_url: str):
        return {
            "license": {"spdx_id": None},
            "pushed_at": "2023-01-01T00:00:00Z",
            "stargazers_count": 999,
        }

    assert (
        filter_candidate(
            candidate,
            reddit_thresholds={},
            repo_metadata_fetcher=stale_repo,
        )
        is None
    )
