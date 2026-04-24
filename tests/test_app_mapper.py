"""Tests for map_apps — Windows-to-Linux compatibility classifier."""

from __future__ import annotations

import pytest

from quickhatch.analysis.app_mapper import _contains_native_app, _fuzzy_match, map_apps
from quickhatch.config import AppInfo


class TestFuzzyMatch:
    def test_exact_match(self):
        assert _fuzzy_match("Adobe Photoshop", {"Adobe Photoshop": {}}) == "Adobe Photoshop"

    def test_substring_match_name_contains_key(self):
        # "Adobe Photoshop 2024" contains "Adobe Photoshop"
        assert _fuzzy_match("Adobe Photoshop 2024", {"Adobe Photoshop": {}}) == "Adobe Photoshop"

    def test_substring_match_key_contains_name(self):
        # "Photoshop" is contained in "Adobe Photoshop"
        assert _fuzzy_match("Photoshop", {"Adobe Photoshop": {}}) == "Adobe Photoshop"

    def test_no_match_returns_none(self):
        assert _fuzzy_match("Totally Unknown App 99", {"Firefox": {}}) is None

    def test_case_insensitive(self):
        assert _fuzzy_match("FIREFOX", {"firefox": {}}) == "firefox"


class TestNativeLinuxDetection:
    def test_firefox_is_native(self):
        apps = [AppInfo(name="Mozilla Firefox", source="winget")]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "native"
        assert (
            "natively" in (mapped[0].linux_alternative or "").lower()
            or "firefox" in (mapped[0].linux_alternative or "").lower()
        )

    def test_vscode_is_native(self):
        apps = [AppInfo(name="Visual Studio Code", source="winget")]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "native"

    def test_steam_is_native(self):
        apps = [AppInfo(name="Steam", source="registry")]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "native"

    def test_case_insensitive_matching(self):
        apps = [AppInfo(name="DISCORD", source="winget")]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "native"

    def test_whole_word_matching_allows_embedded_phrases(self):
        apps = [AppInfo(name="Telegram Desktop", source="winget")]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "native"

    def test_short_native_tokens_do_not_poison_longer_words(self):
        apps = [
            AppInfo(name="LegitApp", source="winget"),
            AppInfo(name="SignalRGB", source="registry"),
            AppInfo(name="Codec Pack", source="registry"),
            AppInfo(name="Nodepad", source="registry"),
        ]
        mapped = map_apps(apps)
        assert [app.compatibility for app in mapped] == [
            "unknown",
            "unknown",
            "unknown",
            "unknown",
        ]


class TestContainsNativeApp:
    def test_matches_whole_phrases(self):
        assert _contains_native_app("Visual Studio Code", {"visual studio code"})
        assert _contains_native_app("Telegram Desktop", {"telegram"})

    def test_rejects_partial_word_collisions(self):
        native_linux = {"git", "signal", "code", "node"}
        assert not _contains_native_app("GitHub Desktop", native_linux)
        assert not _contains_native_app("SignalRGB", native_linux)
        assert not _contains_native_app("Codec Pack", native_linux)
        assert not _contains_native_app("Nodepad", native_linux)


class TestUnknownApps:
    def test_truly_unknown_app_marked_unknown(self):
        apps = [AppInfo(name="Xyz Proprietary Enterprise Tool 9000", source="registry")]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "unknown"

    def test_unknown_has_no_linux_alternative_set(self):
        apps = [AppInfo(name="Xyz Proprietary Enterprise Tool 9000", source="registry")]
        mapped = map_apps(apps)
        # It was never set from the default None/empty
        assert not mapped[0].linux_alternative or mapped[0].linux_alternative == ""


class TestMultipleApps:
    def test_mixed_apps_classified_individually(self):
        apps = [
            AppInfo(name="Firefox", source="winget"),
            AppInfo(name="Xyz Proprietary Enterprise Tool", source="registry"),
            AppInfo(name="Steam", source="registry"),
        ]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "native"
        assert mapped[1].compatibility == "unknown"
        assert mapped[2].compatibility == "native"

    def test_empty_list_returns_empty_list(self):
        assert map_apps([]) == []

    def test_preserves_app_metadata(self):
        apps = [AppInfo(name="Firefox", version="121.0", source="winget")]
        mapped = map_apps(apps)
        assert mapped[0].version == "121.0"
        assert mapped[0].source == "winget"


class TestRealWorldSamples:
    """Apps that appear in the demo profile — they should all classify somehow
    without raising."""

    def test_demo_profile_apps(self):
        names = [
            "Google Chrome",
            "Mozilla Firefox",
            "Visual Studio Code",
            "Steam",
            "Discord",
            "Spotify",
            "Adobe Photoshop",
            "Notepad++",
            "OBS Studio",
            "VLC Media Player",
            "7-Zip",
            "Docker Desktop",
            "Microsoft Office",
            "Slack",
            "PuTTY",
            "WinSCP",
            "Blender",
            "Epic Games Store",
        ]
        apps = [AppInfo(name=n, source="winget") for n in names]
        mapped = map_apps(apps)
        assert len(mapped) == len(names)
        # Every app has a compatibility value set (never left at default beyond "unknown")
        for a in mapped:
            assert a.compatibility in {"native", "alternative", "wine", "none", "unknown"}

    def test_demo_profile_native_count(self):
        """Most of the demo apps should resolve to something other than 'unknown'."""
        names = [
            "Google Chrome",
            "Mozilla Firefox",
            "Visual Studio Code",
            "Steam",
            "Discord",
            "Spotify",
            "VLC Media Player",
            "Blender",
        ]
        apps = [AppInfo(name=n, source="winget") for n in names]
        mapped = map_apps(apps)
        recognized = [a for a in mapped if a.compatibility != "unknown"]
        # At least 75% should be recognized out of this hand-picked list
        assert len(recognized) >= len(names) * 0.75


class TestCuratedCatalog:
    def test_generated_catalog_has_large_popular_app_seed(self):
        from quickhatch.analysis.app_catalog import load_catalog

        generated = [entry for entry in load_catalog() if not entry.id.startswith("legacy:")]
        assert len(generated) >= 500

    def test_catalog_maps_popular_flathub_app_without_model_call(self):
        apps = [AppInfo(name="GitHub Desktop", source="winget")]
        mapped = map_apps(apps)
        assert mapped[0].compatibility == "native"
        assert "GitHub Desktop" in (mapped[0].linux_alternative or "")
        assert "source=flatpak_or_aur" in mapped[0].notes

    def test_catalog_maps_common_unsupported_windows_apps(self):
        apps = [
            AppInfo(name="Adobe Creative Cloud", source="winget"),
            AppInfo(name="Logitech G HUB", source="winget"),
            AppInfo(name="Microsoft OneDrive", source="winget"),
        ]

        mapped = map_apps(apps)
        by_name = {app.name: app for app in mapped}
        assert by_name["Adobe Creative Cloud"].compatibility == "none"
        assert by_name["Logitech G HUB"].compatibility == "alternative"
        assert by_name["Microsoft OneDrive"].compatibility == "alternative"
        assert "confidence=high" in by_name["Adobe Creative Cloud"].notes

    def test_catalog_keeps_launchers_but_filters_individual_games(self):
        from quickhatch.analysis.app_catalog import filter_migration_apps

        apps = [
            AppInfo(name="Steam", source="winget"),
            AppInfo(name="Heroic Games Launcher", source="winget"),
            AppInfo(name="Cyberpunk 2077", source="registry"),
            AppInfo(name="Elden Ring", source="registry"),
            AppInfo(name="Discord", source="winget"),
        ]

        visible = filter_migration_apps(apps)
        names = [app.name for app in visible]
        assert "Steam" in names
        assert "Heroic Games Launcher" in names
        assert "Discord" in names
        assert "Cyberpunk 2077" not in names
        assert "Elden Ring" not in names

    def test_scan_ui_explains_game_filtering(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "Games are handled through launchers." in html
        assert "Launchers & gaming tools" in html

    def test_app_bulk_select_button_can_toggle_to_unselect(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert 'id="appsBulkToggle"' in html
        assert "function appsBulkToggle()" in html
        assert "Unselect all visible" in html
        assert "Unselect all" in html

    def test_selected_catalog_context_scopes_install_hint_to_distro(self, monkeypatch):
        from quickhatch.analysis import app_catalog

        entry = app_catalog.AppCatalogEntry(
            id="override:hinted",
            name="Hinted App",
            compatibility="native",
            alternatives=("Hinted App",),
            source_hint="flatpak",
            confidence="high",
            category="utilities",
            notes="Has hints.",
            install_hints={
                "arch": {
                    "source": "flatpak",
                    "packages": ["org.example.HintedApp"],
                    "notes": "Use Flathub.",
                },
                "fedora": {
                    "source": "repo",
                    "packages": ["hinted-app"],
                    "notes": "Use dnf.",
                },
            },
        )
        monkeypatch.setattr(app_catalog, "load_catalog", lambda: (entry,))

        rows = app_catalog.selected_app_catalog_context(["Hinted App"], distro_family="Arch Linux")

        assert rows[0]["active_install_hint"]["family"] == "arch"
        assert rows[0]["active_install_hint"]["packages"] == ["org.example.HintedApp"]
        assert "install_hints" not in rows[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
