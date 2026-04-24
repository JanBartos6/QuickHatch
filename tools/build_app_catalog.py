"""Build QuickHatch's local app compatibility catalog.

The generated catalog is used at runtime to classify scanned Windows
applications without model calls. This tool is intentionally conservative:
public package indexes provide candidate names, while high-confidence Linux
compatibility and unsupported-app decisions live in app_catalog.overrides.json.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "src" / "quickhatch" / "data"
FLATHUB_POPULAR_URL = "https://flathub.org/api/v2/collection/popular"
WINGET_BROAD_SEARCH_QUERIES = ("a", "e", "i", "o", "u", "s", "m", "c", "d", "p", "t", "g", "r", "l")


@dataclass
class CatalogRow:
    id: str
    name: str
    match_names: list[str] = field(default_factory=list)
    compatibility: str = "unknown"
    alternatives: list[str] = field(default_factory=list)
    source_hint: str = "unknown"
    confidence: str = "low"
    category: str = "other"
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "match_names": _dedupe([self.name, *self.match_names]),
            "compatibility": self.compatibility,
            "alternatives": _dedupe(self.alternatives or [self.name]),
            "source_hint": self.source_hint,
            "confidence": self.confidence,
            "category": self.category,
            "notes": self.notes,
        }


def _normalize(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        norm = _normalize(clean)
        if clean and norm not in seen:
            result.append(clean)
            seen.add(norm)
    return result


def _category_from_text(name: str, app_id: str = "", categories: list[str] | None = None) -> str:
    text = " ".join([name, app_id, *(categories or [])]).lower()
    checks = [
        ("media", r"video|audio|music|media|player|stream|obs|vlc|spotify|podcast|editor"),
        (
            "development",
            r"develop|\bcode\b|programming|\bgit\b|\bide\b|terminal|database|api|"
            r"docker|kubernetes|\bjdk\b|\bjre\b|\bjava\b|netbeans|compiler|\bsdk\b",
        ),
        ("browsers", r"browser|web|chrome|firefox|\bedge\b|brave|chromium|librewolf|zen"),
        ("communication", r"chat|mail|messag|discord|slack|teams|telegram|signal|zoom|matrix"),
        ("gaming-tools", r"game|steam|proton|lutris|heroic|bottles|launcher|minecraft"),
        ("graphics", r"graphics|photo|image|paint|vector|drawing|krita|gimp|inkscape|blender"),
        ("office", r"office|document|pdf|notes|markdown|calendar|spreadsheet|presentation"),
        ("security", r"password|vpn|security|auth|bitwarden|keepass|firewall"),
        ("utilities", r"utility|tool|system|backup|sync|file|archive|clipboard|monitor"),
    ]
    for category, pattern in checks:
        if re.search(pattern, text):
            return category
    return "other"


def _row_from_mapping(raw: dict[str, Any]) -> CatalogRow | None:
    name = str(raw.get("name") or "").strip()
    app_id = str(raw.get("id") or name).strip()
    if not name or not app_id:
        return None
    if "…" in name or "…" in app_id:
        return None
    return CatalogRow(
        id=app_id,
        name=name,
        match_names=[str(v).strip() for v in raw.get("match_names", []) if str(v).strip()],
        compatibility=str(raw.get("compatibility") or "unknown"),
        alternatives=[str(v).strip() for v in raw.get("alternatives", []) if str(v).strip()],
        source_hint=str(raw.get("source_hint") or "unknown"),
        confidence=str(raw.get("confidence") or "low"),
        category=str(raw.get("category") or "other"),
        notes=str(raw.get("notes") or ""),
    )


def load_catalog_file(path: Path) -> list[CatalogRow]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[CatalogRow] = []
    for raw in payload.get("entries", []):
        if isinstance(raw, dict):
            row = _row_from_mapping(raw)
            if row:
                rows.append(row)
    return rows


def fetch_flathub_popular(limit: int = 500) -> list[CatalogRow]:
    rows: list[CatalogRow] = []
    request = urllib.request.Request(
        FLATHUB_POPULAR_URL,
        headers={"User-Agent": "QuickHatch catalog builder"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    for item in payload.get("hits", []):
        if len(rows) >= limit:
            break
        name = str(item.get("name") or "").strip()
        app_id = str(item.get("id") or item.get("app_id") or "").strip()
        if not name or not app_id:
            continue
        categories = [str(v) for v in item.get("main_categories") or []]
        rows.append(
            CatalogRow(
                id=app_id,
                name=name,
                match_names=[name],
                compatibility="native",
                alternatives=[name],
                source_hint="flatpak",
                confidence="medium",
                category=_category_from_text(name, app_id, categories),
                notes="Popular Flathub app; exact distro package names are resolved later.",
            )
        )
    return rows


def parse_winget_table_text(text: str) -> list[CatalogRow]:
    """Parse winget list/search table output as low-confidence candidates."""
    rows: list[CatalogRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("-")
            or "…" in line
            or line.lower().startswith(("name ", "winget ", "no package", "the source"))
        ):
            continue
        # winget list is column-aligned. The package identifier is usually the
        # first token containing a dot after at least two spaces.
        parts = re.split(r"\s{2,}", line)
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        package_id = next((p.strip() for p in parts[1:] if "." in p and " " not in p), "")
        if not name or len(name) < 2:
            continue
        rows.append(
            CatalogRow(
                id=f"winget:{package_id or _normalize(name).replace(' ', '-')}",
                name=name,
                match_names=[name, package_id] if package_id else [name],
                compatibility="unknown",
                alternatives=[],
                source_hint="needs_research",
                confidence="low",
                category=_category_from_text(name, package_id),
                notes="Discovered from winget list; add a curated override before relying on it.",
            )
        )
    return rows


def parse_winget_list(path: Path) -> list[CatalogRow]:
    """Parse a saved winget list/search output as low-confidence catalog candidates."""

    return parse_winget_table_text(path.read_text(encoding="utf-8", errors="ignore"))


def run_winget_search(query: str, count: int = 1000) -> list[CatalogRow]:
    """Run `winget search` and convert the result table into catalog candidates."""

    completed = subprocess.run(
        [
            "winget",
            "search",
            query,
            "--source",
            "winget",
            "--accept-source-agreements",
            "--disable-interactivity",
            "--count",
            str(count),
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )
    text = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode not in (0, 1):
        print(f"warning: winget search {query!r} exited {completed.returncode}", file=sys.stderr)
    return parse_winget_table_text(text)


def merge_rows(groups: list[list[CatalogRow]]) -> list[CatalogRow]:
    merged: dict[str, CatalogRow] = {}
    aliases: dict[str, str] = {}

    for group in groups:
        for row in group:
            names = _dedupe([row.name, *row.match_names])
            key = aliases.get(_normalize(row.name)) or _normalize(row.name)
            for name in names:
                alias = _normalize(name)
                if alias in aliases:
                    key = aliases[alias]
                    break
            existing = merged.get(key)
            if existing is None:
                merged[key] = row
                for name in names:
                    aliases[_normalize(name)] = key
                continue

            # Later groups are more authoritative. Preserve aliases learned
            # from earlier sources while replacing the compatibility decision.
            row.match_names = _dedupe([*existing.match_names, existing.name, *row.match_names])
            if not row.alternatives:
                row.alternatives = existing.alternatives
            merged[key] = row
            for name in [row.name, *row.match_names]:
                aliases[_normalize(name)] = key

    return sorted(merged.values(), key=lambda r: (_category_sort_key(r.category), r.name.lower()))


def _category_sort_key(category: str) -> int:
    order = [
        "browsers",
        "communication",
        "development",
        "gaming-tools",
        "graphics",
        "media",
        "office",
        "security",
        "system",
        "utilities",
        "creative",
        "other",
    ]
    try:
        return order.index(category)
    except ValueError:
        return len(order)


def build_catalog(
    *,
    existing_path: Path,
    overrides_path: Path,
    winget_lists: list[Path],
    winget_searches: list[str],
    fetch_flathub: bool,
    flathub_limit: int,
) -> dict[str, Any]:
    source_groups: list[list[CatalogRow]] = []
    existing_rows = load_catalog_file(existing_path)
    if existing_rows:
        source_groups.append(existing_rows)
    if fetch_flathub:
        try:
            source_groups.append(fetch_flathub_popular(flathub_limit))
        except Exception as exc:  # noqa: BLE001 - catalog generation should degrade offline
            print(f"warning: Flathub fetch failed: {exc}", file=sys.stderr)
    for winget_list in winget_lists:
        source_groups.append(parse_winget_list(winget_list))
    for query in winget_searches:
        source_groups.append(run_winget_search(query))
    override_rows = load_catalog_file(overrides_path)
    if override_rows:
        source_groups.append(override_rows)

    entries = [row.to_json() for row in merge_rows(source_groups)]
    return {
        "schema": 1,
        "generated_on": datetime.now(UTC).date().isoformat(),
        "source": (
            "Generated by tools/build_app_catalog.py from existing generated data, "
            "Flathub popular apps when available, optional winget-list inputs, "
            "and QuickHatch curated overrides."
        ),
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DATA_DIR / "app_catalog.generated.json")
    parser.add_argument("--existing", type=Path, default=DATA_DIR / "app_catalog.generated.json")
    parser.add_argument("--overrides", type=Path, default=DATA_DIR / "app_catalog.overrides.json")
    parser.add_argument("--winget-list", type=Path, action="append", default=[])
    parser.add_argument(
        "--winget-search",
        action="append",
        default=[],
        help="Run winget search QUERY and import results as low-confidence candidates.",
    )
    parser.add_argument(
        "--winget-search-preset",
        choices=["broad"],
        help="Run a predefined broad set of winget searches to seed thousands of Windows app names.",
    )
    parser.add_argument("--no-network", action="store_true", help="Skip Flathub refresh.")
    parser.add_argument("--flathub-limit", type=int, default=500)
    args = parser.parse_args(argv)

    winget_searches = list(args.winget_search or [])
    if args.winget_search_preset == "broad":
        winget_searches.extend(WINGET_BROAD_SEARCH_QUERIES)

    payload = build_catalog(
        existing_path=args.existing,
        overrides_path=args.overrides,
        winget_lists=list(args.winget_list or []),
        winget_searches=winget_searches,
        fetch_flathub=not args.no_network,
        flathub_limit=args.flathub_limit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload['entries'])} catalog entries to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
