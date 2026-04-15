"""Tests for map_apps — Windows-to-Linux compatibility classifier."""

from __future__ import annotations

import pytest

from quickhatch.analysis.app_mapper import _fuzzy_match, map_apps
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
