"""Smoke tests for the 4 largest previously-untested modules.

Not deep unit tests — each one just exercises the public surface so an
obvious regression (import error, crash on empty input, wrong output
type, stray mutable default) fails a test instead of silently shipping.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from quickhatch.config import (
    AIConfig,
    AppInfo,
    GPUInfo,
    HardwareInfo,
    Preferences,
    UserProfile,
)

# ---------------------------------------------------------------------------
# wizard/usb.py — USB drive listing + ISO URL lookup
# ---------------------------------------------------------------------------


class TestUsbDriveListing:
    def test_empty_on_subprocess_failure(self, monkeypatch):
        """Every platform branch swallows FileNotFoundError/TimeoutExpired
        and returns an empty list. No crash on missing tools."""
        from quickhatch.wizard import usb

        def boom(*a, **k):
            raise FileNotFoundError("tool missing")

        monkeypatch.setattr(subprocess, "run", boom)
        result = usb._list_usb_drives()
        assert result == []

    def test_parses_windows_json_single_disk(self, monkeypatch):
        """PowerShell returns a bare object when there's one drive
        (not a list). The code must handle both shapes."""
        from quickhatch.wizard import usb

        monkeypatch.setattr("sys.platform", "win32")
        one_disk = json.dumps(
            {"Number": 1, "FriendlyName": "Kingston DataTraveler", "SizeGB": 14.5}
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=one_disk, stderr=""),
        )
        drives = usb._list_usb_drives()
        assert len(drives) == 1
        assert drives[0]["name"] == "Kingston DataTraveler"
        assert drives[0]["size_gb"] == 14.5

    def test_parses_windows_json_multiple_disks(self, monkeypatch):
        from quickhatch.wizard import usb

        monkeypatch.setattr("sys.platform", "win32")
        two_disks = json.dumps(
            [
                {"Number": 1, "FriendlyName": "USB Drive A", "SizeGB": 8},
                {"Number": 2, "FriendlyName": "USB Drive B", "SizeGB": 32},
            ]
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=two_disks, stderr=""),
        )
        drives = usb._list_usb_drives()
        assert len(drives) == 2
        assert {d["size_gb"] for d in drives} == {8, 32}

    def test_parses_linux_lsblk_sizes(self, monkeypatch):
        """Size parsing of lsblk's mixed G/T/M suffixes."""
        from quickhatch.wizard import usb

        monkeypatch.setattr("sys.platform", "linux")
        lsblk = json.dumps(
            {
                "blockdevices": [
                    {
                        "name": "sdb",
                        "model": "Corsair Voyager",
                        "type": "disk",
                        "tran": "usb",
                        "rm": "1",
                        "size": "14.5G",
                    },
                    {
                        "name": "sdc",
                        "model": "SanDisk Cruzer",
                        "type": "disk",
                        "tran": "usb",
                        "rm": "1",
                        "size": "500M",
                    },
                    {
                        "name": "sda",
                        "type": "disk",
                        "tran": "sata",  # internal — must be skipped
                        "rm": "0",
                        "size": "500G",
                    },
                ]
            }
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=lsblk, stderr=""),
        )
        drives = usb._list_usb_drives()
        assert len(drives) == 2, "internal SATA disk must not appear"
        by_name = {d["name"]: d for d in drives}
        assert by_name["Corsair Voyager"]["size_gb"] == 14.5
        # 500M should round to ~0.5G
        assert abs(by_name["SanDisk Cruzer"]["size_gb"] - 0.49) < 0.1


class TestDistroIsoLookup:
    def test_known_distro_resolves(self):
        """distros.json ships alongside the package; a known entry must
        return a non-empty URL."""
        from quickhatch.wizard.usb import _get_distro_iso_url

        # Pick a handful of common names; at least ONE must resolve.
        results = [_get_distro_iso_url(n) for n in ("Ubuntu", "Fedora", "Arch Linux", "Debian")]
        assert any(r for r in results), f"no distros resolved in {results}"

    def test_unknown_distro_returns_none(self):
        from quickhatch.wizard.usb import _get_distro_iso_url

        assert _get_distro_iso_url("Not A Real Distro 9000") is None

    def test_case_insensitive_match(self):
        from quickhatch.wizard.usb import _get_distro_iso_url

        a = _get_distro_iso_url("ubuntu")
        b = _get_distro_iso_url("UBUNTU")
        c = _get_distro_iso_url("Ubuntu")
        # Either all None (distros.json missing in test env) or all equal.
        assert a == b == c


# ---------------------------------------------------------------------------
# gallery/describe.py — Markdown formatting for the curator batches
# ---------------------------------------------------------------------------


class TestFormatEntryForCurator:
    def _entry(self, **over):
        base = {
            "id": "hyprland-catppuccin",
            "upvotes": 1234,
            "source": "reddit",
            "title": "Catppuccin Hyprland",
            "source_url": "https://reddit.com/r/unixporn/example",
            "dotfiles_url": "https://github.com/user/dotfiles",
            "de": "Hyprland",
            "theme": "Catppuccin",
            "icons": "Papirus",
            "font_ui": "Inter",
            "components": {"panel": "waybar", "terminal": "kitty"},
            "decorations": {"rounded_corners_px": 12, "blur": True},
            "signature_features": ["rainbow cursor", "animated workspaces"],
            "wallpaper_hint": "mountain scene",
        }
        base.update(over)
        return base

    def test_full_entry_renders_sections(self):
        from quickhatch.gallery.describe import format_entry_for_curator

        out = format_entry_for_curator(self._entry())
        assert "## hyprland-catppuccin" in out
        assert "1234 upvotes" in out
        assert "### Core" in out
        assert "### Layout" in out
        assert "### Decorations" in out
        assert "### Signature features" in out
        assert "rainbow cursor" in out
        assert "mountain scene" in out

    def test_sparse_entry_omits_empty_sections(self):
        from quickhatch.gallery.describe import format_entry_for_curator

        out = format_entry_for_curator({"id": "minimal", "de": "i3"})
        assert "## minimal" in out
        assert "### Core" in out
        # Missing sections should not appear
        assert "### Layout" not in out
        assert "### Decorations" not in out
        assert "### Signature features" not in out

    def test_totally_empty_entry_still_renders_header(self):
        from quickhatch.gallery.describe import format_entry_for_curator

        out = format_entry_for_curator({})
        assert "## None" in out  # id missing → "None" — not a crash


class TestRenderCuratorBatch:
    def test_empty_list(self):
        from quickhatch.gallery.describe import render_curator_batch

        assert render_curator_batch([]) == ""

    def test_joins_entries_with_separator(self):
        from quickhatch.gallery.describe import render_curator_batch

        entries = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        out = render_curator_batch(entries)
        assert out.count("\n\n---\n\n") == 2
        assert "## a" in out and "## b" in out and "## c" in out

    def test_start_end_slice(self):
        from quickhatch.gallery.describe import render_curator_batch

        entries = [{"id": f"e{i}"} for i in range(10)]
        out = render_curator_batch(entries, start=2, end=5)
        assert "## e2" in out and "## e4" in out
        assert "## e0" not in out and "## e5" not in out


# ---------------------------------------------------------------------------
# gallery/scrape.py — cache-path helpers (the only parts safe to unit-test;
# the network paths are covered by integration / manual scrape runs)
# ---------------------------------------------------------------------------


class TestScrapeCachePath:
    def test_cache_name_deterministic_same_inputs(self, tmp_path, monkeypatch):
        from quickhatch.gallery import scrape

        monkeypatch.setattr(scrape, "RAW_CACHE_DIR", tmp_path)
        a = scrape._cache_name("reddit", "unixporn", "t3_abc")
        b = scrape._cache_name("reddit", "unixporn", "t3_abc")
        assert a == b
        assert a.parent == tmp_path

    def test_cache_name_differs_per_subpath(self, tmp_path, monkeypatch):
        from quickhatch.gallery import scrape

        monkeypatch.setattr(scrape, "RAW_CACHE_DIR", tmp_path)
        a = scrape._cache_name("reddit", "unixporn", "post1")
        b = scrape._cache_name("reddit", "unixporn", "post2")
        assert a != b

    def test_cli_parser_builds(self):
        """The argparse parser builds without erroring (catches
        copy-paste typos in --flag definitions)."""
        from quickhatch.gallery.scrape import build_parser

        parser = build_parser()
        # Parse zero args — defaults should be intact
        args = parser.parse_args([])
        # At least these two are exposed on the current CLI
        assert hasattr(args, "refresh")
        assert isinstance(args.refresh, bool)


# ---------------------------------------------------------------------------
# ai/instruction_gen.py — document assembly for the AI agent brief
# ---------------------------------------------------------------------------


class TestInstructionGeneration:
    def _profile(self, **over) -> UserProfile:
        p = UserProfile(
            preferences=Preferences(
                migration_type="fresh_install",
                distro_preference="arch",
                desktop_look="macos_like",
                additional_notes="prefers vibrant themes",
            ),
            ai_config=AIConfig(provider="local_lmstudio", model_name="gemma"),
            hardware=HardwareInfo(
                cpu="Ryzen 9 5900X",
                cpu_cores=12,
                ram_gb=32.0,
                gpus=[GPUInfo(name="RTX 3080", vendor="nvidia")],
            ),
            apps=[AppInfo(name="Brave"), AppInfo(name="Steam")],
            apps_to_install=["Brave", "Steam"],
        )
        for k, v in over.items():
            setattr(p, k, v) if hasattr(p, k) else setattr(p.preferences, k, v)
        return p

    def test_generate_returns_non_empty_markdown(self):
        from quickhatch.ai.instruction_gen import generate_instructions

        text = generate_instructions(self._profile())
        assert isinstance(text, str)
        assert len(text) > 500
        # Must embed user context
        assert "Ryzen" in text or "5900X" in text
        assert "arch" in text.lower()

    def test_customize_linux_branch_differs_from_migration(self):
        from quickhatch.ai.instruction_gen import generate_instructions

        migrate = generate_instructions(self._profile())
        customize = generate_instructions(self._profile(migration_type="customize_linux"))
        assert migrate != customize
        assert "SSH" in migrate
        assert "directly" in customize.lower() or "locally" in customize.lower()

    def test_empty_profile_doesnt_crash(self):
        """A barely-configured profile (user mashed past most steps)
        must still produce a document — downstream LLM can handle
        missing data, but an exception here would break the pipeline."""
        from quickhatch.ai.instruction_gen import generate_instructions

        text = generate_instructions(UserProfile())
        assert isinstance(text, str)
        assert len(text) > 100

    def test_save_instructions_writes_file(self, tmp_path):
        from quickhatch.ai.instruction_gen import save_instructions

        p = self._profile()
        out_path = save_instructions(p, tmp_path)
        assert out_path.exists()
        assert out_path.suffix.lower() in (".md", ".txt")
        content = out_path.read_text(encoding="utf-8")
        assert "arch" in content.lower()

    def test_api_key_not_leaked_in_generated_instructions(self):
        """The instructions go into the export bundle — the agent's API
        key must never appear in the output."""
        from quickhatch.ai.instruction_gen import generate_instructions

        p = self._profile()
        p.ai_config.api_key = "sk-ant-super-secret-dont-leak-123"
        text = generate_instructions(p)
        assert "sk-ant-super-secret" not in text
        assert "dont-leak" not in text

    def test_export_instructions_are_reference_brief_not_runtime_prompt(self):
        from quickhatch.ai.instruction_gen import generate_instructions

        text = generate_instructions(self._profile())
        assert "Reference Brief" in text
        assert "not the authoritative live analysis prompt" in text
        assert "sectioned prompts with structured outputs" in text

    def test_gallery_picks_are_called_authoritative_in_export_brief(self):
        from quickhatch.ai.instruction_gen import generate_instructions

        p = self._profile()
        p.preferences.gallery_picks = ["unixporn-zyx123"]
        text = generate_instructions(p)
        assert "unixporn-zyx123" in text
        assert "authoritative desktop references" in text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
