from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_builder():
    root = Path(__file__).resolve().parent.parent
    path = root / "tools" / "build_app_catalog.py"
    spec = importlib.util.spec_from_file_location("build_app_catalog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_catalog_builder_merges_existing_winget_and_curated_overrides(tmp_path):
    builder = _load_builder()
    existing = tmp_path / "existing.json"
    overrides = tmp_path / "overrides.json"
    winget_list = tmp_path / "winget-list.txt"

    existing.write_text(
        json.dumps(
            {
                "schema": 1,
                "entries": [
                    {
                        "id": "org.example.Foo",
                        "name": "Foo App",
                        "match_names": ["Foo App"],
                        "compatibility": "native",
                        "alternatives": ["Foo App"],
                        "source_hint": "flatpak",
                        "confidence": "medium",
                        "category": "utilities",
                        "notes": "seed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    overrides.write_text(
        json.dumps(
            {
                "schema": 1,
                "entries": [
                    {
                        "id": "override:foo",
                        "name": "Foo App",
                        "match_names": ["FooApp"],
                        "compatibility": "alternative",
                        "alternatives": ["Bar App"],
                        "source_hint": "alternative",
                        "confidence": "high",
                        "category": "utilities",
                        "notes": "override wins",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    winget_list.write_text(
        "Name                Id                      Version\n"
        "--------------------------------------------------\n"
        "Mystery Tool        Vendor.MysteryTool      1.2.3\n",
        encoding="utf-8",
    )

    payload = builder.build_catalog(
        existing_path=existing,
        overrides_path=overrides,
        winget_lists=[winget_list],
        fetch_flathub=False,
        flathub_limit=10,
    )
    entries = {entry["name"]: entry for entry in payload["entries"]}

    assert entries["Foo App"]["compatibility"] == "alternative"
    assert entries["Foo App"]["confidence"] == "high"
    assert "FooApp" in entries["Foo App"]["match_names"]
    assert entries["Mystery Tool"]["source_hint"] == "needs_research"
    assert entries["Mystery Tool"]["confidence"] == "low"
