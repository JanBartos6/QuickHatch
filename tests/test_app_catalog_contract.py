from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomllib

from quickhatch.analysis import app_catalog


def _clear_catalog_caches() -> None:
    if hasattr(app_catalog.load_catalog, "cache_clear"):
        app_catalog.load_catalog.cache_clear()
    app_catalog._catalog_exact_index.cache_clear()
    app_catalog._catalog_phrase_candidates.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_catalog_caches():
    _clear_catalog_caches()
    yield
    _clear_catalog_caches()


def test_catalog_contract_documents_hint_precedence():
    contract = app_catalog.CATALOG_CONTRACT.lower()

    assert "best-effort hints" in contract
    assert "target repo/package-manager output overrides" in contract
    assert "provider/web search may override" in contract
    assert "missing catalog hints must not block setup" in contract
    assert "stale catalog hints must fall back" in contract


def test_runtime_loader_ignores_agent_suggestion_artifacts(monkeypatch, tmp_path):
    suggestion = tmp_path / "app_catalog.agent_suggestions.batch_test.json"
    suggestion.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "agent:only",
                        "name": "Agent Suggestion Only",
                        "compatibility": "native",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_catalog, "_data_dir", lambda: tmp_path)
    _clear_catalog_caches()

    assert app_catalog.load_catalog() == ()
    assert app_catalog.match_catalog_entry("Agent Suggestion Only") is None

    generated = tmp_path / "app_catalog.generated.json"
    generated.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "app:runtime",
                        "name": "Runtime App",
                        "compatibility": "native",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _clear_catalog_caches()

    assert [entry.name for entry in app_catalog.load_catalog()] == ["Runtime App"]


def test_unknown_app_context_is_research_hint_not_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(app_catalog, "_data_dir", lambda: tmp_path)
    _clear_catalog_caches()

    rows = app_catalog.selected_app_catalog_context(["Unknown Vendor Tool"], distro_family="arch")

    assert rows == [
        {
            "name": "Unknown Vendor Tool",
            "catalog_id": "",
            "canonical_name": "Unknown Vendor Tool",
            "compatibility": "unknown",
            "alternatives": [],
            "source_hint": "needs_research",
            "confidence": "low",
            "category": "unknown",
            "notes": "No curated catalog match; research before installing.",
            "install_hints": {},
            "evidence": [],
        }
    ]


def test_catalog_validation_reports_advisory_warnings_and_hint_samples(tmp_path):
    generated = tmp_path / "app_catalog.generated.json"
    generated.write_text(
        json.dumps(
            {
                "entries": [
                    {"id": "", "name": "", "compatibility": "native"},
                    {
                        "id": "app:native-empty",
                        "name": "Native Without Package",
                        "compatibility": "native",
                        "install_hints": {"arch": {"source": "repo", "packages": []}},
                    },
                    {
                        "id": "app:alternative-marked-native",
                        "name": "Alternative Marked Native",
                        "compatibility": "native",
                        "alternatives": ["Different Linux App"],
                        "install_hints": {"fedora": {"source": "repo", "packages": ["linux-app"]}},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    report = app_catalog.validate_runtime_catalog(data_dir=tmp_path, sample_limit=2)

    assert report["success"] is True
    assert report["warning_count"] == 3
    assert report["warnings"]["malformed_entries"]["count"] == 1
    assert report["warnings"]["native_without_package_hints"]["count"] == 1
    assert report["warnings"]["native_with_distinct_alternatives"]["count"] == 1
    assert report["install_hint_samples"]["fedora"]["samples"] == [
        {"name": "Alternative Marked Native", "source": "repo", "packages": ["linux-app"]}
    ]


def test_catalog_dev_artifacts_are_excluded_from_built_distributions():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    exclude = set(config["tool"]["hatch"]["build"]["exclude"])

    assert "src/quickhatch/data/app_catalog.agent_suggestions*.json" in exclude
    assert "src/quickhatch/data/app_catalog.research_batch*.json" in exclude
