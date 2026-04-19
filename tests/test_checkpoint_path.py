"""Tests for UserProfile.save_checkpoint / load_checkpoint path handling.

Anchors two behaviours:
1. The default checkpoint is ~/.quickhatch-checkpoint.json, not a
   CWD-relative file. Relaunching QuickHatch from a different working
   directory must find the same checkpoint.
2. Legacy CWD-local checkpoints migrate to the home-dir location on
   first load.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from quickhatch import config as config_mod
from quickhatch.config import UserProfile


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() into tmp_path so each test gets an isolated
    $HOME."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class TestCheckpointPath:
    def test_default_path_is_under_home(self, fake_home):
        p = config_mod._checkpoint_path()
        assert p == fake_home / ".quickhatch-checkpoint.json"

    def test_save_then_load_roundtrip_across_cwd_change(self, fake_home, tmp_path, monkeypatch):
        """This is the original bug — save from CWD A, load from CWD B
        should work because the checkpoint anchors to $HOME."""
        profile = UserProfile()
        profile.preferences.distro_preference = "Arch Linux"
        profile.linux_user = "archuser"

        cwd_a = tmp_path / "dir-a"
        cwd_a.mkdir()
        monkeypatch.chdir(cwd_a)
        profile.save_checkpoint()

        cwd_b = tmp_path / "dir-b"
        cwd_b.mkdir()
        monkeypatch.chdir(cwd_b)
        loaded = UserProfile.load_checkpoint()
        assert loaded is not None
        assert loaded.preferences.distro_preference == "Arch Linux"
        assert loaded.linux_user == "archuser"

    def test_atomic_write_no_partial_file(self, fake_home):
        """After save, no .tmp sibling should remain — os.replace
        guarantees atomic rename."""
        profile = UserProfile()
        profile.save_checkpoint()
        home_files = {p.name for p in fake_home.iterdir()}
        assert ".quickhatch-checkpoint.json" in home_files
        assert ".quickhatch-checkpoint.json.tmp" not in home_files

    def test_utf8_nonascii_roundtrip(self, fake_home):
        profile = UserProfile()
        profile.additional_info = "NVIDIA → open kernel modules, café config"
        profile.save_checkpoint()
        loaded = UserProfile.load_checkpoint()
        assert loaded is not None
        assert loaded.additional_info == profile.additional_info


class TestLegacyMigration:
    def test_legacy_cwd_file_migrates_on_load(self, fake_home, tmp_path, monkeypatch):
        """User launches QuickHatch for the first time after the path
        fix; a legacy ./.quickhatch-checkpoint.json exists in the CWD.
        It should be relocated into $HOME and still load cleanly."""
        cwd = tmp_path / "legacy"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        # Hand-craft a legacy file in CWD.
        legacy_payload = {
            "preferences": {"distro_preference": "Fedora"},
            "ai_config": {},
            "hardware": {},
            "apps": [],
            "files_summary": {},
            "windows_settings": {},
            "ssh_public_key_path": None,
            "additional_info": "",
            "apps_to_install": [],
            "data_description": "",
            "data_transfer_method": "",
            "linux_ip": "",
            "linux_user": "",
            "completed_steps": ["welcome", "ai_setup"],
        }
        legacy_file = cwd / ".quickhatch-checkpoint.json"
        legacy_file.write_text(json.dumps(legacy_payload), encoding="utf-8")

        loaded = UserProfile.load_checkpoint()
        assert loaded is not None
        assert loaded.preferences.distro_preference == "Fedora"
        assert "ai_setup" in loaded.completed_steps
        # Legacy file should have been moved to the new location.
        assert not legacy_file.exists()
        assert (fake_home / ".quickhatch-checkpoint.json").exists()

    def test_legacy_does_not_clobber_newer_home_checkpoint(self, fake_home, tmp_path, monkeypatch):
        """If the user has a newer checkpoint at $HOME AND a stale
        legacy file in CWD, the HOME one wins."""
        cwd = tmp_path / "legacy"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        # Newer HOME checkpoint: Arch.
        newer = UserProfile()
        newer.preferences.distro_preference = "Arch Linux"
        newer.save_checkpoint()

        # Older legacy in CWD: Fedora (should be ignored).
        legacy_payload = {
            "preferences": {"distro_preference": "Fedora"},
            "ai_config": {},
            "hardware": {},
            "apps": [],
            "files_summary": {},
            "windows_settings": {},
            "ssh_public_key_path": None,
            "additional_info": "",
            "apps_to_install": [],
            "data_description": "",
            "data_transfer_method": "",
            "linux_ip": "",
            "linux_user": "",
            "completed_steps": [],
        }
        (cwd / ".quickhatch-checkpoint.json").write_text(
            json.dumps(legacy_payload), encoding="utf-8"
        )

        loaded = UserProfile.load_checkpoint()
        assert loaded is not None
        assert loaded.preferences.distro_preference == "Arch Linux"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
