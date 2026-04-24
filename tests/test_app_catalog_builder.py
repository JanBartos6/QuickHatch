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
        winget_searches=[],
        fetch_flathub=False,
        flathub_limit=10,
    )
    entries = {entry["name"]: entry for entry in payload["entries"]}

    assert entries["Foo App"]["compatibility"] == "alternative"
    assert entries["Foo App"]["confidence"] == "high"
    assert "FooApp" in entries["Foo App"]["match_names"]
    assert entries["Mystery Tool"]["source_hint"] == "needs_research"
    assert entries["Mystery Tool"]["confidence"] == "low"


def test_winget_parser_skips_truncated_wrapped_rows():
    builder = _load_builder()

    rows = builder.parse_winget_table_text(
        "Name                                      Id                     Version\n"
        "-----------------------------------------------------------------------\n"
        "Cisco Webex Network Recording Converter   Cisco.WebexConverter   1.0\n"
        "Cisco Webex Network Recording Conve… Cisco.WebexNetworkRecordingConverter   45.6\n"
    )

    assert [row.name for row in rows] == ["Cisco Webex Network Recording Converter"]


def test_catalog_loader_skips_truncated_generated_rows(tmp_path):
    builder = _load_builder()
    generated = tmp_path / "generated.json"
    overrides = tmp_path / "overrides.json"

    generated.write_text(
        json.dumps(
            {
                "schema": 1,
                "entries": [
                    {
                        "id": "winget:1.0",
                        "name": "Acronis Cyber Protect Home Off… Acronis.CyberProtectHomeOffice",
                    },
                    {"id": "winget:Valid.App", "name": "Valid App"},
                ],
            }
        ),
        encoding="utf-8",
    )
    overrides.write_text(json.dumps({"schema": 1, "entries": []}), encoding="utf-8")

    payload = builder.build_catalog(
        existing_path=generated,
        overrides_path=overrides,
        winget_lists=[],
        winget_searches=[],
        fetch_flathub=False,
        flathub_limit=10,
    )

    assert [entry["name"] for entry in payload["entries"]] == ["Valid App"]


def test_catalog_builder_preserves_curated_install_hints_and_evidence(tmp_path):
    builder = _load_builder()
    existing = tmp_path / "existing.json"
    overrides = tmp_path / "overrides.json"

    existing.write_text(json.dumps({"schema": 1, "entries": []}), encoding="utf-8")
    overrides.write_text(
        json.dumps(
            {
                "schema": 1,
                "entries": [
                    {
                        "id": "override:hinted",
                        "name": "Hinted App",
                        "match_names": ["Hinted App"],
                        "compatibility": "native",
                        "alternatives": ["Hinted App"],
                        "source_hint": "flatpak",
                        "confidence": "high",
                        "category": "utilities",
                        "notes": "Has distro hints.",
                        "install_hints": {
                            "arch": {
                                "source": "flatpak",
                                "packages": ["org.example.HintedApp"],
                                "notes": "Use Flathub.",
                            }
                        },
                        "evidence": [
                            {
                                "label": "Flathub",
                                "url": "https://flathub.org/apps/org.example.HintedApp",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = builder.build_catalog(
        existing_path=existing,
        overrides_path=overrides,
        winget_lists=[],
        winget_searches=[],
        fetch_flathub=False,
        flathub_limit=10,
    )

    entry = payload["entries"][0]
    assert entry["install_hints"]["arch"]["packages"] == ["org.example.HintedApp"]
    assert entry["evidence"][0]["label"] == "Flathub"
