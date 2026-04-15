"""Shared helpers for the desktop recipe gallery pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import quote, urlparse

import requests

# Re-exported as ``UTC`` for Python 3.10 compatibility (``datetime.UTC`` landed
# in 3.11). All gallery modules import this name instead of ``datetime.UTC``.
UTC = timezone.utc

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PACKAGE_ROOT / "data"
GALLERY_JSON_PATH = DATA_DIR / "gallery.json"
GALLERY_IMAGE_DIR = DATA_DIR / "gallery"
SCRAPE_CACHE_DIR = PROJECT_ROOT / ".scrape-cache"
RAW_CACHE_DIR = SCRAPE_CACHE_DIR / "raw"
FILTERED_CACHE_DIR = SCRAPE_CACHE_DIR / "filtered"
EXTRACTED_CACHE_DIR = SCRAPE_CACHE_DIR / "extracted"
DOTFILES_CACHE_DIR = SCRAPE_CACHE_DIR / "dotfiles"

ALLOWED_LICENSES = {
    "MIT",
    "GPL-2.0",
    "GPL-3.0",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC0-1.0",
}
ALLOWED_LICENSE_PREFIXES = ("GPL-", "BSD-")
PACKAGE_DISTROS = ("arch", "fedora", "ubuntu")
COMMUNITY_ENTRY_WARNING = (
    "Community recipe — not personally verified, but the agent will attempt it and adapt on errors."
)
USER_AGENT = "QuickHatchGalleryBot/0.1 (+https://github.com/JanBartos6/QuickHatch)"
REQUEST_TIMEOUT = 20
RECENT_MONTHS = 18

DE_KEYWORDS = {
    "hyprland": ("hyprland",),
    "plasma": ("kde plasma", "plasma desktop", "plasma", "kde"),
    "gnome": ("gnome", "gnome-shell"),
    "i3": ("i3wm", "i3-gaps", "i3"),
    "sway": ("sway",),
    "xfce": ("xfce", "xfwm"),
    "cosmic": ("cosmic",),
    "cinnamon": ("cinnamon",),
    "bspwm": ("bspwm",),
    "awesome": ("awesomewm",),
    "dwm": ("dwm",),
    "river": ("riverwm",),
    "lxqt": ("lxqt",),
    "openbox": ("openbox",),
}

# DE-specific subreddits where the DE is unambiguous from the sub itself.
# Used as a fallback when detect_de() on title+body returns nothing — a post
# of "Rice" in r/hyprland is obviously Hyprland even if the title doesn't
# name it.
SUBREDDIT_DE = {
    "hyprland": "hyprland",
    "kde": "plasma",
    "gnome": "gnome",
    "i3wm": "i3",
    "swaywm": "sway",
    "bspwm": "bspwm",
    "awesomewm": "awesome",
    "xfce": "xfce",
}

THEME_KEYWORDS = (
    "adwaita",
    "breeze",
    "catppuccin",
    "dracula",
    "everforest",
    "gruvbox",
    "kanagawa",
    "nord",
    "orchis",
    "rose pine",
    "tokyonight",
    "whitesur",
    "mocha",
    "macchiato",
    "latte",
    "frappe",
)

ICON_KEYWORDS = (
    "papirus",
    "papirus-dark",
    "tela",
    "kora",
    "la-capitaine",
    "whitesur",
    "bibata",
    "colloid",
)

FONT_KEYWORDS = (
    "inter",
    "jetbrainsmono",
    "jetbrains mono",
    "fira code",
    "maple mono",
    "monaspace",
    "cantarell",
    "sf pro",
    "source code pro",
    "nerd font",
)

THEME_REGEX = re.compile(
    r"(?:^|[\s\[(])(?:gtk theme|qt theme|colorscheme|color scheme|theme)\b\s*[:=-]\s*([^\n|,;]{2,60})",
    re.IGNORECASE,
)
ICON_REGEX = re.compile(
    r"(?:^|[\s\[(])(?:icon pack|cursor theme|cursor|icons?)\b\s*[:=-]\s*([^\n|,;]{2,60})",
    re.IGNORECASE,
)
FONT_REGEX = re.compile(
    r"(?:^|[\s\[(])(?:font-family|ui font|mono font|terminal font|font)\b\s*[:=-]\s*([^\n|,;}{]{2,60})",
    re.IGNORECASE,
)
GENERIC_TOKEN_VALUES = {
    "dark",
    "default",
    "downloads",
    "font",
    "icon",
    "icons",
    "light",
    "system",
    "theme",
}


def ensure_pipeline_dirs() -> None:
    """Create all gallery pipeline directories."""
    for path in (
        GALLERY_IMAGE_DIR,
        SCRAPE_CACHE_DIR,
        RAW_CACHE_DIR,
        FILTERED_CACHE_DIR,
        EXTRACTED_CACHE_DIR,
        DOTFILES_CACHE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any = None) -> Any:
    """Load JSON from disk or return the provided default."""
    if not path.exists():
        return [] if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> Path:
    """Write JSON with stable formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return path


def slugify(value: str) -> str:
    """Convert arbitrary text into a filesystem-safe slug."""
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return text or "item"


def normalize_recipe_id(source: str, source_id: str) -> str:
    """Build a stable recipe identifier."""
    return f"{slugify(source)}-{slugify(source_id)}"


def today_iso() -> str:
    """Return today's UTC date in ISO format."""
    return datetime.now(UTC).date().isoformat()


def parse_date(value: Any) -> date | None:
    """Parse common timestamp formats into a date."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).date()
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def is_recent(value: Any, months: int = RECENT_MONTHS, *, reference: date | None = None) -> bool:
    """Return whether the provided date is within the recent-month window."""
    parsed = parse_date(value)
    if parsed is None:
        return False
    today = reference or datetime.now(UTC).date()
    cutoff = today - timedelta(days=months * 30)
    return parsed >= cutoff


def repo_cache_path(repo_url: str) -> Path:
    """Return the cache path for a repo metadata lookup."""
    digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:16]
    return RAW_CACHE_DIR / "repo-meta" / f"{digest}.json"


def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> Any:
    """Issue a JSON HTTP request with QuickHatch defaults."""
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)
    response = requests.get(url, params=params, headers=merged_headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def request_json_cached(
    url: str,
    *,
    cache_path: Path,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    refresh: bool = False,
) -> Any:
    """Read JSON from cache when present, otherwise fetch and store it."""
    if cache_path.exists() and not refresh:
        return load_json(cache_path, default={})
    payload = request_json(url, params=params, headers=headers)
    write_json(cache_path, payload)
    return payload


def parse_repo_url(url: str) -> tuple[str, str, str] | None:
    """Extract (host, owner, repo) from a Git forge repository URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if host not in {"github.com", "gitlab.com", "codeberg.org"}:
        return None
    # Reject URLs that clearly don't point to a clonable repo root: issues,
    # pull requests, releases, wikis, raw file URLs.
    if len(parts) >= 3 and parts[2] in {
        "issues",
        "pull",
        "pulls",
        "releases",
        "wiki",
        "discussions",
        "actions",
        "projects",
    }:
        return None
    return host, owner, repo


def canonical_repo_url(url: str) -> str:
    """Normalize any forge URL down to its clonable repo root.

    Turns ``https://github.com/foo/bar/tree/main/sub`` or
    ``https://github.com/foo/bar/blob/main/file.png`` into
    ``https://github.com/foo/bar``. Returns empty string if the URL doesn't
    point to a supported forge repo.
    """
    parsed = parse_repo_url(url)
    if parsed is None:
        return ""
    host, owner, repo = parsed
    return f"https://{host}/{owner}/{repo}"


def api_repo_metadata(repo_url: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Fetch repository metadata from GitHub, GitLab, or Codeberg."""
    parsed = parse_repo_url(repo_url)
    if parsed is None:
        return None
    host, owner, repo = parsed
    cache_path = repo_cache_path(repo_url)
    if cache_path.exists() and not refresh:
        return load_json(cache_path, default={})

    if host == "github.com":
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
    elif host == "gitlab.com":
        project = quote(f"{owner}/{repo}", safe="")
        api_url = f"https://gitlab.com/api/v4/projects/{project}"
    else:
        api_url = f"https://codeberg.org/api/v1/repos/{owner}/{repo}"

    try:
        payload = request_json(api_url)
    except requests.RequestException:
        return None
    write_json(cache_path, payload)
    return payload


def extract_license_spdx(metadata: dict[str, Any] | None) -> str | None:
    """Normalize a repository license from forge metadata."""
    if not metadata:
        return None
    license_info = metadata.get("license")
    if isinstance(license_info, dict):
        candidates = [
            license_info.get("spdx_id"),
            license_info.get("key"),
            license_info.get("nickname"),
            license_info.get("name"),
        ]
    else:
        candidates = [metadata.get("license_spdx_id"), metadata.get("license_name"), license_info]
    for value in candidates:
        if not value:
            continue
        text = str(value).strip()
        upper = text.upper()
        if upper in ALLOWED_LICENSES or upper.startswith(ALLOWED_LICENSE_PREFIXES):
            return upper
        if upper in {"MIT LICENSE", "MIT"}:
            return "MIT"
        if "APACHE" in upper:
            return "Apache-2.0"
        if upper.startswith("BSD"):
            return "BSD-3-Clause"
        if "CC0" in upper:
            return "CC0-1.0"
    return None


def extract_repo_activity_date(metadata: dict[str, Any] | None) -> str | None:
    """Pull the best available 'last updated' date from repo metadata."""
    if not metadata:
        return None
    for key in ("pushed_at", "updated_at", "last_activity_at", "created_at"):
        parsed = parse_date(metadata.get(key))
        if parsed:
            return parsed.isoformat()
    return None


def extract_repo_stars(metadata: dict[str, Any] | None) -> int:
    """Return the repo's social proof metric."""
    if not metadata:
        return 0
    for key in ("stargazers_count", "star_count", "stars_count"):
        value = metadata.get(key)
        if isinstance(value, int):
            return value
    return 0


def find_repo_urls(text: str) -> list[str]:
    """Extract candidate dotfile repository links from free-form text."""
    if not text:
        return []
    matches = re.findall(r"https?://[^\s)>\"]+", text)
    urls: list[str] = []
    for match in matches:
        cleaned = match.split("](", 1)[0].rstrip(".,]")
        if parse_repo_url(cleaned):
            urls.append(cleaned)
    return list(dict.fromkeys(urls))


def detect_de(text: str) -> str | None:
    """Identify the desktop environment/window manager from text."""
    for de, markers in DE_KEYWORDS.items():
        if any(_contains_phrase(text, marker) for marker in markers):
            return de
    return None


def _contains_phrase(text: str, phrase: str) -> bool:
    pattern = re.escape(phrase).replace(r"\ ", r"[\s._+-]+")
    return bool(re.search(rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])", text, re.IGNORECASE))


def _keyword_match(text: str, keywords: tuple[str, ...]) -> str | None:
    for keyword in keywords:
        if _contains_phrase(text, keyword):
            return _normalize_detected_value(keyword)
    return None


def _normalize_detected_value(value: str) -> str | None:
    text = value.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = text.strip("`'\"()[]{}<>.,;:/\\|")
    text = text.replace("_", " ")
    text = re.sub(r"\s+", "-", text)
    if not text or text in GENERIC_TOKEN_VALUES:
        return None
    if len(text) < 2 or len(text) > 48:
        return None
    if not re.fullmatch(r"[a-z0-9.+-]+", text):
        return None
    if not re.search(r"[a-z]", text):
        return None
    return text


def detect_theme(text: str) -> str | None:
    """Detect a named theme from text."""
    explicit = THEME_REGEX.search(text)
    if explicit:
        return _normalize_detected_value(explicit.group(1))
    return _keyword_match(text, THEME_KEYWORDS)


def detect_icons(text: str) -> str | None:
    """Detect a named icon pack or cursor family from text."""
    explicit = ICON_REGEX.search(text)
    if explicit:
        return _normalize_detected_value(explicit.group(1))
    return _keyword_match(text, ICON_KEYWORDS)


def detect_font(text: str) -> str | None:
    """Detect a named UI/mono font from text."""
    explicit = FONT_REGEX.search(text)
    if explicit:
        return _normalize_detected_value(explicit.group(1))
    return _keyword_match(text, FONT_KEYWORDS)


def detect_named_design_tokens(text: str) -> dict[str, str]:
    """Return any theme/icon/font names explicitly mentioned in the text."""
    found: dict[str, str] = {}
    theme = detect_theme(text)
    icons = detect_icons(text)
    font = detect_font(text)
    if theme:
        found["theme"] = theme
    if icons:
        found["icons"] = icons
    if font:
        found["font"] = font
    return found


def reddit_threshold(scores: list[int]) -> float:
    """Compute median + 1 stdev without depending on numpy."""
    if not scores:
        return math.inf
    center = median(scores)
    if len(scores) == 1:
        return float(center)
    mean = sum(scores) / len(scores)
    variance = sum((score - mean) ** 2 for score in scores) / len(scores)
    return float(center) + math.sqrt(variance)
