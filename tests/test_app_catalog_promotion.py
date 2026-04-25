from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_promoter():
    root = Path(__file__).resolve().parent.parent
    path = root / "tools" / "promote_app_catalog_suggestions.py"
    spec = importlib.util.spec_from_file_location("promote_app_catalog_suggestions", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_promotes_only_high_confidence_safe_suggestions(tmp_path):
    promoter = _load_promoter()
    overrides = tmp_path / "overrides.json"
    suggestions = tmp_path / "app_catalog.agent_suggestions.test.json"

    _write_json(overrides, {"schema": 1, "entries": []})
    _write_json(
        suggestions,
        {
            "schema": 1,
            "entries": [
                {
                    "id": "winget:Vendor.SafeApp",
                    "name": "Safe App",
                    "match_names": ["Safe App"],
                    "compatibility": "native",
                    "alternatives": ["Safe App"],
                    "source_hint": "flatpak",
                    "confidence": "high",
                    "category": "utilities",
                    "notes": "Verified native Linux package.",
                    "install_hints": {
                        "arch": {
                            "source": "flatpak",
                            "packages": ["org.example.SafeApp", "bad;command"],
                            "notes": "Install from Flathub.",
                        }
                    },
                    "evidence": [
                        {"label": "Flathub", "url": "https://flathub.org/apps/org.example.SafeApp"}
                    ],
                },
                {
                    "id": "winget:Vendor.LowApp",
                    "name": "Low App",
                    "compatibility": "native",
                    "alternatives": ["Low App"],
                    "source_hint": "repo",
                    "confidence": "low",
                    "evidence": [{"label": "Repo", "url": "https://example.com/low"}],
                },
                {
                    "id": "winget:Vendor.UnknownApp",
                    "name": "Unknown App",
                    "compatibility": "unknown",
                    "alternatives": [],
                    "source_hint": "needs_research",
                    "confidence": "high",
                    "evidence": [{"label": "Unknown", "url": "https://example.com/unknown"}],
                },
                {
                    "id": "winget:Vendor.NoEvidence",
                    "name": "No Evidence",
                    "compatibility": "native",
                    "alternatives": ["No Evidence"],
                    "source_hint": "repo",
                    "confidence": "high",
                },
                {
                    "id": "winget:Vendor.Truncated",
                    "name": "Broken App… Vendor.Truncated",
                    "compatibility": "native",
                    "alternatives": ["Broken App"],
                    "source_hint": "repo",
                    "confidence": "high",
                    "evidence": [{"label": "Repo", "url": "https://example.com/broken"}],
                },
            ],
        },
    )

    payload, stats = promoter.promote_suggestions(
        overrides_path=overrides,
        suggestion_paths=[suggestions],
        min_confidence="high",
        require_evidence=True,
    )

    assert stats.promoted == 1
    assert [entry["name"] for entry in payload["entries"]] == ["Safe App"]
    promoted = payload["entries"][0]
    assert promoted["install_hints"]["arch"]["packages"] == ["org.example.SafeApp"]
    assert promoted["evidence"][0]["url"].startswith("https://")
    assert promoted["promoted_from_agent_suggestions"] is True


def test_promotion_skips_existing_override_aliases(tmp_path):
    promoter = _load_promoter()
    overrides = tmp_path / "overrides.json"
    suggestions = tmp_path / "app_catalog.agent_suggestions.test.json"

    _write_json(
        overrides,
        {
            "schema": 1,
            "entries": [
                {
                    "id": "override:existing",
                    "name": "Existing App",
                    "match_names": ["Existing Alias"],
                    "compatibility": "alternative",
                    "alternatives": ["Alternative"],
                    "source_hint": "alternative",
                    "confidence": "high",
                    "category": "utilities",
                    "notes": "Already curated.",
                }
            ],
        },
    )
    _write_json(
        suggestions,
        {
            "schema": 1,
            "entries": [
                {
                    "id": "winget:Vendor.ExistingAlias",
                    "name": "Existing Alias",
                    "compatibility": "native",
                    "alternatives": ["Existing Alias"],
                    "source_hint": "repo",
                    "confidence": "high",
                    "evidence": [{"label": "Repo", "url": "https://example.com/existing"}],
                }
            ],
        },
    )

    payload, stats = promoter.promote_suggestions(
        overrides_path=overrides,
        suggestion_paths=[suggestions],
    )

    assert stats.promoted == 0
    assert stats.skipped_existing == 1
    assert [entry["name"] for entry in payload["entries"]] == ["Existing App"]


def test_promotion_skips_strong_existing_generated_catalog_aliases(tmp_path):
    promoter = _load_promoter()
    overrides = tmp_path / "overrides.json"
    generated = tmp_path / "generated.json"
    suggestions = tmp_path / "app_catalog.agent_suggestions.test.json"

    _write_json(overrides, {"schema": 1, "entries": []})
    _write_json(
        generated,
        {
            "schema": 1,
            "entries": [
                {
                    "id": "org.mozilla.firefox",
                    "name": "Firefox",
                    "match_names": ["Mozilla Firefox"],
                    "compatibility": "native",
                    "alternatives": ["Firefox"],
                    "source_hint": "flatpak",
                    "confidence": "medium",
                }
            ],
        },
    )
    _write_json(
        suggestions,
        {
            "schema": 1,
            "entries": [
                {
                    "id": "winget:Mozilla.Firefox",
                    "name": "Mozilla Firefox",
                    "compatibility": "native",
                    "alternatives": ["Firefox"],
                    "source_hint": "repo",
                    "confidence": "high",
                    "install_hints": {
                        "arch": {"source": "repo", "packages": ["firefox"], "notes": ""}
                    },
                    "evidence": [{"label": "Repo", "url": "https://example.com/firefox"}],
                }
            ],
        },
    )

    payload, stats = promoter.promote_suggestions(
        overrides_path=overrides,
        suggestion_paths=[suggestions],
        existing_catalog_paths=[generated],
        legacy_path=None,
    )

    assert stats.promoted == 0
    assert stats.skipped_existing == 1
    assert payload["entries"] == []


def test_promotion_requires_install_hints_for_installable_sources(tmp_path):
    promoter = _load_promoter()
    overrides = tmp_path / "overrides.json"
    suggestions = tmp_path / "app_catalog.agent_suggestions.test.json"

    _write_json(overrides, {"schema": 1, "entries": []})
    _write_json(
        suggestions,
        {
            "schema": 1,
            "entries": [
                {
                    "id": "winget:Vendor.NoHint",
                    "name": "No Hint App",
                    "compatibility": "native",
                    "alternatives": ["No Hint App"],
                    "source_hint": "repo",
                    "confidence": "high",
                    "evidence": [{"label": "Repo", "url": "https://example.com/nohint"}],
                }
            ],
        },
    )

    payload, stats = promoter.promote_suggestions(
        overrides_path=overrides,
        suggestion_paths=[suggestions],
        existing_catalog_paths=[],
        legacy_path=None,
    )

    assert stats.promoted == 0
    assert stats.skipped_invalid == 1
    assert payload["entries"] == []
