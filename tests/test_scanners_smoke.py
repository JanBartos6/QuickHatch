"""Smoke tests for the scanners that had zero coverage before.

These don't try to exhaustively cover every winget/dpkg/rpm output
format — they pin down the contract we rely on (clean names, no
placeholder entries, no raw column leakage) and catch obvious
regressions.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# apps — winget parser (HIGH-traffic path on Windows)
# ---------------------------------------------------------------------------


class TestWingetParser:
    @staticmethod
    def _fake_winget(output: str):
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    def test_clean_names_no_trailing_id_letter(self, monkeypatch):
        """The original bug: short names got the first Id char appended
        because of a blind parts[:40] slice."""
        from quickhatch.scanners import apps as apps_mod

        output = (
            "Name                                   Id                                     Version\n"
            "-------------------------------------------------------------------------------------\n"
            "Brave                                  Brave.Brave                            147.1.89.137\n"
            "Google Chrome                          Google.Chrome.EXE                      147.0.7727.101\n"
            "Vivaldi                                ARP\\User\\X64\\Vivaldi                   7.9.3970.55\n"
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: self._fake_winget(output),
        )
        import sys as _sys

        monkeypatch.setattr(_sys, "platform", "win32")
        apps = apps_mod._scan_winget()
        names = [a.name for a in apps]
        assert "Brave" in names
        assert "Google Chrome" in names
        assert "Vivaldi" in names
        # The feared failure modes:
        assert not any(n.endswith(" B") or n.endswith(" G") or n.endswith(" V") for n in names), (
            f"Trailing-id-letter regressed: {names}"
        )

    def test_skips_progress_spinner_rows(self, monkeypatch):
        """winget prints progress spinner characters sometimes;
        don't turn them into 'apps' named '-' or '/'."""
        from quickhatch.scanners import apps as apps_mod

        output = (
            "\\\n"
            "|\n"
            "Name                                   Id\n"
            "---------------------------------------\n"
            "Brave                                  Brave.Brave\n"
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: self._fake_winget(output))
        import sys as _sys

        monkeypatch.setattr(_sys, "platform", "win32")
        apps = apps_mod._scan_winget()
        names = [a.name for a in apps]
        assert "Brave" in names
        assert not any(n in ("\\", "|", "/") for n in names)

    def test_handles_output_with_no_separator(self, monkeypatch):
        """On some non-English locales winget may not emit the
        dash-separator line as expected. The fallback split-on-two-
        spaces path must still produce clean names."""
        from quickhatch.scanners import apps as apps_mod

        output = "Brave  Brave.Brave  147.1.89\nSteam  Valve.Steam  3.2\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: self._fake_winget(output))
        import sys as _sys

        monkeypatch.setattr(_sys, "platform", "win32")
        apps = apps_mod._scan_winget()
        # Fallback path should at least not crash and should produce
        # SOMETHING non-empty for non-trivial input.
        names = {a.name for a in apps}
        # Not asserting exact set because the fallback is best-effort
        # on malformed input — but any name it did produce must not be
        # a separator artifact.
        assert not any(set(n) <= {"-", " ", "="} for n in names)

    def test_returns_empty_list_on_subprocess_failure(self, monkeypatch):
        import sys as _sys

        from quickhatch.scanners import apps as apps_mod

        def boom(*a, **k):
            raise FileNotFoundError("winget not installed")

        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.setattr(_sys, "platform", "win32")
        assert apps_mod._scan_winget() == []


# ---------------------------------------------------------------------------
# apps — pacman / dpkg / rpm parsers (Linux paths that had zero tests)
# ---------------------------------------------------------------------------


class TestLinuxPackageScanners:
    def test_scan_pacman_parses_list(self, monkeypatch):
        from quickhatch.scanners import apps as apps_mod

        output = "firefox 120.0-1\nlibreoffice-fresh 7.6\ngit 2.42\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=output, stderr=""),
        )
        out = apps_mod._scan_pacman()
        names = [a.name for a in out]
        assert "firefox" in names
        assert "git" in names
        assert all(a.source == "pacman" for a in out)

    def test_scan_pacman_empty_on_failure(self, monkeypatch):
        from quickhatch.scanners import apps as apps_mod

        def boom(*a, **k):
            raise FileNotFoundError("pacman not installed")

        monkeypatch.setattr(subprocess, "run", boom)
        assert apps_mod._scan_pacman() == []


# ---------------------------------------------------------------------------
# settings scanner smoke test
# ---------------------------------------------------------------------------


class TestSettingsScanner:
    def test_scan_returns_dict_on_unsupported_platform(self, monkeypatch):
        import sys as _sys

        from quickhatch.scanners import settings

        monkeypatch.setattr(_sys, "platform", "sunos5")  # very unsupported
        result = settings.scan()
        assert isinstance(result, dict)
        # Should not raise; content may be empty or partial.


# ---------------------------------------------------------------------------
# files scanner smoke test
# ---------------------------------------------------------------------------


class TestFilesScanner:
    def test_scan_returns_dict(self, monkeypatch):
        from quickhatch.scanners import files

        # Scanner reads from real home dir. Just ensure it returns the
        # right shape without raising — specific numbers are brittle.
        result = files.scan()
        assert isinstance(result, dict)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
