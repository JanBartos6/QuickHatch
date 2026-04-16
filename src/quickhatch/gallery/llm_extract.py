"""LLM-powered metadata extraction for gallery recipes.

Replaces the keyword-regex approach in :func:`parse_repo_metadata` with a
structured LLM call that reads the README plus the highest-signal config
and package files, and returns grounded theme/icons/font data.

Uses LM Studio's OpenAI-compatible endpoint by default so the whole pipeline
can run offline. The default model and endpoint are overridable via env vars
for CI / alternate providers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from .common import USER_AGENT

LLM_CACHE_DIR = Path(".scrape-cache/llm")

# Markdown image syntax confuses the local model into generating endless
# ``<|image>`` tokens in its output. Strip it before prompting. We keep the
# alt text so the LLM still has the caption signal.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_HTML_IMAGE_RE = re.compile(r"<img[^>]*alt=[\"']([^\"']*)[\"'][^>]*>", re.IGNORECASE)
_HTML_IMAGE_BARE_RE = re.compile(r"<img[^>]*/?>", re.IGNORECASE)


def _strip_images(text: str) -> str:
    text = _MD_IMAGE_RE.sub(r"[image: \1]", text)
    text = _HTML_IMAGE_RE.sub(r"[image: \1]", text)
    text = _HTML_IMAGE_BARE_RE.sub("[image]", text)
    return text


LLM_ENDPOINT = os.environ.get(
    "QUICKHATCH_LLM_ENDPOINT", "http://localhost:1234/v1/chat/completions"
)
LLM_MODEL = os.environ.get("QUICKHATCH_LLM_MODEL", "google/gemma-4-26b-a4b")
LLM_TIMEOUT_SECONDS = int(os.environ.get("QUICKHATCH_LLM_TIMEOUT", "180"))

README_NAMES = ("README.md", "README", "readme.md", "Readme.md", "README.rst")
PACKAGE_MANIFEST_GLOBS = (
    "pkglist*",
    "aurlist*",
    "packages*",
    "Brewfile",
    "install.sh",
    "setup.sh",
    "flake.nix",
    "configuration.nix",
    "home.nix",
)
STYLE_CONFIG_GLOBS = (
    ".config/gtk-3.0/settings.ini",
    ".config/gtk-4.0/settings.ini",
    "gtk-3.0/settings.ini",
    "gtk-4.0/settings.ini",
    ".gtkrc-2.0",
    ".config/hypr/hyprland.conf",
    "hypr/hyprland.conf",
    "hyprland.conf",
    ".config/waybar/style.css",
    "waybar/style.css",
    ".config/kitty/kitty.conf",
    "kitty/kitty.conf",
    "kitty.conf",
    ".config/alacritty/alacritty.toml",
    "alacritty/alacritty.toml",
    "alacritty.toml",
    ".config/alacritty/alacritty.yml",
    "alacritty/alacritty.yml",
    ".config/qt5ct/qt5ct.conf",
    ".config/qt6ct/qt6ct.conf",
    ".config/kdeglobals",
    "kdeglobals",
)

MAX_CONTEXT_CHARS = 14000
MAX_README_CHARS = 8000
MAX_FILE_CHARS = 2500

EXTRACTION_SCHEMA: dict[str, Any] = {
    "name": "recipe_metadata",
    "schema": {
        "type": "object",
        "properties": {
            "is_dotfiles": {
                "type": "boolean",
                "description": (
                    "True if this repo configures a Linux desktop (dotfiles, rice). "
                    "False for tools, frameworks, neovim distros, applications."
                ),
            },
            "rejection_reason": {
                "type": ["string", "null"],
                "description": "Short explanation when is_dotfiles is false; null otherwise.",
            },
            "de": {
                "type": ["string", "null"],
                "description": (
                    "Primary desktop environment / window manager: "
                    "hyprland, plasma, gnome, i3, sway, xfce, cosmic, bspwm, "
                    "awesome, dwm, cinnamon, lxqt, openbox, river. "
                    "Null if not clearly identifiable."
                ),
            },
            "theme": {
                "type": ["string", "null"],
                "description": (
                    "GTK / overall theme name as a kebab-case token, "
                    'e.g. "catppuccin-mocha", "nord", "tokyonight", "gruvbox-dark". '
                    "Prefer gtk-theme-name= or package-list evidence over README prose."
                ),
            },
            "icons": {
                "type": ["string", "null"],
                "description": (
                    'Icon pack as a kebab-case token, e.g. "papirus-dark", "tela", '
                    '"whitesur". Prefer GTK settings or package-list evidence.'
                ),
            },
            "cursor": {
                "type": ["string", "null"],
                "description": "Cursor theme name, e.g. Bibata-Modern-Ice.",
            },
            "font_ui": {
                "type": ["string", "null"],
                "description": 'UI font family, e.g. "Inter", "Cantarell", "SF Pro".',
            },
            "font_mono": {
                "type": ["string", "null"],
                "description": (
                    'Monospace font family, e.g. "JetBrainsMono Nerd Font", '
                    '"FiraCode Nerd Font", "Iosevka".'
                ),
            },
            "wallpaper_hint": {
                "type": ["string", "null"],
                "description": 'Short mood descriptor, e.g. "aurora", "forest-abstract".',
            },
            "mood": {
                "type": ["string", "null"],
                "enum": [None, "dark", "light", "mixed"],
                "description": "Overall light/dark mood.",
            },
            "signature_features": {
                "type": "array",
                "items": {"type": "string", "maxLength": 40},
                "maxItems": 5,
                "description": (
                    "Up to 5 short tokens describing signature visual choices: "
                    '"centered dock", "waybar", "quickshell", "blur", "rounded corners".'
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Confidence in the extraction overall.",
            },
        },
        "required": [
            "is_dotfiles",
            "rejection_reason",
            "de",
            "theme",
            "icons",
            "cursor",
            "font_ui",
            "font_mono",
            "wallpaper_hint",
            "mood",
            "signature_features",
            "confidence",
        ],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """You extract structured metadata from Linux dotfiles repositories.

Your goal is to determine whether the repo configures a full Linux desktop
(dotfiles / "rice") and, if so, which desktop environment, theme, icon pack,
cursor, UI font, and monospace font it uses.

GROUND RULES
- Evidence priority (highest to lowest):
  1. Exact setting lines: gtk-theme-name=, gtk-icon-theme-name=,
     font-family=, font_family=, icon-theme=, cursor-theme-name=.
  2. Package manifests (pkglist, aurlist, Brewfile, install.sh, flake.nix):
     packages like papirus-icon-theme, ttf-jetbrains-mono-nerd,
     catppuccin-gtk-theme-mocha map directly to icons/fonts/theme.
  3. README prose (weakest). Use only when 1-2 have nothing.
- Return null for any field you cannot ground in the provided text.
- Prefer concrete names over umbrella categories: "catppuccin-mocha-lavender"
  beats "catppuccin"; "JetBrainsMono Nerd Font" beats "nerd font".
- Lowercase kebab-case for theme/icons/cursor tokens; font names keep their
  canonical capitalization ("Inter", "JetBrainsMono Nerd Font").
- Reject these as is_dotfiles=false (with a short rejection_reason):
  * Dotfile management tools (chezmoi, yadm, mackup, rcm).
  * Nix framework repos without a personal config (home-manager itself).
  * Neovim-only distributions (NvChad, LazyVim, AstroNvim).
  * Library / widget frameworks (fabric, quickshell core, ags).
  * Terminal-only configurations (bar.wezterm, tmux themes).
  * Single-app configs (spicetify, firefox userchrome).
  * Issue threads, releases, wiki pages, blobs with no surrounding repo.
- Multi-DE rices: pick the most featured DE as the primary de; mention the
  others in signature_features.
"""


def _read_text(path: Path, limit: int, *, strip_images: bool = False) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if strip_images:
        text = _strip_images(text)
    if len(text) > limit:
        text = text[:limit] + f"\n... [truncated at {limit} chars]"
    return text


def _collect_readme(repo_dir: Path) -> tuple[str, str]:
    for name in README_NAMES:
        path = repo_dir / name
        if path.exists():
            return name, _read_text(path, MAX_README_CHARS, strip_images=True)
    return "", ""


def _collect_manifests(repo_dir: Path) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for pattern in PACKAGE_MANIFEST_GLOBS:
        for path in sorted(repo_dir.glob(pattern)):
            if path.is_file():
                files.append((str(path.relative_to(repo_dir)), _read_text(path, MAX_FILE_CHARS)))
        # Also look one directory deep for misplaced lists.
        for path in sorted(repo_dir.glob(f"*/{pattern}")):
            if path.is_file():
                files.append((str(path.relative_to(repo_dir)), _read_text(path, MAX_FILE_CHARS)))
    return files


def _collect_style_configs(repo_dir: Path) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for rel in STYLE_CONFIG_GLOBS:
        path = repo_dir / rel
        if path.exists() and path.is_file():
            files.append((rel, _read_text(path, MAX_FILE_CHARS)))
    return files


def assemble_context(repo_dir: Path, title: str = "", body: str = "") -> str:
    """Build the LLM context: README + manifests + key style configs."""
    parts: list[str] = []
    if title or body:
        parts.append(f"# POST METADATA\n\ntitle: {title}\nbody: {body}\n")

    readme_name, readme = _collect_readme(repo_dir)
    if readme:
        parts.append(f"# {readme_name}\n\n{readme}\n")

    for rel, content in _collect_manifests(repo_dir):
        if content.strip():
            parts.append(f"# {rel}\n\n{content}\n")

    for rel, content in _collect_style_configs(repo_dir):
        if content.strip():
            parts.append(f"# {rel}\n\n{content}\n")

    context = "\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + f"\n... [truncated at {MAX_CONTEXT_CHARS} chars]"
    return context


# Strings the LLM sometimes emits as field values that should be treated as
# "no value" even though the JSON schema admits them (the schema just says
# "string or null", not "a real token").
_SANITIZE_NULL_TOKENS = frozenset(
    {
        "",
        "null",
        "none",
        "n/a",
        "na",
        "unknown",
        "default",
        "system",
        "system-ui",  # CSS fallback, not a real font identity
        "sans-serif",
        "serif",
        "monospace",
        "normal",
        "regular",
        "true",
        "false",
        "-",
        "_",
        ".",
        "—",
        "__proto__",  # JavaScript prototype pollution string, Gemma hallucination
        "prototype",
    }
)
# Characters that indicate the model leaked JSON syntax into a field value
# (e.g. font_ui = ":{" or font_mono = "} , {").
_SANITIZE_BAD_CHARS = frozenset('{}[]"<>')
_SANITIZE_MAX_LEN = 60


def _sanitize_field(value: Any) -> str | None:
    """Return a cleaned field value, or ``None`` if the value is meaningless.

    Catches Gemma hallucinations like ":{", "__proto__", literal-string
    "null", truncated fragments ("settt_mono_font_font-fa"), and similar
    schema-conforming-but-garbage output.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("`'\"")
    if not cleaned:
        return None
    if cleaned.lower() in _SANITIZE_NULL_TOKENS:
        return None
    if len(cleaned) > _SANITIZE_MAX_LEN:
        return None
    if any(ch in _SANITIZE_BAD_CHARS for ch in cleaned):
        return None
    # Require at least one letter so tokens like ":{" or ",," drop out even
    # if the bad-chars check missed them.
    if not any(ch.isalpha() for ch in cleaned):
        return None
    return cleaned


def sanitize_llm_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a cached LLM extraction result, filtering hallucinated fields."""
    if result is None:
        return None
    cleaned = dict(result)
    for field in ("de", "theme", "icons", "cursor", "font_ui", "font_mono", "wallpaper_hint"):
        cleaned[field] = _sanitize_field(cleaned.get(field))
    # signature_features: drop empty/garbage entries, cap length.
    sig = cleaned.get("signature_features") or []
    if isinstance(sig, list):
        cleaned["signature_features"] = [s for s in (_sanitize_field(item) for item in sig) if s][
            :5
        ]
    else:
        cleaned["signature_features"] = []
    return cleaned


def call_llm(context: str) -> dict[str, Any] | None:
    """Send the assembled context to the LLM and parse its JSON response."""
    if not context.strip():
        return None
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        "temperature": 0.0,
        "max_tokens": 350,
        "response_format": {"type": "json_schema", "json_schema": EXTRACTION_SCHEMA},
    }
    try:
        response = requests.post(
            LLM_ENDPOINT,
            json=payload,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=LLM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
    except (KeyError, ValueError, TypeError):
        return None
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _cache_key(repo_dir: Path, context: str) -> str:
    digest = hashlib.sha1(context.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{repo_dir.name}-{digest}.json"


def llm_extract_metadata(
    repo_dir: Path, candidate: dict[str, Any], *, refresh: bool = False
) -> dict[str, Any] | None:
    """Run LLM extraction on a cloned repo. Returns None on failure.

    Results are cached under ``.scrape-cache/llm/`` keyed by repo name +
    context hash so re-running the pipeline doesn't repeat LLM calls.

    On success returns a dict matching :data:`EXTRACTION_SCHEMA`. Callers
    should treat ``is_dotfiles=False`` as a hard rejection of the candidate.
    """
    context = assemble_context(
        repo_dir,
        title=candidate.get("title", ""),
        body=candidate.get("body", ""),
    )
    if not context.strip():
        return None
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = LLM_CACHE_DIR / _cache_key(repo_dir, context)
    if cache_path.exists() and not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return sanitize_llm_result(cached)
        except (OSError, json.JSONDecodeError):
            pass
    result = call_llm(context)
    if result is not None:
        # Cache the raw model output so the sanitizer can be improved later
        # without needing to re-call the LLM, but return the sanitized view.
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return sanitize_llm_result(result)
    return None


__all__ = [
    "EXTRACTION_SCHEMA",
    "LLM_ENDPOINT",
    "LLM_MODEL",
    "assemble_context",
    "call_llm",
    "llm_extract_metadata",
]


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dry-run LLM extraction on a single cloned repo.")
    parser.add_argument("repo_dir", type=Path)
    args = parser.parse_args()

    result = llm_extract_metadata(args.repo_dir, {"title": "", "body": ""})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
