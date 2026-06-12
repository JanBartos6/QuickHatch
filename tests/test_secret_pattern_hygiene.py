from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 8 * 1024 * 1024
BINARY_EXTENSIONS = {
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".png",
    ".pyc",
    ".pyo",
    ".webp",
}
SCAN_ROOTS = [
    ".github",
    ".quickhatch/corpus-candidates",
    "docs",
    "scripts",
    "src",
    "tests",
]
SCAN_FILES = [
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
]

SECRET_PATTERNS = {
    "anthropic_api_key": re.compile(r"\bsk-ant-api\d{2}-[A-Za-z0-9_-]{40,}\b"),
    "openai_api_key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "github_token": re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"
        r"|\bgithub_pat_[A-Za-z0-9_]{80,}\b"
    ),
    "huggingface_token": re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "stripe_key": re.compile(r"\b(?:sk|pk)_(?:live|test)_[0-9A-Za-z]{20,}\b"),
}


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SCAN_ROOTS:
        root = ROOT / root_name
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())
    files.extend(ROOT / file_name for file_name in SCAN_FILES)
    return files


def _is_scannable(path: Path) -> bool:
    if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in path.parts):
        return False
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return False
    try:
        return path.stat().st_size <= MAX_SCAN_BYTES
    except OSError:
        return False


def test_release_sources_do_not_contain_real_looking_secret_patterns():
    findings: list[str] = []
    for path in _candidate_files():
        if not _is_scannable(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_number, line in enumerate(text.splitlines(), 1):
            for pattern_name, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    rel_path = path.relative_to(ROOT).as_posix()
                    findings.append(f"{rel_path}:{line_number}:{pattern_name}")

    assert findings == []
