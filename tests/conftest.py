from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home_dir(tmp_path, monkeypatch):
    """Keep the test suite out of the real user profile.

    QuickHatch persists checkpoints, export bundles, helper scripts, and SSH
    fallbacks under ``Path.home()``. Without a suite-wide home override, tests
    mutate the developer's real ``~/.quickhatch-*`` files and the next manual
    GUI restart boots from test data instead of the live wizard state.
    """

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOMEDRIVE", fake_home.drive or "C:")
    monkeypatch.setenv("HOMEPATH", fake_home.root if fake_home.root else "\\")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home
