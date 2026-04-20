"""Regression tests for the pre-reboot safety guard.

The guard exists because a real install booted into emergency mode
with a locked root account — `encrypt` hook was missing from
mkinitcpio HOOKS, so the initramfs couldn't unlock the LUKS root, and
sulogin refused to drop into a shell because root had no password.

This test set anchors the parser side: we don't actually SSH anywhere,
but we verify that a STATUS-line transcript from the check script
gets correctly classified into failures vs OKs, and that the FAIL
keys we care about are recognized.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from quickhatch.gui import server as server_mod


def _fake_ssh(rc: int, stdout: str, stderr: str = ""):
    """Build a stub for _ssh_exec that returns (rc, stdout, stderr)."""

    def _stub(host, user, cmd, key_path=None, timeout=120, port=22):
        return (rc, stdout, stderr)

    return _stub


class TestPreRebootChecksParser:
    def test_all_ok_returns_no_failures(self):
        out = "\n".join(
            [
                "STATUS|OK|fstab|/etc/fstab present (8 lines)",
                "STATUS|OK|kernel|/boot has 1 kernel(s) and 1 initramfs image(s)",
                "STATUS|OK|initramfs|initramfs newer than mkinitcpio.conf",
                "STATUS|OK|hooks|HOOKS includes encrypt",
                "STATUS|OK|cmdline|Kernel cmdline references LUKS",
                "STATUS|OK|bootloader|GRUB present",
                "STATUS|OK|recovery|Root recovery available (root_pw=1, wheel+sudoers=1)",
            ]
        )
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(0, out)):
            failures, oks = server_mod._run_pre_reboot_checks("host", "root", None, port=22)
        assert failures == []
        assert len(oks) == 7

    def test_missing_encrypt_hook_is_a_failure(self):
        out = "\n".join(
            [
                "STATUS|OK|fstab|/etc/fstab present",
                "STATUS|OK|luks-present|LUKS containers detected: sda2",
                "STATUS|FAIL|hooks|LUKS in use but HOOKS lacks 'encrypt'",
            ]
        )
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(0, out)):
            failures, oks = server_mod._run_pre_reboot_checks("host", "root", None, port=22)
        assert len(failures) == 1
        assert "encrypt" in failures[0].lower()
        assert "hooks" in failures[0]

    def test_locked_root_is_a_failure(self):
        out = "\n".join(
            [
                "STATUS|OK|fstab|/etc/fstab present",
                "STATUS|FAIL|recovery|Root account is locked AND no sudo-capable user",
            ]
        )
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(0, out)):
            failures, oks = server_mod._run_pre_reboot_checks("host", "root", None, port=22)
        assert len(failures) == 1
        assert "root" in failures[0].lower()
        assert "wheel" in failures[0].lower() or "sudo" in failures[0].lower()

    def test_stale_initramfs_is_a_failure(self):
        out = "STATUS|FAIL|initramfs|mkinitcpio.conf was edited AFTER initramfs was built"
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(0, out)):
            failures, _ = server_mod._run_pre_reboot_checks("h", "u", None)
        assert len(failures) == 1
        assert "initramfs" in failures[0].lower()

    def test_multiple_failures_all_collected(self):
        out = "\n".join(
            [
                "STATUS|FAIL|hooks|missing encrypt",
                "STATUS|FAIL|cmdline|missing cryptdevice=",
                "STATUS|FAIL|recovery|root locked + no wheel",
            ]
        )
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(0, out)):
            failures, _ = server_mod._run_pre_reboot_checks("h", "u", None)
        assert len(failures) == 3

    def test_already_installed_chroot_skipped(self):
        """The check script SKIPs if /mnt has no /etc — meaning we're
        already on the installed system, not chrooting from a live ISO.
        That should NOT count as a failure."""
        out = "STATUS|SKIP|chroot|/mnt has no /etc — likely already on the installed system"
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(0, out)):
            failures, _ = server_mod._run_pre_reboot_checks("h", "u", None)
        assert failures == []

    def test_script_ssh_failure_returns_a_diagnostic_failure(self):
        """If the check script can't even run (SSH error, no shell),
        we still return a failure so the agent doesn't blindly reboot."""
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(255, "", "Connection refused")):
            failures, _ = server_mod._run_pre_reboot_checks("h", "u", None)
        assert len(failures) == 1
        assert "rc=255" in failures[0] or "failed to run" in failures[0].lower()

    def test_malformed_status_lines_silently_dropped(self):
        """Garbage from the script (non-STATUS lines, half-formatted
        rows) should not crash the parser or get treated as a failure."""
        out = "\n".join(
            [
                "+ /usr/bin/grep ...",
                "STATUS|OK|fstab|fine",
                "STATUS|incomplete",  # too few fields
                "warning: stat: bad fd",
                "STATUS|FAIL|hooks|missing encrypt",
            ]
        )
        with patch.object(server_mod, "_ssh_exec", _fake_ssh(0, out)):
            failures, oks = server_mod._run_pre_reboot_checks("h", "u", None)
        assert len(failures) == 1
        assert len(oks) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
