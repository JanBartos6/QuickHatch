"""Tests for _advise_on_repeated_failure — the auto-hint system that
kicks in when the setup agent gets stuck hammering the same failing
command. Motivated by the WhiteSur AUR lock-file loop where the
script's own `exec 2> lockdir/error_log.txt` redirect trapped its
error message inside the very lock directory it was trying to report.
"""

from __future__ import annotations

import pytest

from quickhatch.gui.server import _advise_on_repeated_failure


class TestLockHint:
    def test_already_running_triggers_mutex_hint(self):
        hint = _advise_on_repeated_failure(
            "yay -S --noconfirm whitesur-gtk-theme",
            "ERROR: Whitesur installer or tweaks is already running. Probably it's run by 'archuser'",
            recent=[("yay -S whitesur", 1)],
        )
        assert hint is not None
        assert "lock" in hint.lower() or "mutex" in hint.lower()
        assert "/tmp" in hint  # points at the likely location

    def test_is_in_use_triggers_mutex_hint(self):
        hint = _advise_on_repeated_failure(
            "systemctl start foo",
            "Error: resource is in use by another process",
            recent=[],
        )
        assert hint is not None


class TestPacmanDatabaseLockHint:
    def test_pacman_db_lock(self):
        hint = _advise_on_repeated_failure(
            "sudo pacman -Syu",
            "error: failed to init transaction (unable to lock database)\n"
            "error: could not lock database: File exists",
            recent=[],
        )
        assert hint is not None
        assert "pacman" in hint.lower() or "db.lck" in hint


class TestDiskFullHint:
    def test_disk_full(self):
        hint = _advise_on_repeated_failure(
            "pacstrap -K /mnt base",
            "tar: write error: No space left on device",
            recent=[],
        )
        assert hint is not None
        assert "disk" in hint.lower() or "space" in hint.lower()


class TestPermissionHint:
    def test_perm_denied(self):
        hint = _advise_on_repeated_failure(
            "echo foo > /etc/sudoers.d/test",
            "bash: /etc/sudoers.d/test: Permission denied",
            recent=[],
        )
        assert hint is not None
        assert "sudo" in hint.lower() or "permission" in hint.lower()


class TestNetworkHint:
    def test_network_hint(self):
        hint = _advise_on_repeated_failure(
            "curl -sS https://example.com/x",
            "curl: (6) Could not resolve host: example.com",
            recent=[],
        )
        assert hint is not None
        assert "network" in hint.lower() or "connectivity" in hint.lower()


class TestMissingFileHint:
    def test_file_not_found(self):
        hint = _advise_on_repeated_failure(
            "cat /etc/vconsole.conf",
            "cat: /etc/vconsole.conf: No such file or directory",
            recent=[],
        )
        assert hint is not None


class TestRepeatedCommandHint:
    def test_three_fails_same_command_triggers_generic_hint(self):
        """No pattern match, but same command has failed 3 times —
        surface the generic 'consider cleanup / different approach' advice."""
        cmd = "weird-tool --something-odd"
        recent = [(cmd, 1), (cmd, 1), (cmd, 1)]
        hint = _advise_on_repeated_failure(cmd, "unhelpful output", recent=recent)
        assert hint is not None
        assert "failed" in hint.lower() or "cleanup" in hint.lower()
        # Count matches the actual failures, not +1 (recent includes current)
        assert "3 times" in hint
        assert "4 times" not in hint

    def test_two_fails_same_command_triggers_generic_hint(self):
        """Generic hint should fire on the 2nd failure, not wait for 3rd."""
        cmd = "weird-tool"
        hint = _advise_on_repeated_failure(cmd, "unhelpful output", recent=[(cmd, 1), (cmd, 1)])
        assert hint is not None
        assert "2 times" in hint

    def test_single_failure_no_match_returns_none(self):
        """First failure with no pattern match — don't spam hints."""
        hint = _advise_on_repeated_failure(
            "weird-one-off-cmd",
            "some unique output that doesn't match any pattern",
            recent=[("weird-one-off-cmd", 1)],
        )
        assert hint is None

    def test_different_commands_dont_stack(self):
        """3 different commands each failing once ≠ 3 fails of the same."""
        hint = _advise_on_repeated_failure(
            "cmd-c",
            "generic output",
            recent=[("cmd-a", 1), ("cmd-b", 1), ("cmd-c", 1)],
        )
        assert hint is None


class TestPatternFirstHit:
    def test_pattern_match_fires_on_first_failure(self):
        """Don't wait for repetition when we already know the shape
        of the problem — fire on the first matching output."""
        hint = _advise_on_repeated_failure(
            "yay -S somepkg",
            "ERROR: already running",
            recent=[("yay -S somepkg", 1)],
        )
        assert hint is not None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
