"""Tests for gallery candidate filtering."""

from __future__ import annotations

from quickhatch.gallery.filter import (
    REDDIT_MIN_SCORE,
    compute_reddit_thresholds,
    filter_candidate,
)


def _repo_meta(url: str):
    assert url == "https://github.com/example/dotfiles"
    return {
        "license": {"spdx_id": "MIT"},
        "pushed_at": "2026-03-01T10:00:00Z",
        "stargazers_count": 250,
    }


def _repo_meta_no_license(_url: str):
    return {
        "license": {"spdx_id": None},
        "pushed_at": "2026-03-01T10:00:00Z",
        "stargazers_count": 5,
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
    assert accepted["license_status"] == "permissive"
    assert accepted["dotfiles_last_commit"] == "2026-03-01"
    assert accepted["verified"] is False


def test_accepts_reddit_candidate_without_explicit_signals():
    """Signals are no longer a hard gate — extract stage enriches from the repo."""
    candidate = {
        "kind": "reddit",
        "id": "unixporn-no-signals",
        "source": "r/unixporn",
        "source_id": "nosig",
        "source_url": "https://reddit.com/r/unixporn/comments/nosig",
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
    accepted = filter_candidate(
        candidate,
        reddit_thresholds={("unixporn", 2026): 500},
        repo_metadata_fetcher=_repo_meta,
    )
    assert accepted is not None


def test_reddit_accepts_unlicensed_repo_as_unknown():
    """Unlicensed repos are admitted for Reddit with license_status='unknown'."""
    candidate = {
        "kind": "reddit",
        "id": "unixporn-no-license",
        "source": "r/unixporn",
        "source_id": "nolic",
        "source_url": "https://reddit.com/r/unixporn/comments/nolic",
        "author": "u/example",
        "upvotes": 1000,
        "post_date": "2026-01-05",
        "title": "[Hyprland] nord everything",
        "body": "dotfiles at https://github.com/example/dotfiles",
        "dotfiles_url": "https://github.com/example/dotfiles",
        "de": "hyprland",
        "signals": {"theme": "nord"},
        "year": 2026,
        "subreddit": "unixporn",
    }
    accepted = filter_candidate(
        candidate,
        reddit_thresholds={("unixporn", 2026): 500},
        repo_metadata_fetcher=_repo_meta_no_license,
    )
    assert accepted is not None
    assert accepted["dotfiles_license"] is None
    assert accepted["license_status"] == "unknown"


def test_github_rejects_unlicensed_or_stale_repos():
    """GitHub entries must still have a permissive license and a recent push."""
    base = {
        "kind": "github",
        "id": "github-x",
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

    def unlicensed(_url: str):
        return {
            "license": {"spdx_id": None},
            "pushed_at": "2026-01-01T00:00:00Z",
            "stargazers_count": 999,
        }

    def stale(_url: str):
        return {
            "license": {"spdx_id": "MIT"},
            "pushed_at": "2023-01-01T00:00:00Z",
            "stargazers_count": 999,
        }

    assert (
        filter_candidate(dict(base), reddit_thresholds={}, repo_metadata_fetcher=unlicensed) is None
    )
    assert filter_candidate(dict(base), reddit_thresholds={}, repo_metadata_fetcher=stale) is None


def test_rejects_de_sub_post_without_showcase_hint():
    """r/hyprland news/tool posts without setup-showcase hints are rejected."""
    candidate = {
        "kind": "reddit",
        "id": "hyprland-release",
        "source": "r/hyprland",
        "source_id": "rel1",
        "source_url": "https://reddit.com/r/hyprland/comments/rel1",
        "author": "u/x",
        "upvotes": 500,
        "post_date": "2026-01-05",
        "title": "Hyprland v0.47.0 released",
        "body": "Changelog at https://github.com/example/dotfiles",
        "dotfiles_url": "https://github.com/example/dotfiles",
        "de": "hyprland",
        "signals": {},
        "year": 2026,
        "subreddit": "hyprland",
    }
    rejected = filter_candidate(
        candidate,
        reddit_thresholds={("hyprland", 2026): 100},
        repo_metadata_fetcher=_repo_meta,
    )
    assert rejected is None


def test_accepts_de_sub_post_with_showcase_hint():
    candidate = {
        "kind": "reddit",
        "id": "hyprland-setup",
        "source": "r/hyprland",
        "source_id": "setup1",
        "source_url": "https://reddit.com/r/hyprland/comments/setup1",
        "author": "u/x",
        "upvotes": 500,
        "post_date": "2026-01-05",
        "title": "My first hyprland setup",
        "body": "dotfiles: https://github.com/example/dotfiles",
        "dotfiles_url": "https://github.com/example/dotfiles",
        "de": "hyprland",
        "signals": {},
        "year": 2026,
        "subreddit": "hyprland",
    }
    accepted = filter_candidate(
        candidate,
        reddit_thresholds={("hyprland", 2026): 100},
        repo_metadata_fetcher=_repo_meta,
    )
    assert accepted is not None


def test_compute_reddit_thresholds_uses_floor_and_top_fraction():
    """Threshold combines per-sub absolute floor with top-N% of the bucket."""
    scores = [6000, 2000, 1000, 800, 700, 600, 500, 400, 300, 200]
    candidates = [
        {
            "kind": "reddit",
            "upvotes": s,
            "subreddit": "unixporn",
            "year": 2026,
        }
        for s in scores
    ]
    thresholds = compute_reddit_thresholds(candidates)
    cutoff = thresholds[("unixporn", 2026)]
    # top 40% of 10 = 4, so the 4th highest score (800) sets the top-cutoff.
    # min_score for unixporn is 500, so max(500, 800) = 800.
    assert cutoff == max(REDDIT_MIN_SCORE["unixporn"], 800)


def test_compute_reddit_thresholds_honors_absolute_floor():
    """For a bucket of tiny posts the per-sub floor still applies."""
    candidates = [
        {"kind": "reddit", "upvotes": s, "subreddit": "swaywm", "year": 2026}
        for s in (80, 50, 40, 30, 20, 10)
    ]
    thresholds = compute_reddit_thresholds(candidates)
    cutoff = thresholds[("swaywm", 2026)]
    # top 40% of 6 = 2.4 -> rounded to 2, so 2nd highest (50) -> but floor is 30.
    # max(30, 50) = 50.
    assert cutoff == 50
